"""Aggregate every completed capture into one per-signal evidence ledger.

``telemetry-analyze-capture`` answers "was this one capture sound?".  This
answers "what do I still not know?" - which is the question that decides what
to schedule next, and the one that is unanswerable by opening summaries one at
a time once a catalog runs to hundreds of DIDs.

The ledger is a review aid.  It never edits a curated signal map and never
promotes an evidence state; ``--check`` only reports where a curated claim is
not backed by observation.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
import json
from pathlib import Path
from typing import Any, Iterable

from .capture import (
    evaluate_formula,
    extract_raw,
    load_decode_overrides,
    read_jsonl,
    validate_candidate_capture,
)
from .planning import ecu_views, validate_config
from .console import (
    LOG,
    add_output_arguments,
    configure,
    emit,
    fields,
    heading,
    table,
)
from .project import atomic_json, load_json, project_root, resolve_path


# Beyond this many distinct raw values a signal is plainly continuous; keeping
# every value would cost memory without changing any conclusion.
DISTINCT_VALUE_CAP = 4096

# Evidence states that assert the signal was seen to move.  Claiming one of
# these for a field that never changed in any capture is the specific mistake
# --check exists to catch.
EXCITATION_STATES = ("correlated", "validated")


class SignalRecord:
    """Accumulated observations for one catalog signal across every capture."""

    __slots__ = (
        "did", "name", "first_bit", "last_bit", "unit", "formula",
        "formula_source", "samples", "runs", "batch_ids", "experiment_ids",
        "raw_min", "raw_max", "distinct", "distinct_overflow",
        "value_min", "value_max", "decode_failed",
    )

    def __init__(self, did: str, row: dict[str, str]) -> None:
        self.did = did
        self.name = row.get("name", "")
        self.first_bit = int(row["first_bit"])
        self.last_bit = int(row["last_bit"])
        self.unit = row.get("unit", "")
        self.formula = row.get("formula") or "raw"
        self.formula_source = row.get("formula_source", "catalog")
        self.samples = 0
        self.runs = 0
        self.batch_ids: set[str] = set()
        self.experiment_ids: set[str] = set()
        self.raw_min: int | None = None
        self.raw_max: int | None = None
        self.distinct: set[int] = set()
        self.distinct_overflow = False
        self.value_min: float | None = None
        self.value_max: float | None = None
        self.decode_failed = False

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (self.did, self.name, self.first_bit, self.last_bit)

    def observe(self, raw: int) -> None:
        self.samples += 1
        if self.raw_min is None or raw < self.raw_min:
            self.raw_min = raw
        if self.raw_max is None or raw > self.raw_max:
            self.raw_max = raw
        if not self.distinct_overflow:
            self.distinct.add(raw)
            if len(self.distinct) > DISTINCT_VALUE_CAP:
                self.distinct_overflow = True
                self.distinct.clear()

    def decode(self) -> None:
        """Evaluate the formula at the observed extremes only.  A monotonic
        conversion maps them to the value range; a non-monotonic one would not,
        which is why raw extremes are reported alongside and never replaced."""
        if self.raw_min is None or self.raw_max is None:
            return
        try:
            low = evaluate_formula(self.formula, self.raw_min)
            high = evaluate_formula(self.formula, self.raw_max)
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
            self.decode_failed = True
            return
        self.value_min = min(low, high)
        self.value_max = max(low, high)

    @property
    def constant(self) -> bool:
        return not self.distinct_overflow and len(self.distinct) <= 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "did": self.did,
            "name": self.name,
            "bits": f"{self.first_bit}..{self.last_bit}",
            "first_bit": self.first_bit,
            "last_bit": self.last_bit,
            "unit": self.unit,
            "formula": self.formula,
            "formula_source": self.formula_source,
            "runs": self.runs,
            "samples": self.samples,
            "raw_min": self.raw_min,
            "raw_max": self.raw_max,
            "distinct_raw_values": (
                f">{DISTINCT_VALUE_CAP}" if self.distinct_overflow else len(self.distinct)
            ),
            "value_min": self.value_min,
            "value_max": self.value_max,
            "decode_failed": self.decode_failed,
            "constant_in_every_run": self.constant,
            "batch_ids": sorted(self.batch_ids),
            "experiment_ids": sorted(self.experiment_ids),
            "status": self.status(),
        }

    def status(self) -> str:
        if self.samples == 0:
            return "unobserved"
        if self.constant:
            return "constant"
        if self.runs < 2:
            return "single_run"
        return "varied"


def load_catalog_rows(
    catalog_path: Path, ecu_key: int, decode_overrides: Path | None
) -> dict[str, list[dict[str, str]]]:
    """Every decodable catalog row for one ECU, with reviewed overrides applied.

    Unlike ``capture.load_catalog`` this is not filtered to a DID subset: the
    ledger spans all captures, so the whole ECU is in scope and the CSV should
    be read exactly once.
    """
    rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not catalog_path.is_file():
        return rows
    overrides = load_decode_overrides(decode_overrides, ecu_key)
    with catalog_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for source in csv.DictReader(handle):
            if source.get("ecu_key") != str(ecu_key):
                continue
            if not source.get("first_bit") or not source.get("last_bit"):
                continue
            try:
                did = f"0x{int(source.get('data_id', ''), 16):04X}"
            except ValueError:
                continue
            row = dict(source)
            key = (did, row.get("name", ""), row["first_bit"], row["last_bit"])
            override = overrides.get(key)
            if override:
                row["formula"] = str(override["formula"])
                row["unit"] = str(override.get("unit", row.get("unit", "")))
                row["formula_source"] = "reviewed_override"
            else:
                row["formula_source"] = "catalog"
            rows[did].append(row)
    return rows


def iter_captures(capture_root: Path) -> Iterable[Path]:
    if not capture_root.exists():
        return []
    return sorted(capture_root.rglob("*.complete.jsonl"))


def build_ledger(
    capture_root: Path,
    catalog_rows: dict[str, list[dict[str, str]]],
    profile_id: str,
    maximum_dids: int,
) -> tuple[dict[tuple[str, str, int, int], SignalRecord], dict[str, Any]]:
    signals: dict[tuple[str, str, int, int], SignalRecord] = {}
    stats = {
        "captures_read": 0,
        "captures_skipped": 0,
        "skipped_files": [],
        "qualification_runs": 0,
        "batch_runs": 0,
        "experiment_runs": 0,
        "dids_observed": set(),
        # Longest response payload seen per DID. A catalog row whose bit range
        # runs past this is absent from the reply this vehicle actually sends,
        # which is a finding rather than a decode failure.
        "did_response_bytes": {},
    }

    for path in iter_captures(capture_root):
        try:
            records = read_jsonl(path)
            start, samples = validate_candidate_capture(
                records, 0, maximum_dids, frozenset({"completed", "user_stop"})
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            stats["captures_skipped"] += 1
            stats["skipped_files"].append(str(path))
            continue
        if start.get("profile_id") != profile_id:
            continue

        stats["captures_read"] += 1
        batch_id = start.get("batch_id") or ""
        is_experiment = isinstance(batch_id, str) and batch_id.startswith("experiment-")
        if start.get("mode") == "candidate_qualification":
            stats["qualification_runs"] += 1
        elif is_experiment:
            stats["experiment_runs"] += 1
        else:
            stats["batch_runs"] += 1

        # Qualification proves the DID answers; it carries one sample and tells
        # us nothing about whether a field moves.  Only its support result is
        # interesting, and progress already tracks that.  Its response length is
        # worth keeping even so: for a DID never road-captured, this is the only
        # evidence of how long a reply the vehicle actually sends.
        if start.get("mode") == "candidate_qualification":
            for sample in samples:
                if sample.get("supported") is not True:
                    continue
                did = sample.get("did")
                if not isinstance(did, str):
                    continue
                try:
                    length = len(bytes.fromhex(sample["response_hex"])) - 3
                except (KeyError, TypeError, ValueError):
                    continue
                if length > stats["did_response_bytes"].get(did, 0):
                    stats["did_response_bytes"][did] = length
            continue

        seen_here: set[tuple[str, str, int, int]] = set()
        for sample in samples:
            if sample.get("supported") is not True:
                continue
            did = sample.get("did")
            if not isinstance(did, str):
                continue
            stats["dids_observed"].add(did)
            try:
                data = bytes.fromhex(sample["response_hex"])[3:]
            except (KeyError, TypeError, ValueError):
                continue
            if len(data) > stats["did_response_bytes"].get(did, 0):
                stats["did_response_bytes"][did] = len(data)
            for row in catalog_rows.get(did, []):
                try:
                    raw = extract_raw(data, int(row["first_bit"]), int(row["last_bit"]))
                except (ValueError, KeyError):
                    continue
                key = (did, row.get("name", ""), int(row["first_bit"]), int(row["last_bit"]))
                record = signals.get(key)
                if record is None:
                    record = SignalRecord(did, row)
                    signals[key] = record
                record.observe(raw)
                if key not in seen_here:
                    seen_here.add(key)
                    record.runs += 1
                    if is_experiment:
                        record.experiment_ids.add(batch_id)
                    else:
                        record.batch_ids.add(batch_id)

    for record in signals.values():
        record.decode()
    stats["dids_observed"] = len(stats["dids_observed"])
    return signals, stats


def check_map(
    map_path: Path, signals: dict[tuple[str, str, int, int], SignalRecord]
) -> list[dict[str, str]]:
    """Report curated rows whose claim outruns the observations.

    Rows are matched on identifier and bit range, not on name: a curated map is
    expected to use clearer names than the OEM catalog ("Generator Revolution
    (MG1)" for "Generator Revolution"), and a rename must not read as missing
    evidence.  A differing name is reported separately, as a note.
    """
    by_bits: dict[tuple[str, int, int], SignalRecord] = {}
    for record in signals.values():
        by_bits.setdefault((record.did, record.first_bit, record.last_bit), record)

    findings: list[dict[str, str]] = []
    with map_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            try:
                did = f"0x{int(row['data_id'], 16):04X}"
                first_bit = int(row["first_bit"])
                last_bit = int(row["last_bit"])
            except (KeyError, TypeError, ValueError):
                findings.append({
                    "line": str(line_number),
                    "signal": row.get("name", "<unreadable>"),
                    "severity": "error",
                    "issue": "row could not be parsed",
                })
                continue
            name = row.get("name", "")
            state = (row.get("evidence_state") or "").strip()
            # "Candidate" asserts nothing beyond "the database mentions this",
            # so there is no observation for it to outrun. Generated maps carry
            # such rows deliberately, for fields absent from this vehicle.
            if state.lower().startswith("candidate"):
                continue
            record = by_bits.get((did, first_bit, last_bit))
            if record is None or record.samples == 0:
                findings.append({
                    "line": str(line_number),
                    "signal": f"{did} [{first_bit}..{last_bit}] {name}",
                    "severity": "error",
                    "issue": f"claims '{state}' but no capture observed this bit range",
                })
                continue
            if record.constant and any(
                word in state.lower() for word in EXCITATION_STATES
            ):
                findings.append({
                    "line": str(line_number),
                    "signal": f"{did} [{first_bit}..{last_bit}] {name}",
                    "severity": "error",
                    "issue": (
                        f"claims '{state}' but the field never changed across "
                        f"{record.runs} run(s) / {record.samples} sample(s)"
                    ),
                })
                continue
            if record.name and name and record.name != name:
                findings.append({
                    "line": str(line_number),
                    "signal": f"{did} [{first_bit}..{last_bit}] {name}",
                    "severity": "note",
                    "issue": f"catalog calls this '{record.name}'",
                })
    return findings


def render_summary(summary: dict[str, Any]) -> str:
    """The counts a person actually reads, not the whole document."""
    by_status = summary.get("by_status", {})
    out = heading(f"Evidence ledger - ECU {summary['ecu_key']}")
    out += "\n" + fields([
        ("captures read", summary["captures_read"]),
        ("qualification runs", summary["qualification_runs"]),
        ("batch runs", summary["batch_runs"]),
        ("experiment runs", summary["experiment_runs"]),
        ("DIDs observed", summary["dids_observed"]),
        ("signals observed", summary["signals_observed"]),
    ])
    if by_status:
        out += "\n\n" + table(
            [[k, v] for k, v in sorted(by_status.items(), key=lambda i: -i[1])],
            ["status", "signals"], ["<", ">"],
        )
    if summary.get("captures_skipped"):
        out += f"\n\n  {summary['captures_skipped']} capture(s) skipped as unreadable."
    return out


def write_csv(path: Path, records: list[SignalRecord]) -> None:
    fields = [
        "did", "name", "bits", "unit", "formula", "formula_source", "status",
        "runs", "samples", "raw_min", "raw_max", "distinct_raw_values",
        "value_min", "value_max",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            item = record.to_dict()
            writer.writerow({field: item.get(field, "") for field in fields})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ecu", type=int, action="append", help="which ECU to report on")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--capture-root", type=Path)
    parser.add_argument(
        "--json-out", type=Path, dest="json_path", help="write the full ledger")
    parser.add_argument(
        "--csv-out", type=Path, dest="csv_path", help="write a flat signal table")
    parser.add_argument(
        "--pending",
        action="store_true",
        help="list only signals that still need excitation or a second run",
    )
    parser.add_argument(
        "--check",
        type=Path,
        help="validate a curated signal map against the observations",
    )
    parser.add_argument("--limit", type=int, default=40, help="rows printed to the console")
    add_output_arguments(parser)
    args = parser.parse_args()
    configure(args)

    try:
        root = project_root(args.project_root, args.config)
        profile = load_json(resolve_path(root, args.config))
        validate_config(profile)
        views = ecu_views(profile, args.ecu)
        if len(views) > 1:
            raise ValueError(
                "the ledger covers one ECU at a time; select it with --ecu "
                f"({', '.join(str(v['endpoint']['ecu_key']) for v in views)})"
            )
        config = views[0]

        ecu_key = int(config["endpoint"]["ecu_key"])
        profile_id = str(config["profile_id"])
        catalog_path = resolve_path(root, config["catalog"]["path"])
        capture_root = resolve_path(
            root, args.capture_root or config["paths"]["capture_root"]
        )
        overrides_value = config.get("analysis", {}).get("decode_overrides")
        decode_overrides = resolve_path(root, overrides_value) if overrides_value else None
        maximum_dids = int(
            config["selection"].get("maximum_dids_per_road_batch", 6)
        )

        catalog_rows = load_catalog_rows(catalog_path, ecu_key, decode_overrides)
        if not catalog_rows:
            LOG.error("no catalog rows for ECU key %s in %s", ecu_key, catalog_path)
            return 1

        signals, stats = build_ledger(capture_root, catalog_rows, profile_id, maximum_dids)
        records = sorted(signals.values(), key=lambda item: (item.did, item.first_bit, item.name))

        by_status: dict[str, int] = defaultdict(int)
        for record in records:
            by_status[record.status()] += 1

        summary = {
            "schema_version": 1,
            "profile_id": profile_id,
            "ecu_key": ecu_key,
            "capture_root": str(capture_root),
            "captures_read": stats["captures_read"],
            "captures_skipped": stats["captures_skipped"],
            "qualification_runs": stats["qualification_runs"],
            "batch_runs": stats["batch_runs"],
            "experiment_runs": stats["experiment_runs"],
            "dids_observed": stats["dids_observed"],
            "signals_observed": len(records),
            "by_status": dict(sorted(by_status.items())),
        }

        if args.check:
            findings = check_map(resolve_path(root, args.check), signals)
            errors = [item for item in findings if item["severity"] == "error"]
            notes = [item for item in findings if item["severity"] == "note"]
            emit(
                {**summary, "check_errors": len(errors), "check_notes": len(notes)},
                render_summary(summary)
                + "\n\n"
                + fields([("check errors", len(errors)), ("check notes", len(notes))]),
                args.as_json,
            )
            if not args.as_json:
                for label, group in (("need review", errors), ("worth a note", notes)):
                    if not group:
                        continue
                    print(f"\n{len(group)} curated row(s) {label}:\n")
                    print(table(
                        [[f["line"], f["signal"], f["issue"]] for f in group[: args.limit]],
                        ["line", "signal", "issue"], [">", "<", "<"],
                    ))
                    if len(group) > args.limit:
                        LOG.info("%d more not shown; use --limit.", len(group) - args.limit)
            if not errors:
                if not args.as_json:
                    print("\nEvery curated row is backed by an observation.")
                return 0
            return 1

        emit(summary, render_summary(summary), args.as_json)

        if args.json_path:
            atomic_json(
                resolve_path(root, args.json_path),
                {**summary, "signals": [record.to_dict() for record in records]},
            )
            LOG.info("wrote %s", args.json_path)
        if args.csv_path:
            write_csv(resolve_path(root, args.csv_path), records)
            LOG.info("wrote %s", args.csv_path)

        shown = records
        if args.pending:
            shown = [record for record in records if record.status() in ("constant", "single_run")]

        if not args.as_json:
            print(heading(
                f"{len(shown)} signal(s) still needing excitation or an independent run"
                if args.pending else f"{len(shown)} observed signal(s)"
            ))
            rows = []
            for record in shown[: args.limit]:
                item = record.to_dict()
                span = (
                    f"{item['value_min']}..{item['value_max']}"
                    if item["value_min"] is not None
                    else f"raw {item['raw_min']}..{item['raw_max']}"
                )
                rows.append([
                    record.did, record.name[:40], record.status(),
                    record.runs, record.samples, span, record.unit,
                ])
            print(table(
                rows,
                ["did", "signal", "status", "runs", "samples", "range", "unit"],
                ["<", "<", "<", ">", ">", "<", "<"],
            ))
            if len(shown) > args.limit:
                LOG.info("%d more not shown; use --limit or --csv-out.",
                         len(shown) - args.limit)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
