#!/usr/bin/env python3
"""Build an ecu-profile.json from the installed Techstream databases.

Techstream knows about every ECU definition across every model and database
generation - on one installation that is roughly two thousand records, most of
which name no database at all and almost none of which belong to any single
vehicle. This narrows that to a profile worth probing.

The default filter is which database files are installed, since an installation
only ships the databases for its coverage. That is still a superset: the result
lists candidates, and only a live probe establishes which ECUs a particular car
answers for.

Existing entries are preserved. Evidence, profile state, and signal overrides
already written for an ECU are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


PHASE = re.compile(r"_P(\d)\.ddb$", re.IGNORECASE)

DEFAULT_DATABASE_DIRECTORY = Path(
    r"C:\Program Files (x86)\Toyota Diagnostics\Techstream\NA"
)


def powershell_x86() -> Path:
    """The Techstream libraries are 32-bit, so the query must be too."""
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    x86 = windows / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not x86.is_file():
        raise FileNotFoundError(f"32-bit Windows PowerShell was not found at {x86}")
    return x86


def query(
    query_script: Path, database_directory: Path, arguments: list[str]
) -> list[dict[str, Any]]:
    """Run the query script and read its JSON Lines output."""
    command = [
        str(powershell_x86()), "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(query_script),
        "-DatabaseDirectory", str(database_directory),
        *arguments,
        "-NoRaw", "-OutputFormat", "JsonLines",
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8-sig", errors="replace", check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Techstream query failed ({result.returncode}):\n{result.stderr.strip()}")
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def endpoints_for(
    query_script: Path, database_directory: Path, keys: list[int]
) -> dict[int, dict[str, str]]:
    """Each ECU's diagnostic identifier pair, from its protocol table.

    Only the protocol table is read, not the monitor and conversion join a
    catalog build performs: a probe needs an endpoint and nothing else, and
    joining ECUs the vehicle may not have would be wasted work.
    """
    found: dict[int, dict[str, str]] = {}
    for row in query(
        query_script, database_directory,
        ["-Keys", ",".join(str(k) for k in keys), "-ProtocolInfo"],
    ):
        if row.get("Status") != "0x00000000" or int(row.get("Count", 0) or 0) <= 0:
            continue
        try:
            key = int(row.get("Key"))
        except (TypeError, ValueError):
            continue
        tester = row.get("PhysicalRequestCanId", "")
        response = row.get("ResponseCanIdCandidate", "")
        if tester and response:
            found[key] = {"tester_can_id": tester, "response_can_id": response}
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--merge", type=Path,
        help="existing ecu-profile.json whose evidence and overrides are preserved",
    )
    parser.add_argument(
        "--database-directory", type=Path, default=DEFAULT_DATABASE_DIRECTORY,
        help="Techstream region directory containing DB/",
    )
    parser.add_argument(
        "--query-script", type=Path,
        default=Path(__file__).resolve().parent / "query-techstream-db.ps1",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="include every ECU, not only those whose database is installed",
    )
    parser.add_argument(
        "--phase", type=int, action="append",
        help="restrict to these database generations, e.g. --phase 5",
    )
    parser.add_argument("--key", type=int, action="append", help="restrict to these ECU keys")
    parser.add_argument(
        "--no-endpoints", action="store_true",
        help="skip the protocol-table query; the result cannot be probed as-is",
    )
    parser.add_argument("--force", action="store_true", help="overwrite the output file")
    args = parser.parse_args()

    try:
        script = args.query_script.resolve()
        if not script.is_file():
            raise FileNotFoundError(f"query script not found: {script}")

        records = [
            row for row in query(script, args.database_directory, ["-Catalog"])
            if row.get("Ecu") and row.get("File") and row.get("Key") is not None
        ]
        if not records:
            raise ValueError("the ECU catalog query returned nothing")

        database_dir = args.database_directory / "DB"
        installed = (
            {p.name.lower() for p in database_dir.glob("*.ddb")}
            if database_dir.is_dir() else set()
        )
        if not args.all and not installed:
            raise ValueError(
                f"no .ddb files found under {database_dir}; pass --database-directory "
                "or --all to skip the installed-database filter"
            )

        wanted_phases = set(args.phase) if args.phase else None
        wanted_keys = set(args.key) if args.key else None

        selected = []
        for record in records:
            name = str(record["File"])
            if not args.all and name.lower() not in installed:
                continue
            if wanted_keys is not None and int(record["Key"]) not in wanted_keys:
                continue
            if wanted_phases is not None:
                match = PHASE.search(name)
                if not match or int(match.group(1)) not in wanted_phases:
                    continue
            selected.append(record)
        if not selected:
            raise ValueError("every record was filtered out; loosen --phase/--key/--all")

        existing: dict[str, Any] = {}
        previous: dict[int, dict[str, Any]] = {}
        if args.merge and args.merge.is_file():
            existing = json.loads(args.merge.read_text(encoding="utf-8-sig"))
            previous = {int(e["key"]): e for e in existing.get("ecus", [])}

        endpoints: dict[int, dict[str, str]] = {}
        if not args.no_endpoints:
            endpoints = endpoints_for(
                script, args.database_directory, [int(r["Key"]) for r in selected])

        ecus = []
        for record in sorted(selected, key=lambda r: int(r["Key"])):
            key = int(record["Key"])
            carried = previous.get(key)
            if carried is not None:
                entry = dict(carried)
                entry["name"] = carried.get("name") or record["Ecu"]
                entry["database"] = record["File"]
            else:
                entry = {
                    "key": key,
                    "name": record["Ecu"],
                    "database": record["File"],
                    "evidence": "Listed in the Techstream ECU catalog.",
                    "profile_state": "catalog-listed",
                    "endpoint_state": "not probed",
                }
            endpoint = endpoints.get(key)
            if endpoint:
                entry.setdefault("tester_can_id", endpoint["tester_can_id"])
                entry.setdefault("response_can_id", endpoint["response_can_id"])
            ecus.append(entry)

        profile = dict(existing) if existing else {}
        profile.setdefault("schema_version", 1)
        profile.setdefault("profile_id", "replace-me")
        profile.setdefault(
            "scope_note",
            "Catalog-listed ECUs are candidates. Only a live probe establishes which "
            "this vehicle answers for.",
        )
        profile["ecus"] = ecus
        profile.setdefault("signal_overrides", [])

        if args.output.exists() and not args.force:
            raise FileExistsError(f"{args.output} exists; pass --force to replace it")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        kept = sum(1 for e in ecus if int(e["key"]) in previous)
        print(json.dumps({
            "catalog_records": len(records),
            "installed_databases": len(installed),
            "selected": len(ecus),
            "carried_over": kept,
            "added": len(ecus) - kept,
            "with_endpoint": sum(1 for e in ecus if e.get("tester_can_id")),
            "output": str(args.output),
        }, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
