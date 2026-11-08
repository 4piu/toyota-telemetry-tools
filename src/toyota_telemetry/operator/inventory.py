"""Candidate inventory: imported rows, manual endpoints, and the signal tree.

The inventory is ignored machine-local state (``state/inventory.json``). It
holds every imported candidate row, including ECUs whose endpoint is not yet
confirmed. Only rows for confirmed ECUs enter the rebuildable cache, so the
tracked authorities never depend on unverified addresses.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping

from ..i18n import _, N_, ngettext
from ..identity import (
    SignalRef, address_suffix, canonical_did, canonical_target_address, stable_slug, within_read_range,
)
from ..repository import (
    ProjectRepository,
    _atomic_write,
    _canonical_json,
    _encoded_json,
    _exact_json,
    _normalized_catalog_rows,
    catalog_variant_id,
    connect_cache,
)
from ..objectives import condition_refs
from .sources import SourceRows, canonical_can_id


INVENTORY_FILE = "inventory.json"
# Schema 2: one merged list; each version names the sources that supply it.
INVENTORY_SCHEMA_VERSION = 2

# DID states: compared in code; marked so the interface can translate them.
VERIFIED = N_("Verified")
NOT_VERIFIED = N_("Not verified")
FAILED = N_("Failed")
ENDPOINT_NOT_VERIFIED = N_("Endpoint not verified")
# The endpoint was probed and did not answer, so its DIDs were not read.
ENDPOINT_SILENT = N_("ECU did not answer")

# One status ladder per signal, from least to most established.
SIGNAL_NOT_CHECKED = N_("Not checked")
SIGNAL_NO_ANSWER = N_("No answer")
SIGNAL_FAILED = N_("Refused")
SIGNAL_RESPONDS = N_("Responds")
SIGNAL_RECORDED = N_("Recorded")
SIGNAL_REVIEWED = N_("Reviewed")
SIGNAL_STATUSES = (
    SIGNAL_NOT_CHECKED, SIGNAL_NO_ANSWER, SIGNAL_FAILED, SIGNAL_RESPONDS, SIGNAL_RECORDED, SIGNAL_REVIEWED,
)

# Review evidence states from the project method; a review in any state other
# than Candidate counts as Reviewed. Stored in the review file as written.
EVIDENCE_STATES = (
    N_("Candidate"), N_("Observed"), N_("Correlated"), N_("Validated"),
    N_("Observed and replayed"), N_("Correlated and replayed"), N_("Validated and replayed"),
)

OUTCOME_TEXT = {
    "negative": N_("refused"),
    "timeout": N_("no answer"),
    "transport_error": N_("connection error"),
    "session_unavailable": N_("session could not be opened"),
}
# Common UDS negative response codes in plain words.
NRC_TEXT = {
    "0x10": N_("general reject"),
    "0x11": N_("service not supported"),
    "0x12": N_("sub-function not supported"),
    "0x13": N_("wrong message length"),
    "0x14": N_("response too long"),
    "0x22": N_("conditions not correct"),
    "0x31": N_("request out of range"),
    "0x33": N_("security access required"),
    "0x72": N_("general programming failure"),
    "0x7E": N_("not supported in this session"),
    "0x7F": N_("service not supported in this session"),
}


def outcome_text(outcome: str, nrc: str | None = None) -> str:
    text = _(OUTCOME_TEXT[outcome]) if outcome in OUTCOME_TEXT else outcome
    if nrc:
        meaning = NRC_TEXT.get(nrc.upper().replace("0X", "0x"))
        if meaning:
            return _("{outcome}: {meaning} (code {code})").format(outcome=text, meaning=_(meaning), code=nrc)
        return _("{outcome} (code {code})").format(outcome=text, code=nrc)
    return text


def inventory_path(repository: ProjectRepository):
    return repository.paths.state / INVENTORY_FILE


def empty_inventory(project_id: str) -> dict[str, Any]:
    return {
        "document_type": "telemetry.candidate-inventory",
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "project_id": project_id,
        "sources": [],
        "rows": [],
        "manual_endpoints": [],
    }


def load_inventory(repository: ProjectRepository) -> dict[str, Any]:
    path = inventory_path(repository)
    if not path.is_file():
        return empty_inventory(repository.project_id)
    document, _raw = _exact_json(path)
    validate_inventory(document, repository.project_id)
    return document


def validate_inventory(document: Mapping[str, Any], project_id: str) -> None:
    expected = {
        "document_type", "schema_version", "project_id", "sources", "rows", "manual_endpoints",
    }
    if set(document) != expected:
        raise ValueError(f"candidate inventory fields mismatch: {sorted(document)}")
    if (document["document_type"] != "telemetry.candidate-inventory"
            or document["schema_version"] != INVENTORY_SCHEMA_VERSION):
        raise ValueError(f"candidate inventory must be telemetry.candidate-inventory schema {INVENTORY_SCHEMA_VERSION}")
    if document["project_id"] != project_id:
        raise ValueError("candidate inventory belongs to a different project")
    source_ids = {source["source_id"] for source in document["sources"]}
    variants = set()
    for entry in document["rows"]:
        if set(entry) != {"sources", "row"} or not entry["sources"] or set(entry["sources"]) - source_ids:
            raise ValueError("candidate inventory row has no known source")
        SignalRef.parse(entry["row"]["signal_ref"])
        variant = entry["row"].get("catalog_variant_id")
        if variant != catalog_variant_id(entry["row"]) or variant in variants:
            raise ValueError(f"candidate inventory version id mismatch: {entry['row']['signal_ref']}")
        variants.add(variant)
    for endpoint in document["manual_endpoints"]:
        fields = {"tester_can_id", "response_can_id", "ecu_keys"}
        if set(endpoint) - {"target_address"} != fields:
            raise ValueError("candidate inventory manual endpoint fields mismatch")
        if "target_address" in endpoint and (
            canonical_target_address(endpoint["target_address"]) != endpoint["target_address"]
        ):
            raise ValueError("candidate inventory manual endpoint target_address must be canonical 0xNN")


def write_inventory(repository: ProjectRepository, document: Mapping[str, Any]) -> None:
    validate_inventory(document, repository.project_id)
    _atomic_write(inventory_path(repository), _encoded_json(document))


def confirmed_ecus(repository: ProjectRepository) -> dict[int, str]:
    return {
        int(ecu): endpoint["id"]
        for endpoint in repository.transport["endpoints"]
        for ecu in endpoint["ecu_keys"]
    }


def cache_rows(repository: ProjectRepository, inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rows eligible for the rebuildable cache: those on confirmed ECUs."""

    confirmed = confirmed_ecus(repository)
    rows = [
        entry["row"] for entry in inventory["rows"]
        if SignalRef.parse(entry["row"]["signal_ref"]).ecu_key in confirmed
    ]
    return _normalized_catalog_rows(rows)


def refresh_cache(repository: ProjectRepository, inventory: Mapping[str, Any]) -> bool:
    return repository.refresh_cache(cache_rows(repository, inventory), repository.paths.captures)


def rebuild_cache(repository: ProjectRepository, inventory: Mapping[str, Any]) -> None:
    repository.rebuild_cache(cache_rows(repository, inventory), repository.paths.captures)


def source_rows(inventory: Mapping[str, Any], source_id: str | None = None) -> list[dict[str, Any]]:
    """Candidate rows as a row file holds them (every version, or one source's).

    The workspace-assigned catalog_variant_id is left out, so importing the
    rows again yields the same signals.
    """

    if source_id is not None and source_id not in {source["source_id"] for source in inventory["sources"]}:
        known = sorted(source["source_id"] for source in inventory["sources"])
        raise ValueError(_("no imported source {source!r}; imported: {known}").format(
            source=source_id, known=", ".join(known) or _("none"),
        ))
    return [
        {name: value for name, value in entry["row"].items() if name != "catalog_variant_id"}
        for entry in inventory["rows"]
        if source_id is None or source_id in entry["sources"]
    ]


@dataclass(frozen=True)
class ImportPreview:
    """What applying a source will do, computed by the same plan that applies it."""

    source_id: str
    kind: str
    label: str
    rows: tuple[dict[str, Any], ...]
    rejections: tuple[tuple[str, str], ...]
    counts: dict[str, int]
    conflicts: tuple[str, ...]
    blocking_problem: str | None
    changed_fields: dict[str, int] = field(default_factory=dict)
    document: Mapping[str, Any] | None = None
    base_sha256: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "label": self.label,
            "counts": dict(self.counts),
            "changed_fields": dict(self.changed_fields),
            "conflicts": list(self.conflicts),
            "rejections": [
                {"where": where, "reason": reason} for where, reason in self.rejections[:50]
            ],
            "rejection_count": len(self.rejections),
            "blocking_problem": self.blocking_problem,
        }


@dataclass(frozen=True)
class DeletePreview:
    """What deleting candidate signals will do; applying writes exactly this."""

    deleted: tuple[str, ...]
    refused: dict[str, str]
    document: Mapping[str, Any]
    base_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"deleted": list(self.deleted), "refused": dict(self.refused)}


def preview_import(
    repository: ProjectRepository, inventory: Mapping[str, Any], source: SourceRows
) -> ImportPreview:
    """Plan merging a source into the candidate list, without writing anything.

    A signal is its ECU key, DID, and bits; a version of it is that plus its
    formula. Importing adds new signals and versions, updates the descriptive
    fields (name, unit, states, addresses) of existing versions, and replaces
    a version this source alone supplied when the source now gives the signal
    another formula. Signals missing from the import are kept.
    """

    did_min = int(repository.transport["safety"]["did_min"], 16)
    did_max = int(repository.transport["safety"]["did_max"], 16)
    accepted = []
    rejections = list(source.rejections)
    for row in source.rows:
        ref = SignalRef.parse(row["signal_ref"])
        if not within_read_range(ref.data_id, did_min, did_max):
            rejections.append((str(ref), _("DID is outside the transport safety range")))
            continue
        accepted.append(row)
    incoming, duplicates = _merge_versions(accepted)
    try:
        incoming = _normalized_catalog_rows(incoming)
    except ValueError as exc:
        return _blocked(source, rejections, str(exc))

    pinned = {
        review["catalog_variant_id"] for review in repository.review["signals"] if review["catalog_variant_id"]
    }
    touched = {row["signal_ref"] for row in incoming}
    before: dict[str, list[dict[str, Any]]] = {}
    entries: list[dict[str, Any]] = []
    by_variant: dict[str, dict[str, Any]] = {}
    for entry in inventory["rows"]:
        row = entry["row"]
        sources = [value for value in entry["sources"]
                   if not (row["signal_ref"] in touched and value == source.source_id)]
        copy = {"sources": sources, "row": row}
        entries.append(copy)
        by_variant[row["catalog_variant_id"]] = copy
        if row["signal_ref"] in touched:
            before.setdefault(row["signal_ref"], []).append(row)
    for row in incoming:
        existing = by_variant.get(row["catalog_variant_id"])
        if existing is None:
            entry = {"sources": [source.source_id], "row": row}
            entries.append(entry)
            by_variant[row["catalog_variant_id"]] = entry
        else:
            existing["row"] = row
            existing["sources"] = sorted({*existing["sources"], source.source_id})
    kept_pinned = 0
    rows = []
    for entry in entries:
        if not entry["sources"]:
            if entry["row"]["catalog_variant_id"] not in pinned:
                continue  # replaced: this source now gives the signal another formula
            entry["sources"] = [source.source_id]
            kept_pinned += 1
        rows.append(entry)
    rows.sort(key=lambda entry: (SignalRef.parse(entry["row"]["signal_ref"]), entry["row"]["catalog_variant_id"]))

    after: dict[str, list[dict[str, Any]]] = {}
    for entry in rows:
        if entry["row"]["signal_ref"] in touched:
            after.setdefault(entry["row"]["signal_ref"], []).append(entry["row"])
    added = updated = unchanged = 0
    changed_fields: dict[str, int] = {}
    for signal_ref in touched:
        old = {row["catalog_variant_id"]: row for row in before.get(signal_ref, [])}
        new = {row["catalog_variant_id"]: row for row in after.get(signal_ref, [])}
        if not old:
            added += 1
            continue
        if {key: _canonical_json(row) for key, row in old.items()} == {
            key: _canonical_json(row) for key, row in new.items()
        }:
            unchanged += 1
            continue
        updated += 1
        names: set[str] = set()
        if set(old) != set(new):
            names.add("formula")
        for key in set(old) & set(new):
            for name in set(old[key]) | set(new[key]):
                if _canonical_json(old[key].get(name)) != _canonical_json(new[key].get(name)):
                    names.add(name)
        for name in names:
            changed_fields[name] = changed_fields.get(name, 0) + 1
    previously = {
        entry["row"]["signal_ref"] for entry in inventory["rows"] if source.source_id in entry["sources"]
    }
    conflicts = sorted((ref for ref, values in after.items() if len(values) > 1), key=SignalRef.parse)
    document = _with_sources(inventory, rows, {
        "source_id": source.source_id, "kind": source.kind, "label": source.label,
    })
    endpoints = {_row_endpoint(row) for row in incoming} - {None}
    counts = {
        "ecus": len({SignalRef.parse(row["signal_ref"]).ecu_key for row in incoming}),
        "endpoint_candidates": len(endpoints),
        "dids": len({_did_identity(row) for row in incoming}),
        "signals": len(touched),
        "rows": len(accepted),
        "duplicates_merged": duplicates,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "kept": len(previously - touched),
        "kept_reviewed": kept_pinned,
        "conflicts": len(conflicts),
        "rejected": len(rejections),
    }
    problem = None
    try:
        repository._validate_catalog_rows(cache_rows(repository, document))
    except ValueError as exc:
        problem = _("reviewed signals would no longer resolve: {error}").format(error=exc)
    return ImportPreview(
        source.source_id, source.kind, source.label, tuple(incoming), tuple(rejections),
        counts, tuple(conflicts), problem, dict(sorted(changed_fields.items())), document,
        inventory_sha256(inventory),
    )


def apply_import(
    repository: ProjectRepository, inventory: Mapping[str, Any], preview: ImportPreview
) -> dict[str, Any]:
    """Write exactly the previewed candidate list, then rebuild the cache."""

    if preview.blocking_problem:
        raise ValueError(preview.blocking_problem)
    if preview.document is None or inventory_sha256(inventory) != preview.base_sha256:
        raise ValueError(_("the candidate list changed since the preview; preview the source again"))
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    document = {**preview.document, "sources": [
        {**row, "imported_utc": stamp} if row["source_id"] == preview.source_id else row
        for row in preview.document["sources"]
    ]}
    return _write(repository, document)


def preview_delete_signals(
    repository: ProjectRepository, inventory: Mapping[str, Any], signal_refs: Iterable[str]
) -> DeletePreview:
    """Plan deleting signals (every version) from the candidate list.

    Signals that are reviewed, targeted, used in a target condition, or
    recorded are refused, with the reason. A later import of a deleted
    signal's source adds it again.
    """

    wanted = {str(SignalRef.parse(str(ref))) for ref in signal_refs}
    present = {entry["row"]["signal_ref"] for entry in inventory["rows"]}
    reasons: dict[str, str] = {}
    for ref in wanted - present:
        reasons[ref] = N_("not in the candidate list")
    for review in repository.review["signals"]:
        reasons.setdefault(review["signal_ref"], N_("reviewed"))
    for target in repository.policy["targets"]:
        reasons.setdefault(target["signal_ref"], N_("has a target"))
        if target["condition"] is not None:
            for ref in condition_refs(target["condition"]):
                reasons.setdefault(ref, N_("used in a target condition"))
    try:
        for ref in repository.signal_observation_summary():
            reasons.setdefault(ref, N_("recorded"))
    except sqlite3.Error:
        pass
    refused = {ref: reasons[ref] for ref in sorted(wanted, key=SignalRef.parse) if ref in reasons}
    deleted = tuple(sorted(wanted - refused.keys(), key=SignalRef.parse))
    gone = set(deleted)
    rows = [entry for entry in inventory["rows"] if entry["row"]["signal_ref"] not in gone]
    return DeletePreview(deleted, refused, _with_sources(inventory, rows, None), inventory_sha256(inventory))


def apply_delete_signals(
    repository: ProjectRepository, inventory: Mapping[str, Any], preview: DeletePreview
) -> dict[str, Any]:
    if inventory_sha256(inventory) != preview.base_sha256:
        raise ValueError(_("the candidate list changed since the preview; select the signals again"))
    if not preview.deleted:
        raise ValueError(_("none of the selected signals can be deleted"))
    return _write(repository, preview.document)


def describe_import(preview: ImportPreview) -> list[str]:
    """Plain lines saying what applying the preview will do (any interface)."""

    counts = preview.counts
    lines = [
        _(
            "{label}: {rows} rows, {signals} signals, {dids} DIDs, {ecus} ECUs, "
            "{endpoints} endpoint candidates."
        ).format(
            label=preview.label, rows=f"{counts.get('rows', 0):,}", signals=f"{counts.get('signals', 0):,}",
            dids=f"{counts.get('dids', 0):,}", ecus=f"{counts.get('ecus', 0):,}",
            endpoints=f"{counts.get('endpoint_candidates', 0):,}",
        ),
        _("To add {added} signals; to update {updated}; unchanged {unchanged}.").format(
            added=f"{counts.get('added', 0):,}", updated=f"{counts.get('updated', 0):,}",
            unchanged=f"{counts.get('unchanged', 0):,}",
        ),
    ]
    if preview.changed_fields:
        # Field names are the row file's own keys and stay as they are.
        lines.append(_("Updates change: {fields}.").format(fields=", ".join(
            f"{name} {number:,}" for name, number in preview.changed_fields.items()
        )))
    if n := counts.get("kept"):
        lines.append(ngettext(
            "{count} signal from this source's last import is not in it; it is kept.",
            "{count} signals from this source's last import are not in it; they are kept.", n,
        ).format(count=f"{n:,}"))
    if n := counts.get("kept_reviewed"):
        lines.append(ngettext(
            "{count} replaced definition is kept because a review uses it.",
            "{count} replaced definitions are kept because a review uses them.", n,
        ).format(count=f"{n:,}"))
    if n := counts.get("duplicates_merged"):
        lines.append(ngettext(
            "{count} repeated row merged into an existing definition.",
            "{count} repeated rows merged into an existing definition.", n,
        ).format(count=f"{n:,}"))
    if n := counts.get("conflicts"):
        lines.append(ngettext(
            "{count} of these signals has more than one formula.",
            "{count} of these signals have more than one formula.", n,
        ).format(count=f"{n:,}"))
    if n := counts.get("rejected"):
        lines.append(ngettext("{count} row rejected.", "{count} rows rejected.", n).format(count=f"{n:,}"))
    return lines


def describe_deletion(preview: DeletePreview) -> list[str]:
    count = len(preview.deleted)
    lines = [
        ngettext(
            "Delete {count} signal from the candidate list. Importing its source again adds it back.",
            "Delete {count} signals from the candidate list. Importing their source again adds them back.",
            count,
        ).format(count=f"{count:,}")
    ] if preview.deleted else [_("Nothing to delete.")]
    reasons: dict[str, int] = {}
    for reason in preview.refused.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    if reasons:
        lines.append(_("Kept: {reasons}.").format(reasons=", ".join(
            f"{number:,} {_(reason)}" for reason, number in sorted(reasons.items())
        )))
    return lines


def inventory_sha256(inventory: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(inventory).encode("utf-8")).hexdigest()


def _merge_versions(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """One row per version (signal and formula); a repeated version keeps the
    first row's descriptive fields. Returns the rows and how many merged."""

    merged: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for raw in rows:
        row = {name: value for name, value in raw.items() if name != "catalog_variant_id"}
        row["signal_ref"] = str(SignalRef.parse(str(row["signal_ref"])))
        variant = catalog_variant_id(row)
        if variant in merged:
            duplicates += 1
            continue
        row["catalog_variant_id"] = variant
        merged[variant] = row
    return list(merged.values()), duplicates


def _with_sources(
    inventory: Mapping[str, Any],
    rows: list[dict[str, Any]],
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The inventory with these rows and each source's version count."""

    sources = {row["source_id"]: dict(row) for row in inventory["sources"]}
    if source is not None:
        sources[source["source_id"]] = {
            "imported_utc": "", **sources.get(source["source_id"], {}), **source,
        }
    for row in sources.values():
        row["row_count"] = sum(1 for entry in rows if row["source_id"] in entry["sources"])
    return {
        **inventory,
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "sources": sorted(sources.values(), key=lambda row: row["source_id"]),
        "rows": rows,
    }


def _write(repository: ProjectRepository, document: Mapping[str, Any]) -> dict[str, Any]:
    write_inventory(repository, document)
    _write_catalog_manifest(repository, document)
    refreshed = ProjectRepository(repository.root)
    rebuild_cache(refreshed, document)
    return dict(document)


def add_manual_endpoints(
    repository: ProjectRepository,
    inventory: Mapping[str, Any],
    endpoints: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Add endpoint candidates by their CAN IDs, all or none.

    Each entry has ``tester_can_id``, ``ecu_keys``, and optionally
    ``response_can_id`` (default: tester + 8) and ``target_address`` (an ECU
    behind a gateway). No session is chosen here: verification tries each
    reviewed session until one opens, as for imported endpoints.
    """

    confirmed = confirmed_ecus(repository)
    added: list[dict[str, Any]] = []
    for number, entry in enumerate(endpoints, start=1):
        tester = canonical_can_id(entry.get("tester_can_id"))
        if tester is None:
            raise ValueError(_("endpoint {number}: enter the request CAN ID as hexadecimal, e.g. 0x7E0").format(number=number))
        response_text = entry.get("response_can_id")
        response = canonical_can_id(response_text) if response_text not in (None, "") else (
            canonical_can_id(int(tester, 16) + 8)
        )
        if response is None or response == tester:
            raise ValueError(_(
                "endpoint {number}: the response CAN ID must be an 11-bit ID other than the request ID"
            ).format(number=number))
        try:
            target = canonical_target_address(entry.get("target_address") or None)
        except ValueError as exc:
            raise ValueError(
                _("endpoint {number}: the address must be one hexadecimal byte, e.g. 0x2A").format(number=number)
            ) from exc
        keys = sorted({int(key) for key in entry.get("ecu_keys") or ()})
        if not keys:
            raise ValueError(_("endpoint {number}: enter at least one ECU key").format(number=number))
        if conflicting := [key for key in keys if key in confirmed]:
            raise ValueError(_("endpoint {number}: ECU keys already belong to a confirmed endpoint: {keys}").format(
                number=number, keys=conflicting,
            ))
        for other in added:
            if (other["tester_can_id"], other["response_can_id"], other.get("target_address")) == (
                tester, response, target,
            ) or set(other["ecu_keys"]) & set(keys):
                raise ValueError(_("endpoint {number}: repeats an endpoint or ECU key given above").format(number=number))
        added.append({
            "tester_can_id": tester,
            "response_can_id": response,
            **({} if target is None else {"target_address": target}),
            "ecu_keys": keys,
        })
    if not added:
        raise ValueError(_("enter at least one endpoint"))
    manual = list(inventory["manual_endpoints"])
    for row in added:
        manual = [
            old for old in manual
            if (old["tester_can_id"], old["response_can_id"], old.get("target_address"))
            != (row["tester_can_id"], row["response_can_id"], row.get("target_address"))
            and not set(old["ecu_keys"]) & set(row["ecu_keys"])
        ]
    manual.extend(added)
    manual.sort(key=lambda row: (row["tester_can_id"], row["response_can_id"], row.get("target_address") or ""))
    document = {**inventory, "manual_endpoints": manual}
    write_inventory(repository, document)
    return document


def _write_catalog_manifest(repository: ProjectRepository, inventory: Mapping[str, Any]) -> None:
    rows = sorted(
        (entry["row"] for entry in inventory["rows"]),
        key=lambda row: (row["signal_ref"], _canonical_json(row)),
    )
    manifest = {
        "document_type": "telemetry.catalog-manifest",
        "schema_version": 1,
        "profile_id": repository.profile_id,
        "catalog_row_count": len(rows),
        "catalog_rows_sha256": hashlib.sha256(
            (_canonical_json(rows) + "\n").encode("utf-8")
        ).hexdigest(),
        "sources": [
            {"kind": source["kind"], "label": source["label"], "row_count": source["row_count"]}
            for source in inventory["sources"]
        ],
    }
    _atomic_write(repository.paths.catalog_manifest, _encoded_json(manifest))


def _blocked(source: SourceRows, rejections: list, problem: str) -> ImportPreview:
    return ImportPreview(
        source.source_id, source.kind, source.label, (), tuple(rejections),
        {"rows": 0, "rejected": len(rejections)}, (), problem,
    )


def _did_identity(row: Mapping[str, Any]) -> tuple[int, int]:
    ref = SignalRef.parse(row["signal_ref"])
    return ref.ecu_key, ref.data_id


def _row_endpoint(row: Mapping[str, Any]) -> tuple[str, str, str | None] | None:
    """(tester, response, target address) of a candidate row; the address is
    None for normal addressing."""

    tester = canonical_can_id(row.get("tester_can_id"))
    response = canonical_can_id(
        row.get("response_can_id", row.get("ecu_response_can_id_candidate"))
    )
    if tester is None or response is None or tester == response:
        return None
    return tester, response, canonical_target_address(row.get("target_address"))


def _decode_identity(row: Mapping[str, Any]) -> str:
    return _canonical_json(row.get("formula") or "raw")


def _conflicting_signals(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    seen: dict[str, set[str]] = {}
    for row in rows:
        seen.setdefault(row["signal_ref"], set()).add(_decode_identity(row))
    return sorted((ref for ref, values in seen.items() if len(values) > 1), key=SignalRef.parse)


def session_opened(result: Mapping[str, Any], recipe: Mapping[str, Any]) -> bool:
    """Whether every session step of a probe succeeded, as the probe judges continuation."""

    steps = result.get("session_steps") or []
    if len(steps) != len(recipe["steps"]):
        return False
    return all(
        step["outcome"] == "positive" or (step["outcome"] == "negative" and spec["tolerate_negative"])
        for step, spec in zip(steps, recipe["steps"])
    )


def pair_key(tester_can_id: str, response_can_id: str, target_address: str | None = None) -> str:
    return (
        f"{tester_can_id[2:].lower()}-{response_can_id[2:].lower()}"
        f"{address_suffix(target_address)}"
    )


@dataclass
class SignalItem:
    signal_ref: str
    name: str
    unit: str
    formula: str
    variants: int
    conflict: bool
    review: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    in_inventory: bool = True
    observations: int = 0
    status: str = SIGNAL_NOT_CHECKED
    ecu_name: str = ""
    states: dict[str, str] = field(default_factory=dict)

    @property
    def ecu_key(self) -> int:
        return SignalRef.parse(self.signal_ref).ecu_key

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_ref": self.signal_ref,
            "name": self.name,
            "unit": self.unit,
            "formula": self.formula,
            "variants": self.variants,
            "conflict": self.conflict,
            "review": self.review,
            "target": self.target,
            "progress": self.progress,
            "in_inventory": self.in_inventory,
            "observations": self.observations,
            "status": self.status,
            "ecu_name": self.ecu_name,
            "states": dict(self.states),
        }


@dataclass
class DidItem:
    endpoint_key: str
    data_id: str
    signals: list[SignalItem] = field(default_factory=list)
    state: str = NOT_VERIFIED
    detail: str = ""
    baseline_hex: str | None = None
    latency_ms: int | None = None
    observed_utc: str | None = None
    settings: dict[str, Any] | None = None
    acquisition_id: str | None = None
    manual: bool = False

    @property
    def key(self) -> str:
        return f"{self.endpoint_key}/{self.data_id}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "data_id": self.data_id,
            "state": self.state,
            "detail": self.detail,
            "baseline_hex": self.baseline_hex,
            "latency_ms": self.latency_ms,
            "observed_utc": self.observed_utc,
            "settings": self.settings,
            "acquisition_id": self.acquisition_id,
            "signals": [signal.as_dict() for signal in self.signals],
        }


@dataclass
class EndpointItem:
    key: str
    tester_can_id: str | None
    response_can_id: str | None
    ecu_keys: list[int]
    ecu_names: list[str]
    state: str
    endpoint_id: str | None = None
    confirmed: bool = False
    session_recipe_id: str | None = None
    candidate_ids: list[str] = field(default_factory=list)
    dids: list[DidItem] = field(default_factory=list)
    target_address: str | None = None
    # False when the ECU answered but no probe opened its whole session yet.
    session_open: bool = True

    @property
    def label(self) -> str:
        names = ", ".join(self.ecu_names) or _("unnamed ECU")
        return f"{self.key} ({names})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "endpoint_id": self.endpoint_id,
            "confirmed": self.confirmed,
            "state": self.state,
            "tester_can_id": self.tester_can_id,
            "response_can_id": self.response_can_id,
            **({} if self.target_address is None else {"target_address": self.target_address}),
            "session_recipe_id": self.session_recipe_id,
            "ecu_keys": self.ecu_keys,
            "ecu_names": self.ecu_names,
            "candidate_ids": self.candidate_ids,
            "dids": [did.as_dict() for did in self.dids],
        }


@dataclass
class InventoryTree:
    endpoints: list[EndpointItem]

    def endpoint(self, key: str) -> EndpointItem | None:
        lowered = key.lower()
        for endpoint in self.endpoints:
            if lowered == endpoint.key or lowered == endpoint.endpoint_id or lowered in endpoint.candidate_ids:
                return endpoint
        return None

    def dids(self) -> Iterable[DidItem]:
        for endpoint in self.endpoints:
            yield from endpoint.dids

    def signals(self) -> Iterable[tuple[EndpointItem, DidItem, SignalItem]]:
        for endpoint in self.endpoints:
            for did in endpoint.dids:
                for signal in did.signals:
                    yield endpoint, did, signal

    def find_did(self, key: str) -> tuple[EndpointItem, DidItem] | None:
        endpoint_key, _slash, data_id = key.rpartition("/")
        endpoint = self.endpoint(endpoint_key)
        if endpoint is None:
            return None
        canonical = canonical_did(data_id)
        for did in endpoint.dids:
            if did.data_id == canonical:
                return endpoint, did
        return None

    def find_signal(self, signal_ref: str) -> tuple[EndpointItem, DidItem, SignalItem] | None:
        for endpoint, did, signal in self.signals():
            if signal.signal_ref == signal_ref:
                return endpoint, did, signal
        return None

    def counts(self) -> dict[str, int]:
        dids = list(self.dids())
        return {
            "endpoints": len(self.endpoints),
            "confirmed_endpoints": sum(1 for row in self.endpoints if row.confirmed),
            "dids": len(dids),
            "verified_dids": sum(1 for row in dids if row.state == VERIFIED),
            "failed_dids": sum(1 for row in dids if row.state == FAILED),
            "signals": sum(len(row.signals) for row in dids),
            "targets": sum(1 for _endpoint, _did, signal in self.signals() if signal.target is not None),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"endpoints": [endpoint.as_dict() for endpoint in self.endpoints], "counts": self.counts()}


def build_tree(
    repository: ProjectRepository,
    inventory: Mapping[str, Any],
    *,
    progress: Mapping[str, Mapping[str, Any]] | None = None,
    observations: Mapping[str, int] | None = None,
) -> InventoryTree:
    """Group every known signal under its endpoint and DID with current state."""

    confirmed = {row["id"]: row for row in repository.transport["endpoints"]}
    ecu_to_confirmed = confirmed_ecus(repository)
    manual_by_ecu = {
        key: row for row in inventory["manual_endpoints"] for key in row["ecu_keys"]
    }
    reviews = {row["signal_ref"]: row for row in repository.review["signals"]}
    targets = {row["signal_ref"]: row for row in repository.policy["targets"]}
    settings = {row["acquisition_id"]: row for row in repository.policy["acquisitions"]}
    progress = progress or {}

    rows_by_signal: dict[str, list[dict[str, Any]]] = {}
    for entry in inventory["rows"]:
        rows_by_signal.setdefault(entry["row"]["signal_ref"], []).append(entry["row"])
    for signal_ref in list(reviews) + list(targets):
        rows_by_signal.setdefault(signal_ref, [])

    endpoints: dict[str, EndpointItem] = {}
    for endpoint_id, row in sorted(confirmed.items()):
        endpoints[endpoint_id] = EndpointItem(
            key=endpoint_id,
            tester_can_id=row["tester_can_id"],
            response_can_id=row["response_can_id"],
            ecu_keys=sorted(row["ecu_keys"]),
            ecu_names=[],
            state="confirmed",
            endpoint_id=endpoint_id,
            confirmed=True,
            session_recipe_id=row["session_recipe_id"],
            target_address=row.get("target_address"),
        )
    for row in inventory["manual_endpoints"]:
        key = pair_key(row["tester_can_id"], row["response_can_id"], row.get("target_address"))
        endpoints.setdefault(key, EndpointItem(
            key=key,
            tester_can_id=row["tester_can_id"],
            response_can_id=row["response_can_id"],
            ecu_keys=list(row["ecu_keys"]),
            ecu_names=[],
            state="not verified",
            target_address=row.get("target_address"),
        ))

    def endpoint_for(ecu_key: int, rows: list[dict[str, Any]]) -> EndpointItem:
        if ecu_key in ecu_to_confirmed:
            return endpoints[ecu_to_confirmed[ecu_key]]
        manual = manual_by_ecu.get(ecu_key)
        pair = (
            (manual["tester_can_id"], manual["response_can_id"], manual.get("target_address")) if manual
            else next((pair for pair in map(_row_endpoint, rows) if pair), None)
        )
        if pair is None:
            key = f"ecu-{ecu_key}"
            return endpoints.setdefault(key, EndpointItem(
                key=key, tester_can_id=None, response_can_id=None, ecu_keys=[],
                ecu_names=[], state="address unknown",
            ))
        key = pair_key(*pair)
        return endpoints.setdefault(key, EndpointItem(
            key=key, tester_can_id=pair[0], response_can_id=pair[1], ecu_keys=[],
            ecu_names=[], state="not verified", target_address=pair[2],
        ))

    dids: dict[tuple[str, str], DidItem] = {}
    for signal_ref in sorted(rows_by_signal, key=SignalRef.parse):
        rows = rows_by_signal[signal_ref]
        ref = SignalRef.parse(signal_ref)
        endpoint = endpoint_for(ref.ecu_key, rows)
        if ref.ecu_key not in endpoint.ecu_keys:
            endpoint.ecu_keys.append(ref.ecu_key)
        for row in rows:
            name = row.get("ecu_name")
            if name and name not in endpoint.ecu_names:
                endpoint.ecu_names.append(str(name))
        data_id = f"0x{ref.data_id:04X}"
        did = dids.get((endpoint.key, data_id))
        if did is None:
            did = DidItem(endpoint.key, data_id)
            dids[(endpoint.key, data_id)] = did
            endpoint.dids.append(did)
        review = reviews.get(signal_ref)
        chosen = _chosen_row(rows, review)
        name = (review or {}).get("display_name_override") or chosen.get("name") or _("(not in candidate list)")
        unit = (review or {}).get("unit_override") or chosen.get("unit") or ""
        formula = (review or {}).get("formula_override") or chosen.get("formula") or "raw"
        did.signals.append(SignalItem(
            signal_ref=signal_ref,
            name=str(name),
            unit=str(unit),
            formula=str(formula),
            variants=len(rows),
            conflict=len({_decode_identity(row) for row in rows}) > 1,
            review=review,
            target=targets.get(signal_ref),
            progress=progress.get(signal_ref),
            in_inventory=bool(rows),
            ecu_name=str(chosen.get("ecu_name") or ""),
            states={str(raw): str(text) for raw, text in (chosen.get("states") or {}).items()},
        ))

    _apply_verification(repository, endpoints, settings)
    observations = observations or {}
    for endpoint in endpoints.values():
        for did in endpoint.dids:
            for signal in did.signals:
                signal.observations = int(observations.get(signal.signal_ref, 0))
                signal.status = _signal_status(signal, did)
    ordered = sorted(endpoints.values(), key=lambda row: (not row.confirmed, row.key))
    for endpoint in ordered:
        endpoint.ecu_keys.sort()
        endpoint.dids.sort(key=lambda row: row.data_id)
    return InventoryTree(ordered)


def _signal_status(signal: SignalItem, did: DidItem) -> str:
    review = signal.review or {}
    if review.get("evidence_state") and review["evidence_state"] != "Candidate":
        return SIGNAL_REVIEWED
    if signal.observations:
        return SIGNAL_RECORDED
    if did.state == VERIFIED:
        return SIGNAL_RESPONDS
    if did.state == FAILED:
        return SIGNAL_FAILED
    if did.state == ENDPOINT_SILENT:
        return SIGNAL_NO_ANSWER
    return SIGNAL_NOT_CHECKED


def signal_states(repository: ProjectRepository, inventory: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """State text per signal, from the catalog variant the tree shows."""

    reviews = {row["signal_ref"]: row for row in repository.review["signals"]}
    rows_by_signal: dict[str, list[dict[str, Any]]] = {}
    for entry in inventory["rows"]:
        rows_by_signal.setdefault(entry["row"]["signal_ref"], []).append(entry["row"])
    result = {}
    for signal_ref, rows in rows_by_signal.items():
        states = _chosen_row(rows, reviews.get(signal_ref)).get("states")
        if states:
            result[signal_ref] = {str(raw): str(text) for raw, text in states.items()}
    return result


def _chosen_row(rows: list[dict[str, Any]], review: Mapping[str, Any] | None) -> dict[str, Any]:
    if not rows:
        return {}
    if review and review.get("catalog_variant_id"):
        normalized = _normalized_catalog_rows(rows)
        for row in normalized:
            if row["catalog_variant_id"] == review["catalog_variant_id"]:
                return row
    return rows[0]


def _apply_verification(
    repository: ProjectRepository,
    endpoints: Mapping[str, EndpointItem],
    settings: Mapping[str, Mapping[str, Any]],
) -> None:
    discovery = repository.discovery_candidates()
    recipes = {row["id"]: row for row in repository.transport["session_recipes"]}
    preflight = {
        f"{row['endpoint_id']}/{row['data_id']}": row for row in repository.did_preflight_facts()
    }
    recorded = _recorded_did_status(repository)
    for endpoint in endpoints.values():
        if endpoint.tester_can_id and endpoint.response_can_id:
            matching = sorted(
                (
                    fact for fact in discovery
                    if (fact["tester_can_id"], fact["response_can_id"], fact.get("target_address"))
                    == (endpoint.tester_can_id, endpoint.response_can_id, endpoint.target_address)
                ),
                key=lambda fact: fact["observed_utc"],
            )
            endpoint.candidate_ids = [fact["candidate_id"] for fact in matching]
            if not endpoint.confirmed and matching:
                responded = [fact for fact in matching if fact["responded"]]
                if responded:
                    # Prefer the latest probe whose whole session opened; an
                    # ECU that answered but refused every session is probed
                    # again with the other recipes on the next verification.
                    opened = [
                        fact for fact in responded
                        if fact["session_recipe_id"] in recipes
                        and session_opened(fact["result"], recipes[fact["session_recipe_id"]])
                    ]
                    chosen = (opened or responded)[-1]
                    endpoint.state = "responded"
                    endpoint.session_open = bool(opened)
                    endpoint.endpoint_id = chosen["candidate_id"]
                    endpoint.session_recipe_id = chosen["session_recipe_id"]
                else:
                    endpoint.state = "silent"
        ids = [endpoint.endpoint_id] if endpoint.endpoint_id else []
        ids += [value for value in endpoint.candidate_ids if value not in ids]
        for did in endpoint.dids:
            if endpoint.confirmed:
                did.acquisition_id = f"{endpoint.endpoint_id}/{did.data_id}"
                did.settings = dict(settings[did.acquisition_id]) if did.acquisition_id in settings else None
            facts = [preflight[f"{value}/{did.data_id}"] for value in ids if f"{value}/{did.data_id}" in preflight]
            fact = max(facts, key=lambda row: row["observed_utc"], default=None)
            if fact is not None:
                read = fact["result"].get("read") or {}
                did.observed_utc = fact["observed_utc"]
                did.latency_ms = read.get("latency_ms")
                did.baseline_hex = read.get("response_hex")
                if fact["outcome"] == "positive":
                    did.state = VERIFIED
                    did.detail = _("responded during verification")
                else:
                    did.state = FAILED
                    did.detail = outcome_text(fact["outcome"], read.get("nrc"))
                continue
            if endpoint.confirmed and recorded.get((endpoint.endpoint_id, did.data_id)) == "supported":
                did.state = VERIFIED
                did.detail = _("responded in recorded runs")
            elif not endpoint.confirmed and endpoint.state == "silent":
                did.state = ENDPOINT_SILENT
                did.detail = _("the ECU did not answer when verified, so this DID was not read")
            elif not endpoint.confirmed and endpoint.state != "responded":
                did.state = ENDPOINT_NOT_VERIFIED
                did.detail = (
                    _(_ENDPOINT_DETAIL[endpoint.state]) if endpoint.state in _ENDPOINT_DETAIL
                    else _("endpoint is {state}").format(state=endpoint.state)
                )
            else:
                did.state = NOT_VERIFIED


# Why a DID was not read, by its unverified endpoint's state (a stored value).
_ENDPOINT_DETAIL = {
    "not verified": N_("endpoint is not verified"),
    "address unknown": N_("endpoint is address unknown"),
}


def _recorded_did_status(repository: ProjectRepository) -> dict[tuple[str, str], str]:
    database = repository.paths.database
    if not database.is_file():
        return {}
    try:
        with closing(connect_cache(database)) as connection:
            return {
                (endpoint_id, data_id): status
                for endpoint_id, data_id, status in connection.execute(
                    "SELECT endpoint_id, data_id, status FROM did_status"
                )
            }
    except sqlite3.Error:
        return {}


def progress_by_signal(repository: ProjectRepository) -> dict[str, dict[str, Any]]:
    """Read cached target progress without rebuilding; empty when unavailable."""

    database = repository.paths.database
    if not database.is_file():
        return {}
    try:
        with closing(connect_cache(database)) as connection:
            return {
                signal_ref: json.loads(document)
                for signal_ref, document in connection.execute(
                    "SELECT signal_ref, document_json FROM target_progress"
                )
            }
    except sqlite3.Error:
        return {}
