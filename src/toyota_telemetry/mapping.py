"""Emit a complete signal map from the catalog, annotated with observations.

The catalog is the row set: every signal the OEM database claims for an ECU
appears, whether or not a capture has exercised it.  Observations from
completed captures are merged in, and a small hand-written curation file
supplies reviewed evidence states and notes.

Two things this deliberately will not do.  It never raises an evidence state on
its own - an observed signal reaches "Observed and replayed" and no further,
because Correlated and Validated are review conclusions, not measurements.  And
it refuses to emit a curated state the observations do not support, so a
generated map is always defensible against the captures behind it.

DIDs the vehicle refused are written to a separate file: "not present on this
vehicle" is a finding, and another vehicle of the same platform may differ.  A
DID nobody has asked about yet appears in neither file - that is unfinished
work rather than a property of the vehicle, and it is reported as a note.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import __version__
from .console import (
    LOG,
    add_output_arguments,
    configure,
    emit,
    fields,
    heading,
    table,
)
from .capture import classify_nrc
from .planning import ecu_views, validate_config
from .progress import discover_progress
from .project import load_json, project_root, resolve_path
from .review import build_ledger, load_catalog_rows


# Deliberately excludes observed minima, maxima, and value counts.  Those are
# captured readings - a key-cycle count, a state of charge - and republishing
# them puts vehicle state into a document whose purpose is definitions.  They
# stay available locally through `telemetry-review --csv-out`, alongside the
# captures needed to interpret them.
#
# `observation` survives because it is a categorical caveat rather than a
# reading: a consumer needs to know a field never moved, or is absent from this
# calibration, before trusting its conversion.
SIGNAL_FIELDS = [
    "ecu_key", "ecu_name", "tester_can_id", "response_can_id",
    "data_id", "read_request", "internal_id", "name", "first_bit", "last_bit",
    "conversion", "unit",
    "evidence_state", "review_status", "observation",
    "runs", "samples", "notes",
]

UNSUPPORTED_FIELDS = [
    "ecu_key", "ecu_name", "data_id", "read_request",
    "result", "negative_response_code", "classification", "attempts",
    "catalog_signal_count", "catalog_signal_names",
]

CURATION_FIELDS = ["data_id", "first_bit", "last_bit", "name", "evidence_state", "notes"]

# States that assert the field was seen to move. Emitting one of these for a
# signal that never changed is the mistake this tool refuses to make.
EXCITATION_WORDS = ("correlated", "validated")


def curation_key(
    ecu_key: Any, data_id: str, first_bit: Any, last_bit: Any
) -> tuple[int, str, int, int]:
    return (int(ecu_key), f"0x{int(str(data_id), 16):04X}", int(first_bit), int(last_bit))


def load_curation(path: Path | None) -> dict[tuple[int, str, int, int], dict[str, str]]:
    """Hand-written evidence, keyed by ECU as well as by identifier and bits.

    The same DID and the same bit range mean different things on different
    ECUs, so a curation file without an ``ecu_key`` column cannot be applied
    to a map that spans more than one; it is rejected rather than guessed at.
    """
    if path is None or not path.is_file():
        return {}
    result: dict[tuple[int, str, int, int], dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is not None and "ecu_key" not in reader.fieldnames:
            raise ValueError(
                f"{path} has no ecu_key column. A DID and bit range identify a "
                "signal only within one ECU; add ecu_key to every row."
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                key = curation_key(
                    row["ecu_key"], row["data_id"], row["first_bit"], row["last_bit"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number} is not a valid curation row") from exc
            if key in result:
                raise ValueError(
                    f"{path}:{line_number} duplicates ECU {key[0]} {key[1]} "
                    f"[{key[2]}..{key[3]}]")
            result[key] = row
    return result


def build_for_view(
    root: Path, view: dict[str, Any],
    curation: dict[tuple[int, str, int, int], dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[str]]:
    config = view

    ecu_key = int(config["endpoint"]["ecu_key"])
    profile_id = str(config["profile_id"])
    tester = str(config["endpoint"]["tester_can_id"])
    response = str(config["endpoint"]["response_can_id"])
    catalog_path = resolve_path(root, config["catalog"]["path"])
    capture_root = resolve_path(root, config["paths"]["capture_root"])
    overrides_value = config.get("analysis", {}).get("decode_overrides")
    decode_overrides = resolve_path(root, overrides_value) if overrides_value else None
    maximum_dids = int(config["selection"].get("maximum_dids_per_road_batch", 6))

    catalog_rows = load_catalog_rows(catalog_path, ecu_key, decode_overrides)
    if not catalog_rows:
        raise ValueError(f"no catalog rows for ECU key {ecu_key} in {catalog_path}")

    observed, stats = build_ledger(capture_root, catalog_rows, profile_id, maximum_dids)
    by_bits = {}
    for record in observed.values():
        by_bits.setdefault((record.did, record.first_bit, record.last_bit), record)

    progress = discover_progress(capture_root, profile_id)
    response_bytes = stats.get("did_response_bytes", {})

    # A DID the planner deliberately withheld is not the same as one the
    # vehicle refused, and neither is the same as one nobody reached yet.
    excluded: dict[str, str] = {}
    plan_value = config.get("paths", {}).get("plan")
    if plan_value:
        plan_path = resolve_path(root, plan_value)
        if plan_path.is_file():
            try:
                for item in load_json(plan_path).get("excluded", []):
                    did = item.get("data_id")
                    reason = item.get("excluded_reason")
                    if isinstance(did, str) and isinstance(reason, str):
                        excluded[did] = reason
            except (OSError, ValueError):
                pass

    ecu_name = ""
    for rows in catalog_rows.values():
        if rows:
            ecu_name = rows[0].get("ecu_name", "") or ""
            break

    signal_rows: list[dict[str, Any]] = []
    unsupported_rows: list[dict[str, Any]] = []
    violations: list[str] = []
    unqualified: list[str] = []
    counts: dict[str, int] = defaultdict(int)

    for did in sorted(catalog_rows, key=lambda value: int(value, 16)):
        rows = catalog_rows[did]
        supported = did in progress.qualified

        if not supported:
            nrcs = progress.negative_codes.get(did, [])
            attempts = progress.qualification_attempts.get(did, 0)
            names = [r.get("name", "") for r in rows if r.get("name")]
            if nrcs:
                result = "refused"
                classification = classify_nrc(int(nrcs[-1], 16))
            elif did in excluded:
                result = "withheld"
                classification = excluded[did]
            elif attempts:
                result = "no_response"
                classification = ""
            else:
                # Never asked. That is unfinished work, not a property of the
                # vehicle, and publishing it in a file about what this vehicle
                # does not support would assert something no capture supports.
                counts["unqualified_dids"] += 1
                unqualified.append(did)
                continue
            unsupported_rows.append({
                "ecu_key": ecu_key,
                "ecu_name": ecu_name,
                "data_id": did,
                "read_request": rows[0].get("read_request", "") if rows else "",
                "result": result,
                "negative_response_code": nrcs[-1] if nrcs else "",
                "classification": classification,
                "attempts": attempts,
                "catalog_signal_count": len(names),
                "catalog_signal_names": "; ".join(dict.fromkeys(names)),
            })
            counts[f"unsupported_{result}"] += 1
            counts["unsupported_dids"] += 1
            continue

        counts["supported_dids"] += 1
        available_bits = response_bytes.get(did, 0) * 8
        for row in rows:
            try:
                first_bit = int(row["first_bit"])
                last_bit = int(row["last_bit"])
            except (KeyError, TypeError, ValueError):
                continue
            record = by_bits.get((did, first_bit, last_bit))
            curated = curation.get((ecu_key, did, first_bit, last_bit))

            if record is not None and record.samples:
                observation = record.status()
                derived_state = "Observed and replayed"
            elif available_bits and last_bit >= available_bits:
                # The catalog claims a field past the end of the reply this
                # vehicle actually returns; it is absent from this calibration.
                observation = "beyond_response"
                derived_state = "Candidate"
            else:
                observation = "unobserved"
                derived_state = "Candidate"

            state = derived_state
            review_status = "auto"
            notes = ""
            if curated:
                state = (curated.get("evidence_state") or derived_state).strip()
                notes = (curated.get("notes") or "").strip()
                review_status = "reviewed"
                if any(word in state.lower() for word in EXCITATION_WORDS):
                    if observation in ("constant", "unobserved", "beyond_response"):
                        violations.append(
                            f"{did} [{first_bit}..{last_bit}] {row.get('name','')}: "
                            f"curation claims '{state}' but observation is '{observation}'"
                        )
            counts[f"signal_{observation}"] += 1
            counts["signals"] += 1

            signal_rows.append({
                "ecu_key": ecu_key,
                "ecu_name": ecu_name,
                "tester_can_id": tester,
                "response_can_id": response,
                "data_id": did,
                "read_request": row.get("read_request", ""),
                # Several catalog rows can share a DID, bit range, and name while
                # differing only in conversion - a unit selector elsewhere in the
                # response picks between them. The internal id is what tells them
                # apart and what cross-references back into the database.
                "internal_id": row.get("internal_id", ""),
                "name": row.get("name", ""),
                "first_bit": first_bit,
                "last_bit": last_bit,
                "conversion": row.get("formula") or "raw",
                "unit": row.get("unit", ""),
                "evidence_state": state,
                "review_status": review_status,
                "observation": observation,
                "runs": record.runs if record else 0,
                "samples": record.samples if record else 0,
                "notes": notes,
            })

    summary = {
        "ecu_key": ecu_key,
        "ecu_name": ecu_name,
        "profile_id": profile_id,
        "endpoint": f"{tester} -> {response}",
        "catalog_dids": len(catalog_rows),
        "captures_read": stats["captures_read"],
        **{k: v for k, v in sorted(counts.items())},
        "unqualified_sample": sorted(unqualified)[:10],
    }
    return signal_rows, unsupported_rows, summary, violations


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def render_map(document: dict[str, Any]) -> str:
    """What was built, per ECU, and what is still missing."""
    rows = []
    for summary in document["ecus"]:
        rows.append([
            summary["ecu_key"], summary["ecu_name"][:26], summary["endpoint"],
            summary["catalog_dids"], summary.get("supported_dids", 0),
            summary.get("unsupported_dids", 0), summary.get("signals", 0),
            summary.get("unqualified_dids", 0),
        ])
    return (
        heading("Signal map")
        + "\n" + fields([
            ("curation rows", document["curation_rows"]),
            ("signal rows", document["signal_rows"]),
            ("unsupported DIDs", document["unsupported_dids"]),
        ])
        + "\n\n" + table(
            rows,
            ["ecu", "name", "endpoint", "catalog", "supported",
             "unsupported", "signals", "untried"],
            ["<", "<", "<", ">", ">", ">", ">", ">"],
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--ecu", type=int, action="append",
        help="map only these ECU keys; default is every ECU in the profile",
    )
    parser.add_argument("--project-root", type=Path)
    parser.add_argument(
        "--curation", type=Path,
        help="hand-written evidence states and notes (default: data/config/curation.csv)",
    )
    parser.add_argument("--signals-out", type=Path, default=Path("data/map/signals.csv"))
    parser.add_argument(
        "--unsupported-out", type=Path, default=Path("data/map/unsupported.csv"))
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be written and stop")
    add_output_arguments(parser)
    args = parser.parse_args()
    configure(args)

    try:
        root = project_root(args.project_root, args.config)
        curation_path = resolve_path(
            root, args.curation if args.curation else Path("data/config/curation.csv"))
        curation = load_curation(curation_path)

        all_signals: list[dict[str, Any]] = []
        all_unsupported: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        violations: list[str] = []
        config = load_json(resolve_path(root, args.config))
        validate_config(config)
        for view in ecu_views(config, args.ecu):
            signals, unsupported, summary, bad = build_for_view(root, view, curation)
            all_signals.extend(signals)
            all_unsupported.extend(unsupported)
            summaries.append(summary)
            violations.extend(bad)

        document = {
            "schema_version": 1,
            "toolkit_version": __version__,
            "curation_rows": len(curation),
            "signal_rows": len(all_signals),
            "unsupported_dids": len(all_unsupported),
            "ecus": summaries,
        }
        emit(document, render_map(document), args.as_json)

        if violations:
            LOG.error(
                "%d curated row(s) claim more than the captures show:", len(violations))
            for item in violations:
                LOG.error("  %s", item)
            LOG.error("Nothing was written.")
            return 1

        pending = sum(int(s.get("unqualified_dids", 0)) for s in summaries)
        if pending:
            LOG.info(
                "%d planned DID(s) have never been qualified, so nothing is known "
                "about them either way. They are absent from both files until asked: "
                "telemetry-validate qualify --config <profile> --live", pending)

        matched = sum(1 for row in all_signals if row["review_status"] == "reviewed")
        if matched != len(curation):
            LOG.warning(
                "%d curation row(s) matched no catalog signal; check their data_id "
                "and bit range.", len(curation) - matched)

        if args.dry_run:
            LOG.info("Dry run: no files written.")
            return 0

        write_csv(resolve_path(root, args.signals_out), SIGNAL_FIELDS, all_signals)
        write_csv(resolve_path(root, args.unsupported_out), UNSUPPORTED_FIELDS, all_unsupported)
        LOG.info("Wrote %s and %s.", args.signals_out, args.unsupported_out)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
