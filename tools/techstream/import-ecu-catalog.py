#!/usr/bin/env python3
"""Turn a Techstream ECU-catalog dump into an ecu-profile.json.

`query-techstream-db.ps1 -Catalog` lists every ECU definition the installed
Techstream knows about, across every model and database generation - roughly two
thousand records, almost none of which belong to any one vehicle. This narrows
that to a usable profile.

The default filter is which database files are actually installed. A Techstream
installation only ships the databases for the coverage it was built for, so the
present-on-disk set is already far closer to one vehicle than the catalog is.
That is still a superset: the profile it produces lists candidates, and only a
live probe establishes which ECUs a particular car answers for.

Existing entries are preserved. Hand-written evidence, profile state, and signal
overrides in a profile being merged are never overwritten.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


RECORD_FIELD = re.compile(r"^(Index|Ecu|File|Key)\s*:\s*(.*)$")
PHASE = re.compile(r"_P(\d)\.ddb$", re.IGNORECASE)

DEFAULT_DATABASE_DIRECTORY = Path(
    r"C:\Program Files (x86)\Toyota Diagnostics\Techstream\NA"
)


def parse_dump(path: Path) -> list[dict[str, str]]:
    """Read the dump, which PowerShell writes as UTF-16."""
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = path.read_text(encoding=encoding)
            if "Ecu" in text:
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        raise ValueError(f"could not decode {path}")

    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        match = RECORD_FIELD.match(line.strip())
        if not match:
            continue
        field, value = match.group(1), match.group(2).strip()
        if field == "Index" and current:
            records.append(current)
            current = {}
        current[field] = value
    if current:
        records.append(current)

    complete = []
    for record in records:
        if not record.get("Ecu") or not record.get("File") or not record.get("Key"):
            continue
        try:
            record["Key"] = int(record["Key"])
        except ValueError:
            continue
        complete.append(record)
    return complete


def powershell_x86() -> Path:
    """The Techstream libraries are 32-bit, so the query must be too."""
    windows = Path(__import__("os").environ.get("WINDIR", r"C:\Windows"))
    x86 = windows / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not x86.is_file():
        raise FileNotFoundError(f"32-bit Windows PowerShell was not found at {x86}")
    return x86


def query_endpoints(
    query_script: Path, database_directory: Path, keys: list[int]
) -> dict[int, dict[str, str]]:
    """Read each ECU's protocol table for its diagnostic identifier pair.

    Only the protocol table is queried, not the full monitor/conversion join a
    catalog build performs: a probe needs an endpoint and nothing else, and
    joining ninety-five ECUs a vehicle may not even have would be wasted work.
    """
    command = [
        str(powershell_x86()), "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(query_script),
        "-DatabaseDirectory", str(database_directory),
        "-Keys", ",".join(str(k) for k in keys),
        "-ProtocolInfo", "-NoRaw", "-OutputFormat", "JsonLines",
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"protocol query failed ({result.returncode}):\n{result.stderr.strip()}")
    endpoints: dict[int, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("Status") != "0x00000000" or int(row.get("Count", 0) or 0) <= 0:
            continue
        try:
            key = int(row.get("Key"))
        except (TypeError, ValueError):
            continue
        tester = row.get("PhysicalRequestCanId", "")
        response = row.get("ResponseCanIdCandidate", "") or row.get("response_can_id", "")
        if tester and response:
            endpoints[key] = {
                "tester_can_id": tester,
                "response_can_id": response,
                "functional_request_can_id": row.get("FunctionalRequestCanId", ""),
            }
    return endpoints


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path, help="output of query-techstream-db.ps1 -Catalog")
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
        "--all", action="store_true",
        help="include every ECU in the dump, not only those whose database is installed",
    )
    parser.add_argument(
        "--phase", type=int, action="append",
        help="additionally restrict to these database generations, e.g. --phase 5",
    )
    parser.add_argument("--key", type=int, action="append", help="restrict to these ECU keys")
    parser.add_argument(
        "--protocol-info", action="store_true",
        help="also query each selected ECU's protocol table for its endpoint",
    )
    parser.add_argument(
        "--query-script", type=Path,
        default=Path(__file__).resolve().parent / "query-techstream-db.ps1",
    )
    parser.add_argument("--force", action="store_true", help="overwrite the output file")
    args = parser.parse_args()

    try:
        records = parse_dump(args.dump)
        if not records:
            raise ValueError(f"no ECU records parsed from {args.dump}")

        database_dir = args.database_directory / "DB"
        installed = {p.name.lower() for p in database_dir.glob("*.ddb")} if database_dir.is_dir() else set()
        if not args.all and not installed:
            raise ValueError(
                f"no .ddb files found under {database_dir}; pass --database-directory "
                "or --all to skip the installed-database filter"
            )

        wanted_phases = set(args.phase) if args.phase else None
        wanted_keys = set(args.key) if args.key else None

        selected = []
        for record in records:
            name = record["File"]
            if not args.all and name.lower() not in installed:
                continue
            if wanted_keys is not None and record["Key"] not in wanted_keys:
                continue
            if wanted_phases is not None:
                match = PHASE.search(name)
                if not match or int(match.group(1)) not in wanted_phases:
                    continue
            selected.append(record)

        if not selected:
            raise ValueError("every record was filtered out; loosen --phase/--key/--all")

        existing: dict[str, Any] = {}
        previous_by_key: dict[int, dict[str, Any]] = {}
        if args.merge and args.merge.is_file():
            existing = json.loads(args.merge.read_text(encoding="utf-8-sig"))
            previous_by_key = {int(e["key"]): e for e in existing.get("ecus", [])}

        endpoints: dict[int, dict[str, str]] = {}
        if args.protocol_info:
            endpoints = query_endpoints(
                args.query_script.resolve(),
                args.database_directory,
                [r["Key"] for r in selected],
            )

        ecus = []
        for record in sorted(selected, key=lambda r: r["Key"]):
            key = record["Key"]
            previous = previous_by_key.get(key)
            if previous is not None:
                # Keep everything a human wrote about this ECU.
                entry = dict(previous)
                entry["name"] = previous.get("name") or record["Ecu"]
                entry["database"] = record["File"]
            else:
                entry = {
                    "key": key,
                    "name": record["Ecu"],
                    "database": record["File"],
                    "evidence": f"Techstream ECU catalog index {record.get('Index', '?')}.",
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

        output = args.output
        if output.exists() and not args.force:
            raise FileExistsError(f"{output} exists; pass --force to replace it")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        carried = sum(1 for e in ecus if int(e["key"]) in previous_by_key)
        print(json.dumps({
            "records_in_dump": len(records),
            "installed_databases": len(installed),
            "selected": len(ecus),
            "carried_over": carried,
            "added": len(ecus) - carried,
            "with_endpoint": sum(1 for e in ecus if e.get("tester_can_id")),
            "output": str(output),
        }, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
