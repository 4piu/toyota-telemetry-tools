"""Candidate inventory: imported rows, manual endpoints, and the signal tree.

The inventory is ignored machine-local state (``scratch/inventory.json``). It
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

from ..identity import SignalRef, canonical_did, stable_slug
from ..repository import (
    ProjectRepository,
    _atomic_write,
    _canonical_json,
    _encoded_json,
    _exact_json,
    _normalized_catalog_rows,
)
from .sources import SourceRows, canonical_can_id


INVENTORY_FILE = "inventory.json"

VERIFIED = "Verified"
NOT_VERIFIED = "Not verified"
FAILED = "Failed"
ENDPOINT_NOT_VERIFIED = "Endpoint not verified"

# One status ladder per signal, from least to most established.
SIGNAL_NOT_CHECKED = "Not checked"
SIGNAL_FAILED = "Refused"
SIGNAL_RESPONDS = "Responds"
SIGNAL_RECORDED = "Recorded"
SIGNAL_REVIEWED = "Reviewed"
SIGNAL_STATUSES = (SIGNAL_NOT_CHECKED, SIGNAL_FAILED, SIGNAL_RESPONDS, SIGNAL_RECORDED, SIGNAL_REVIEWED)

# Review evidence states from the project method; a review in any state other
# than Candidate counts as Reviewed.
EVIDENCE_STATES = (
    "Candidate", "Observed", "Correlated", "Validated",
    "Observed and replayed", "Correlated and replayed", "Validated and replayed",
)

OUTCOME_TEXT = {
    "negative": "refused",
    "timeout": "no answer",
    "transport_error": "connection error",
    "session_unavailable": "session could not be opened",
}
# Common UDS negative response codes in plain words.
NRC_TEXT = {
    "0x10": "general reject",
    "0x11": "service not supported",
    "0x12": "sub-function not supported",
    "0x13": "wrong message length",
    "0x14": "response too long",
    "0x22": "conditions not correct",
    "0x31": "request out of range",
    "0x33": "security access required",
    "0x72": "general programming failure",
    "0x7E": "not supported in this session",
    "0x7F": "service not supported in this session",
}


def outcome_text(outcome: str, nrc: str | None = None) -> str:
    text = OUTCOME_TEXT.get(outcome, outcome)
    if nrc:
        meaning = NRC_TEXT.get(nrc.upper().replace("0X", "0x"))
        text += f": {meaning} (code {nrc})" if meaning else f" (code {nrc})"
    return text


def inventory_path(repository: ProjectRepository):
    return repository.paths.scratch / INVENTORY_FILE


def empty_inventory(project_id: str) -> dict[str, Any]:
    return {
        "document_type": "telemetry.candidate-inventory",
        "schema_version": 1,
        "project_id": project_id,
        "sources": [],
        "rows": [],
        "manual_endpoints": [],
    }


def load_inventory(repository: ProjectRepository) -> dict[str, Any]:
    path = inventory_path(repository)
    if not path.is_file():
        return empty_inventory(repository.project_id)
    document, _ = _exact_json(path)
    validate_inventory(document, repository.project_id)
    return document


def validate_inventory(document: Mapping[str, Any], project_id: str) -> None:
    expected = {
        "document_type", "schema_version", "project_id", "sources", "rows", "manual_endpoints",
    }
    if set(document) != expected:
        raise ValueError(f"candidate inventory fields mismatch: {sorted(document)}")
    if document["document_type"] != "telemetry.candidate-inventory" or document["schema_version"] != 1:
        raise ValueError("candidate inventory must be telemetry.candidate-inventory schema 1")
    if document["project_id"] != project_id:
        raise ValueError("candidate inventory belongs to a different project")
    source_ids = {source["source_id"] for source in document["sources"]}
    for entry in document["rows"]:
        if set(entry) != {"source_id", "row"} or entry["source_id"] not in source_ids:
            raise ValueError("candidate inventory row has no known source")
        SignalRef.parse(entry["row"]["signal_ref"])
    for endpoint in document["manual_endpoints"]:
        if set(endpoint) != {"tester_can_id", "response_can_id", "session_recipe_id", "ecu_keys"}:
            raise ValueError("candidate inventory manual endpoint fields mismatch")


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


@dataclass(frozen=True)
class ImportPreview:
    source_id: str
    kind: str
    label: str
    rows: tuple[dict[str, Any], ...]
    rejections: tuple[tuple[str, str], ...]
    counts: dict[str, int]
    conflicts: tuple[str, ...]
    blocking_problem: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "label": self.label,
            "counts": dict(self.counts),
            "conflicts": list(self.conflicts),
            "rejections": [
                {"where": where, "reason": reason} for where, reason in self.rejections[:50]
            ],
            "rejection_count": len(self.rejections),
            "blocking_problem": self.blocking_problem,
        }


def preview_import(
    repository: ProjectRepository, inventory: Mapping[str, Any], source: SourceRows
) -> ImportPreview:
    """Reconcile source rows against the inventory without writing anything."""

    did_min = int(repository.transport["safety"]["did_min"], 16)
    did_max = int(repository.transport["safety"]["did_max"], 16)
    accepted = []
    rejections = list(source.rejections)
    for row in source.rows:
        ref = SignalRef.parse(row["signal_ref"])
        if not did_min <= ref.data_id <= did_max:
            rejections.append((str(ref), "DID is outside the transport safety range"))
            continue
        accepted.append(row)
    try:
        normalized = _normalized_catalog_rows(accepted)
    except ValueError as exc:
        return _blocked(source, rejections, str(exc))

    previous = {
        _row_key(entry["row"]) for entry in inventory["rows"] if entry["source_id"] == source.source_id
    }
    incoming = {_row_key(row) for row in normalized}
    others = [
        {"source_id": entry["source_id"], "row": entry["row"]}
        for entry in inventory["rows"] if entry["source_id"] != source.source_id
    ]
    merged = [entry["row"] for entry in others] + list(normalized)
    conflicts = _conflicting_signals(merged)
    endpoints = {_row_endpoint(row) for row in normalized} - {None}
    counts = {
        "ecus": len({SignalRef.parse(row["signal_ref"]).ecu_key for row in normalized}),
        "endpoint_candidates": len(endpoints),
        "dids": len({_did_identity(row) for row in normalized}),
        "signals": len({row["signal_ref"] for row in normalized}),
        "rows": len(normalized),
        "added": len(incoming - previous),
        "removed": len(previous - incoming),
        "unchanged": len(incoming & previous),
        "conflicts": len(conflicts),
        "rejected": len(rejections),
    }
    problem = None
    candidate = {**inventory, "rows": others + [
        {"source_id": source.source_id, "row": row} for row in normalized
    ]}
    try:
        repository._validate_catalog_rows(cache_rows(repository, candidate))
    except ValueError as exc:
        problem = f"reviewed signals would no longer resolve: {exc}"
    return ImportPreview(
        source.source_id, source.kind, source.label, tuple(normalized), tuple(rejections),
        counts, tuple(conflicts), problem,
    )


def apply_import(
    repository: ProjectRepository, inventory: Mapping[str, Any], preview: ImportPreview
) -> dict[str, Any]:
    """Replace this source's rows, update provenance, and rebuild the cache."""

    if preview.blocking_problem:
        raise ValueError(preview.blocking_problem)
    sources = [row for row in inventory["sources"] if row["source_id"] != preview.source_id]
    sources.append({
        "source_id": preview.source_id,
        "kind": preview.kind,
        "label": preview.label,
        "imported_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "row_count": len(preview.rows),
    })
    rows = [entry for entry in inventory["rows"] if entry["source_id"] != preview.source_id]
    rows.extend({"source_id": preview.source_id, "row": row} for row in preview.rows)
    rows.sort(key=lambda entry: (SignalRef.parse(entry["row"]["signal_ref"]), entry["source_id"]))
    document = {
        **inventory,
        "sources": sorted(sources, key=lambda row: row["source_id"]),
        "rows": rows,
    }
    write_inventory(repository, document)
    _write_catalog_manifest(repository, document)
    refreshed = ProjectRepository(repository.root)
    rebuild_cache(refreshed, document)
    return document


def add_manual_endpoint(
    repository: ProjectRepository,
    inventory: Mapping[str, Any],
    tester_can_id: str,
    response_can_id: str,
    session_recipe_id: str,
    ecu_keys: Iterable[int],
) -> dict[str, Any]:
    tester = canonical_can_id(tester_can_id)
    response = canonical_can_id(response_can_id)
    if tester is None or response is None or tester == response:
        raise ValueError("endpoint needs two different 11-bit CAN IDs such as 0x7E0 and 0x7E8")
    recipes = {row["id"] for row in repository.transport["session_recipes"]}
    if session_recipe_id not in recipes:
        raise ValueError(
            f"unknown session recipe {session_recipe_id!r}; reviewed recipes: {', '.join(sorted(recipes))}"
        )
    keys = sorted({int(key) for key in ecu_keys})
    if not keys:
        raise ValueError("a manual endpoint needs at least one ECU key")
    confirmed = confirmed_ecus(repository)
    if conflicting := [key for key in keys if key in confirmed]:
        raise ValueError(f"ECU keys already belong to a confirmed endpoint: {conflicting}")
    manual = [
        row for row in inventory["manual_endpoints"]
        if (row["tester_can_id"], row["response_can_id"]) != (tester, response)
        and not set(row["ecu_keys"]) & set(keys)
    ]
    manual.append({
        "tester_can_id": tester,
        "response_can_id": response,
        "session_recipe_id": stable_slug(session_recipe_id, "session recipe id"),
        "ecu_keys": keys,
    })
    manual.sort(key=lambda row: (row["tester_can_id"], row["response_can_id"]))
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


def _row_key(row: Mapping[str, Any]) -> str:
    return _canonical_json(row)


def _did_identity(row: Mapping[str, Any]) -> tuple[int, int]:
    ref = SignalRef.parse(row["signal_ref"])
    return ref.ecu_key, ref.data_id


def _row_endpoint(row: Mapping[str, Any]) -> tuple[str, str] | None:
    tester = canonical_can_id(row.get("tester_can_id"))
    response = canonical_can_id(
        row.get("response_can_id", row.get("ecu_response_can_id_candidate"))
    )
    if tester is None or response is None or tester == response:
        return None
    return tester, response


def _decode_identity(row: Mapping[str, Any]) -> str:
    return _canonical_json([row.get("name"), row.get("formula"), row.get("unit")])


def _conflicting_signals(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    seen: dict[str, set[str]] = {}
    for row in rows:
        seen.setdefault(row["signal_ref"], set()).add(_decode_identity(row))
    return sorted((ref for ref, values in seen.items() if len(values) > 1), key=SignalRef.parse)


def pair_key(tester_can_id: str, response_can_id: str) -> str:
    return f"{tester_can_id[2:].lower()}-{response_can_id[2:].lower()}"


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

    @property
    def label(self) -> str:
        names = ", ".join(self.ecu_names) or "unnamed ECU"
        return f"{self.key} ({names})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "endpoint_id": self.endpoint_id,
            "confirmed": self.confirmed,
            "state": self.state,
            "tester_can_id": self.tester_can_id,
            "response_can_id": self.response_can_id,
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
        endpoint_key, _, data_id = key.rpartition("/")
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
            "targets": sum(1 for _, _, signal in self.signals() if signal.target is not None),
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
        )
    for row in inventory["manual_endpoints"]:
        key = pair_key(row["tester_can_id"], row["response_can_id"])
        endpoints.setdefault(key, EndpointItem(
            key=key,
            tester_can_id=row["tester_can_id"],
            response_can_id=row["response_can_id"],
            ecu_keys=list(row["ecu_keys"]),
            ecu_names=[],
            state="not verified",
            session_recipe_id=row["session_recipe_id"],
        ))

    def endpoint_for(ecu_key: int, rows: list[dict[str, Any]]) -> EndpointItem:
        if ecu_key in ecu_to_confirmed:
            return endpoints[ecu_to_confirmed[ecu_key]]
        manual = manual_by_ecu.get(ecu_key)
        pair = (
            (manual["tester_can_id"], manual["response_can_id"]) if manual
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
            ecu_names=[], state="not verified",
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
        name = (review or {}).get("display_name_override") or chosen.get("name") or "(not in candidate list)"
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
    return SIGNAL_NOT_CHECKED


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
    preflight = {
        f"{row['endpoint_id']}/{row['data_id']}": row for row in repository.did_preflight_facts()
    }
    recorded = _recorded_did_status(repository)
    for endpoint in endpoints.values():
        if endpoint.tester_can_id and endpoint.response_can_id:
            matching = sorted(
                (
                    fact for fact in discovery
                    if (fact["tester_can_id"], fact["response_can_id"])
                    == (endpoint.tester_can_id, endpoint.response_can_id)
                ),
                key=lambda fact: fact["observed_utc"],
            )
            endpoint.candidate_ids = [fact["candidate_id"] for fact in matching]
            if not endpoint.confirmed and matching:
                responded = [fact for fact in matching if fact["responded"]]
                if responded:
                    endpoint.state = "responded"
                    endpoint.endpoint_id = responded[-1]["candidate_id"]
                    endpoint.session_recipe_id = responded[-1]["session_recipe_id"]
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
                    did.detail = "responded during verification"
                else:
                    did.state = FAILED
                    did.detail = outcome_text(fact["outcome"], read.get("nrc"))
                continue
            if endpoint.confirmed and recorded.get((endpoint.endpoint_id, did.data_id)) == "supported":
                did.state = VERIFIED
                did.detail = "responded in recorded runs"
            elif not endpoint.confirmed and endpoint.state != "responded":
                did.state = ENDPOINT_NOT_VERIFIED
                did.detail = f"endpoint is {endpoint.state}"
            else:
                did.state = NOT_VERIFIED


def _recorded_did_status(repository: ProjectRepository) -> dict[tuple[str, str], str]:
    database = repository.paths.database
    if not database.is_file():
        return {}
    try:
        with closing(sqlite3.connect(database)) as connection:
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
        with closing(sqlite3.connect(database)) as connection:
            return {
                signal_ref: json.loads(document)
                for signal_ref, document in connection.execute(
                    "SELECT signal_ref, document_json FROM target_progress"
                )
            }
    except sqlite3.Error:
        return {}
