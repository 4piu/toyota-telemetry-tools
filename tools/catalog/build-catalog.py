#!/usr/bin/env python3
"""Build a machine-readable telemetry candidate catalog from Techstream.

The script calls query-techstream-db.ps1 in 32-bit Windows PowerShell, joins
Phase-5 monitor, physical-conversion, and unit records, then writes CSV, JSON,
and a compact Markdown summary. It performs no vehicle communication.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable


REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPOSITORY / "build" / "catalog"
DEFAULT_QUERY_SCRIPT = REPOSITORY / "tools" / "techstream" / "query-techstream-db.ps1"
DEFAULT_DATABASE_DIRECTORY = Path(
    os.environ.get(
        "TOYOTA_TECHSTREAM_DB_ROOT",
        str(
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Toyota Diagnostics"
            / "Techstream"
            / "NA"
        ),
    )
)
QUERY_SCRIPT = DEFAULT_QUERY_SCRIPT
QUERY_CONTEXT: list[str] = []


def chunks(values: list[str], size: int = 300) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def powershell_path() -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    x86 = windows / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not x86.exists():
        raise FileNotFoundError(f"32-bit Windows PowerShell was not found at {x86}")
    return x86


def query_json_lines(arguments: list[str]) -> list[dict[str, Any]]:
    command = [
        str(powershell_path()),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(QUERY_SCRIPT),
        *QUERY_CONTEXT,
        *arguments,
        "-OutputFormat",
        "JsonLines",
    ]
    result = subprocess.run(
        command,
        cwd=REPOSITORY,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Techstream query failed ({result.returncode}):\n{result.stderr.strip()}"
        )

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(result.stdout.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid JSON from query line {line_number}: {line[:200]}"
            ) from exc
    return records


def formula_for(physical: dict[str, Any] | None) -> tuple[str, str]:
    if not physical or physical.get("Status") != "0x00000000":
        return "", "missing-physical-record"
    a = int(physical["CoefficientA"])
    b = int(physical["CoefficientB"])
    c = int(physical["CoefficientC"])
    n = int(physical["SignalLsb"])
    if b == 0:
        return "", "invalid-zero-divisor"
    if (a, b, c, n) == (1, 1, 0, 0):
        return "raw", "joined"
    return f"((raw * {a} / {b}) + {c}) / (10 ** {n})", "joined"


def export_ecu(
    ecu: dict[str, Any], overrides: dict[tuple[int, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    key = int(ecu["key"])
    print(f"Querying {ecu['name']} (key {key}) monitor records...", flush=True)
    monitors = query_json_lines(["-Keys", str(key), "-NoRaw"])
    monitors = [r for r in monitors if r.get("Status") == "0x00000000" and r.get("Name")]

    protocol_rows = query_json_lines(["-Keys", str(key), "-ProtocolInfo", "-NoRaw"])
    protocol = next(
        (
            row
            for row in protocol_rows
            if row.get("Status") == "0x00000000" and int(row.get("Count", 0)) > 0
        ),
        {},
    )

    group_ids = sorted({str(r["PhysicalGroup"]) for r in monitors if r.get("PhysicalGroup")})
    physical_rows: list[dict[str, Any]] = []
    for group_chunk in chunks(group_ids):
        physical_rows.extend(
            query_json_lines(
                ["-Keys", str(key), "-PhysicalGroups", ",".join(group_chunk), "-NoRaw"]
            )
        )
    physical_by_group = {
        str(row["PhysicalGroup"]): row
        for row in physical_rows
        if row.get("Status") == "0x00000000" and int(row.get("Count", 0)) > 0
    }

    unit_ids = sorted(
        {
            str(row["UnitId"])
            for row in physical_by_group.values()
            if row.get("UnitId") is not None
        }
    )
    unit_rows: list[dict[str, Any]] = []
    for unit_chunk in chunks(unit_ids):
        unit_rows.extend(
            query_json_lines(["-Keys", str(key), "-UnitIds", ",".join(unit_chunk), "-NoRaw"])
        )
    unit_by_id = {
        str(row["UnitId"]): row.get("Unit") or ""
        for row in unit_rows
        if row.get("Status") == "0x00000000" and int(row.get("Count", 0)) > 0
    }

    joined: list[dict[str, Any]] = []
    for monitor in monitors:
        group_id = str(monitor.get("PhysicalGroup") or "")
        physical = physical_by_group.get(group_id)
        formula, formula_state = formula_for(physical)
        unit_id = str(physical.get("UnitId") or "") if physical else ""
        override = overrides.get((key, str(monitor["InternalId"]).upper()), {})
        first_bit = int(monitor["FirstBit"])
        last_bit = int(monitor["LastBit"])
        joined.append(
            {
                "profile_id": "",
                "ecu_key": key,
                "ecu_name": ecu["name"],
                "ecu_database": ecu["database"],
                "ecu_profile_state": ecu["profile_state"],
                "ecu_evidence": ecu["evidence"],
                "protocol_id": protocol.get("ProtocolId", ""),
                "tester_can_id": protocol.get("PhysicalRequestCanId", ""),
                "ecu_response_can_id_candidate": ecu.get(
                    "response_can_id", protocol.get("ResponseCanIdCandidate", "")
                ),
                "endpoint_state": ecu["endpoint_state"],
                "functional_request_can_id": protocol.get("FunctionalRequestCanId", ""),
                "alternate_request_can_id": protocol.get("AlternateRequestCanId", ""),
                "record_index": monitor["Index"],
                "name": monitor["Name"],
                "internal_id": monitor["InternalId"],
                "flags": monitor["Flags"],
                "acquisition": monitor["Acquisition"],
                "read_request": monitor.get("ReadRequest") or "",
                "data_id": monitor["DataId"],
                "first_bit": first_bit,
                "last_bit": last_bit,
                "bit_length": last_bit - first_bit + 1,
                "physical_group": group_id,
                "coefficient_a": physical.get("CoefficientA", "") if physical else "",
                "coefficient_b": physical.get("CoefficientB", "") if physical else "",
                "coefficient_c": physical.get("CoefficientC", "") if physical else "",
                "decimal_places": physical.get("SignalLsb", "") if physical else "",
                "value_type": physical.get("ValueType", "") if physical else "",
                "unit_id": unit_id,
                "unit": unit_by_id.get(unit_id, ""),
                "formula": formula,
                "formula_state": formula_state,
                "static_state": "Candidate",
                "live_state": override.get("live_state", "Not tested"),
                "notes": override.get("notes", ""),
            }
        )
    return joined


def write_outputs(profile: dict[str, Any], rows: list[dict[str, Any]], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    profile_id = str(profile["profile_id"])
    for row in rows:
        row["profile_id"] = profile_id

    csv_path = output / "telemetry.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    json_path = output / "telemetry.json"
    json_path.write_text(
        json.dumps(
            {
                "profile": profile,
                "generation": {
                    "source": "local Techstream Phase-5 databases",
                    "record_count": len(rows),
                    "warning": "Static candidates require allowlisted live validation on the exact ECU calibration.",
                },
                "signals": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    did_groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        did_groups.setdefault((int(row["ecu_key"]), str(row["data_id"])), []).append(row)
    did_rows: list[dict[str, Any]] = []
    for (ecu_key, data_id), signals in sorted(did_groups.items()):
        first = signals[0]
        data_bytes = (max(int(signal["last_bit"]) for signal in signals) + 8) // 8
        did_rows.append(
            {
                "profile_id": profile_id,
                "ecu_key": ecu_key,
                "ecu_name": first["ecu_name"],
                "tester_can_id": first["tester_can_id"],
                "ecu_response_can_id_candidate": first["ecu_response_can_id_candidate"],
                "endpoint_state": first["endpoint_state"],
                "data_id": data_id,
                "read_request": first["read_request"],
                "signal_count": len(signals),
                "response_data_bytes_min": data_bytes,
                "expected_transport": "single-frame" if data_bytes + 3 <= 7 else "multi-frame",
                "signal_names": "; ".join(dict.fromkeys(str(s["name"]) for s in signals)),
                "live_evidence": "; ".join(
                    dict.fromkeys(
                        str(s["live_state"])
                        for s in signals
                        if s["live_state"] != "Not tested"
                    )
                ),
            }
        )
    did_path = output / "dids.csv"
    with did_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(did_rows[0].keys()))
        writer.writeheader()
        writer.writerows(did_rows)

    lines = [
        "# Static telemetry catalog summary",
        "",
        f"Profile: `{profile_id}`",
        "",
        profile["scope_note"],
        "",
        "| ECU key | ECU | Tester ID | Response ID | Endpoint evidence | Records | Distinct direct DIDs | Formula records joined |",
        "|---:|---|---:|---:|---|---:|---:|---:|",
    ]
    for ecu in profile["ecus"]:
        ecu_rows = [row for row in rows if row["ecu_key"] == int(ecu["key"])]
        direct = [row for row in ecu_rows if row["acquisition"] == "direct DID"]
        dids = len({row["data_id"] for row in direct})
        formulas = sum(row["formula_state"] == "joined" for row in ecu_rows)
        tester_id = ecu_rows[0]["tester_can_id"] if ecu_rows else ""
        response_id = ecu_rows[0]["ecu_response_can_id_candidate"] if ecu_rows else ""
        lines.append(
            f"| {ecu['key']} | {ecu['name']} | `{tester_id}` | `{response_id}` | {ecu['endpoint_state']} | {len(ecu_rows)} | {dids} | {formulas} |"
        )

    live_rows = [row for row in rows if row["live_state"] != "Not tested"]
    lines.extend(
        [
            "",
            f"Total records: **{len(rows)}**. Records carrying prior live evidence: **{len(live_rows)}**.",
            "",
            "## Interpretation limits",
            "",
            "- A database record is a candidate, not proof that the exact vehicle ECU implements that DID.",
            "- ECU database files aggregate multiple ECU software/calibration variants.",
            "- `direct DID` means the static record exposes a `22 xxxx` candidate; unsupported requests may still return a negative response.",
            "- Endpoint evidence comes from the supplied vehicle profile. A response ID inferred from a protocol convention remains a candidate until observed.",
            "- The numeric formula is joined from Techstream coefficients. Signedness, byte order, enumerations, invalid sentinels, and availability conditions still require further static analysis or live validation.",
            "- Mapped/dynamic records need acquisition-path reconstruction before they can be queried independently.",
            "",
            "## Files",
            "",
            "- `telemetry.csv`: flat working catalog for filtering and spreadsheet use.",
            "- `dids.csv`: one row per ECU/DID, with grouped signal names and estimated response size.",
            "- `telemetry.json`: complete catalog plus profile and evidence metadata.",
            "- `summary.md`: this compact generation report.",
            "",
        ]
    )
    (output / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(
    profile: dict[str, Any], rows: list[dict[str, Any]], output: Path, database_root: Path
) -> None:
    names = [str(profile["database_master_file"])] + [
        str(ecu["database"]) for ecu in profile["ecus"]
    ]
    inputs = []
    for name in dict.fromkeys(names):
        path = database_root / "DB" / name
        inputs.append(
            {
                "file": name,
                "sha256": sha256_file(path) if path.is_file() else None,
                "state": "hashed-local-input" if path.is_file() else "not-found",
            }
        )
    manifest = {
        "schema_version": 1,
        "profile_id": profile["profile_id"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generator": str(Path(__file__).relative_to(REPOSITORY)).replace("\\", "/"),
        "generator_sha256": sha256_file(Path(__file__)),
        "techstream_version": profile.get("techstream_version", "not-recorded"),
        "database_region": profile.get("database_region"),
        "inputs": inputs,
        "counts": {
            "signals": len(rows),
            "ecu_did_pairs": len(
                {(int(row["ecu_key"]), str(row["data_id"])) for row in rows}
            ),
            "live_evidence_rows": sum(
                row["live_state"] != "Not tested" for row in rows
            ),
        },
        "notice": "Database inputs are not included; review generated output before publication.",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    global QUERY_SCRIPT, QUERY_CONTEXT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--query-script", type=Path, default=DEFAULT_QUERY_SCRIPT)
    parser.add_argument(
        "--database-directory", type=Path, default=DEFAULT_DATABASE_DIRECTORY
    )
    parser.add_argument("--techstream-bin-directory", type=Path)
    args = parser.parse_args()

    QUERY_SCRIPT = args.query_script.resolve()
    if not QUERY_SCRIPT.is_file():
        raise FileNotFoundError(f"query script not found: {QUERY_SCRIPT}")
    QUERY_CONTEXT = []
    QUERY_CONTEXT.extend(["-DatabaseDirectory", str(args.database_directory)])
    if args.techstream_bin_directory:
        QUERY_CONTEXT.extend(
            ["-TechstreamBinDirectory", str(args.techstream_bin_directory)]
        )

    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    overrides = {
        (int(item["ecu_key"]), str(item["internal_id"]).upper()): item
        for item in profile.get("signal_overrides", [])
    }
    rows: list[dict[str, Any]] = []
    for ecu in profile["ecus"]:
        rows.extend(export_ecu(ecu, overrides))
    if not rows:
        raise RuntimeError("No telemetry records were returned")
    write_outputs(profile, rows, args.output)
    write_manifest(profile, rows, args.output, args.database_directory)
    print(f"Wrote {len(rows)} records to {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # concise CLI failure boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
