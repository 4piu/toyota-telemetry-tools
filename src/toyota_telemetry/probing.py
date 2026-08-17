"""Probe every ECU endpoint the catalog knows about, in one pass.

A platform has many ECUs, and finding which of them answer is otherwise a
per-ECU ritual: hand-write a backend config, run a probe, hand-write a
validation profile, build a plan.  This does the whole sweep from the catalog's
own protocol table and, on request, writes the configuration for the ECUs that
answered.

Each probe is one tester-present frame at one endpoint, run through the same
backend and the same byte-exact allowlist as any other transmission.  The
backend is invoked once per ECU rather than being taught to loop, so nothing
about the transmit path changes to support this.

Dry run is the default.  Nothing is transmitted without --live.
"""

from __future__ import annotations

import argparse
import csv
from collections import OrderedDict
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from .project import atomic_json, load_json, project_root, resolve_path


SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# A probe needs a session only far enough to ask "are you there?".  Tester
# present is the least intrusive request in UDS and the one the adapter
# acceptance test already uses.
PROBE_SESSION = [{"request": "3E 00", "expect": "7E 00", "label": "tester present"}]


def slug(name: str, key: int) -> str:
    text = SLUG_STRIP.sub("-", str(name).lower()).strip("-")
    return text or f"ecu-{key}"


def read_endpoints(catalog_path: Path) -> list[dict[str, Any]]:
    """One entry per ECU in the catalog, with its protocol-table endpoint."""
    found: OrderedDict[int, dict[str, Any]] = OrderedDict()
    with catalog_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                key = int(row["ecu_key"])
            except (KeyError, TypeError, ValueError):
                continue
            if key in found:
                found[key]["catalog_rows"] += 1
                continue
            tester = (row.get("tester_can_id") or "").strip()
            response = (row.get("ecu_response_can_id_candidate") or "").strip()
            found[key] = {
                "ecu_key": key,
                "ecu_name": (row.get("ecu_name") or "").strip(),
                "tester_can_id": tester,
                "response_can_id": response,
                "endpoint_state": (row.get("endpoint_state") or "").strip(),
                "catalog_rows": 1,
            }
    return [item for item in found.values() if item["tester_can_id"] and item["response_can_id"]]


def probe_config(base: dict[str, Any], endpoint: dict[str, Any], profile_id: str) -> dict[str, Any]:
    config = json.loads(json.dumps(base))
    config["profile_id"] = profile_id
    config["description"] = (
        f"Generated probe configuration for {endpoint['ecu_name']} (key {endpoint['ecu_key']}). "
        "Session setup is tester-present only, which is all a probe needs; a capture "
        "configuration needs the real session sequence for this ECU."
    )
    config["transport"]["tester_can_id"] = endpoint["tester_can_id"]
    config["transport"]["response_can_id"] = endpoint["response_can_id"]
    config["session_setup"] = json.loads(json.dumps(PROBE_SESSION))
    return config


def run_probe(
    backend: Path, config_path: Path, root: Path, live: bool, timeout_ms: int
) -> dict[str, Any]:
    command = [
        str(backend), "--config", str(config_path), "--probe",
        "--timeout-ms", str(timeout_ms),
    ]
    if live:
        command.append("--live")
    try:
        completed = subprocess.run(
            command, cwd=root, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"answered": False, "outcome": "backend_error", "detail": str(exc)}

    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    if completed.returncode == 0:
        if not live:
            return {"answered": None, "outcome": "dry_run", "detail": ""}
        return {"answered": True, "outcome": "answered", "detail": _reply_line(stdout)}
    detail = stderr.splitlines()[-1] if stderr else "no output"
    outcome = "no_reply" if "within" in detail or "No CAN frame" in detail else "error"
    return {"answered": False, "outcome": outcome, "detail": detail}


def _reply_line(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("RX "):
            return line.strip()
    return ""


def emit_configs(
    root: Path,
    output_dir: Path,
    endpoint: dict[str, Any],
    base_backend: dict[str, Any],
    base_profile: dict[str, Any] | None,
    profile_id: str,
) -> list[str]:
    """Write a real backend config, and a validation profile if a base exists."""
    written: list[str] = []
    name = slug(endpoint["ecu_name"], endpoint["ecu_key"])
    target = output_dir / name
    target.mkdir(parents=True, exist_ok=True)

    config = json.loads(json.dumps(base_backend))
    config["profile_id"] = profile_id
    config["description"] = (
        f"{endpoint['ecu_name']} (key {endpoint['ecu_key']}) endpoint, confirmed answering "
        "a tester-present probe. The session setup below is inherited from the reference "
        "ECU and is a starting guess; confirm it before trusting a capture."
    )
    config["transport"]["tester_can_id"] = endpoint["tester_can_id"]
    config["transport"]["response_can_id"] = endpoint["response_can_id"]
    backend_path = target / "backend-config.json"
    backend_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    written.append(str(backend_path.relative_to(root)))

    if base_profile is not None:
        profile = json.loads(json.dumps(base_profile))
        profile["plan_id"] = f"{name}-progressive-v1"
        profile["endpoint"] = {
            "ecu_key": endpoint["ecu_key"],
            "tester_can_id": endpoint["tester_can_id"],
            "response_can_id": endpoint["response_can_id"],
            "bitrate": base_profile["endpoint"]["bitrate"],
            "addressing": base_profile["endpoint"].get("addressing", "normal"),
        }
        # Progress is reconstructed by filtering on profile_id alone, so each
        # ECU needs its own capture root or they count each other's work.
        profile["paths"] = {
            "plan": f"scratch/plans/vehicle-a/{name}-progressive.json",
            "capture_root": f"scratch/captures/vehicle-a/{name}",
            "output_root": f"scratch/captures/vehicle-a/{name}/progressive",
        }
        profile["backend"] = {
            "schema_version": 1,
            "executable": base_profile["backend"]["executable"],
            "arguments": ["--config", str(
                (backend_path.relative_to(root)).as_posix())],
            "mode_arguments": base_profile["backend"]["mode_arguments"],
        }
        profile_path = target / "validation-profile.json"
        profile_path.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(str(profile_path.relative_to(root)))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument(
        "--endpoint", action="append", metavar="TX:RX[:NAME]",
        help="probe an endpoint the catalog does not know, e.g. 0x7A2:0x7AA:unknown-a; "
             "repeatable, and usable without --catalog",
    )
    parser.add_argument(
        "--base-config", type=Path, required=True,
        help="an existing backend-config.json supplying bitrate, limits, and device",
    )
    parser.add_argument(
        "--base-profile", type=Path,
        help="an existing validation-profile.json to model emitted profiles on",
    )
    parser.add_argument("--backend", type=Path, help="backend executable; default from --base-profile")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--live", action="store_true", help="actually transmit")
    parser.add_argument("--timeout-ms", type=int, default=1500)
    parser.add_argument("--ecu-key", type=int, action="append", help="probe only these ECU keys")
    parser.add_argument(
        "--emit-config", type=Path,
        help="write backend/profile configuration for every ECU that answered",
    )
    parser.add_argument("--report", type=Path, default=Path("scratch/analysis/probe-report.json"))
    args = parser.parse_args()

    try:
        root = project_root(args.project_root, args.base_config)
        catalog_path = resolve_path(root, args.catalog) if args.catalog else None
        base_backend = load_json(resolve_path(root, args.base_config))
        base_profile = (
            load_json(resolve_path(root, args.base_profile)) if args.base_profile else None
        )
        profile_id = str(base_backend.get("profile_id", "unknown"))

        backend_value = args.backend
        if backend_value is None and base_profile is not None:
            backend_value = Path(base_profile["backend"]["executable"])
        if backend_value is None:
            raise ValueError("supply --backend or --base-profile so the executable is known")
        backend = resolve_path(root, backend_value)
        if args.live and not backend.is_file():
            raise FileNotFoundError(f"backend executable not found: {backend}")

        endpoints = read_endpoints(catalog_path) if args.catalog else []
        if args.ecu_key:
            wanted = set(args.ecu_key)
            endpoints = [e for e in endpoints if e["ecu_key"] in wanted]

        # An endpoint seen answering on the bus is worth probing even when no
        # catalog row claims it; that is how an unlisted ECU gets identified.
        for index, spec in enumerate(args.endpoint or [], start=1):
            parts = spec.split(":")
            if len(parts) not in (2, 3):
                raise ValueError(f"--endpoint must be TX:RX or TX:RX:NAME, got {spec!r}")
            tester, response = parts[0].strip(), parts[1].strip()
            name = parts[2].strip() if len(parts) == 3 else f"unlisted-{index}"
            endpoints.append({
                "ecu_key": -index,
                "ecu_name": name,
                "tester_can_id": tester,
                "response_can_id": response,
                "endpoint_state": "not in catalog",
                "catalog_rows": 0,
            })

        if not endpoints:
            raise ValueError("supply --catalog, --endpoint, or both")

        staging = resolve_path(root, Path("scratch/probe"))
        staging.mkdir(parents=True, exist_ok=True)

        print(f"{'ECU':>5}  {'name':<20} {'endpoint':<17} {'rows':>6}  result")
        print(f"{'-'*5}  {'-'*20} {'-'*17} {'-'*6}  {'-'*28}")
        results: list[dict[str, Any]] = []
        for endpoint in endpoints:
            name = slug(endpoint["ecu_name"], endpoint["ecu_key"])
            config_path = staging / f"{name}-probe.json"
            config_path.write_text(
                json.dumps(probe_config(base_backend, endpoint, profile_id), indent=2) + "\n",
                encoding="utf-8")
            outcome = run_probe(backend, config_path, root, args.live, args.timeout_ms)
            record = {**endpoint, **outcome}
            results.append(record)
            arrow = f"{endpoint['tester_can_id']} -> {endpoint['response_can_id']}"
            label = {
                "answered": "ANSWERED",
                "no_reply": "no reply",
                "error": "error",
                "backend_error": "backend error",
                "dry_run": "dry run - not transmitted",
            }.get(outcome["outcome"], outcome["outcome"])
            detail = outcome["detail"][:28]
            print(
                f"{endpoint['ecu_key']:>5}  {endpoint['ecu_name'][:20]:<20} {arrow:<17} "
                f"{endpoint['catalog_rows']:>6}  {label}"
                + (f"  {detail}" if detail and outcome["outcome"] != "answered" else "")
            )

        answered = [r for r in results if r.get("answered") is True]
        report = {
            "schema_version": 1,
            "profile_id": profile_id,
            "live": args.live,
            "catalog": str(args.catalog),
            "ecus_probed": len(results),
            "ecus_answered": len(answered),
            "results": results,
        }
        atomic_json(resolve_path(root, args.report), report)
        print(f"\n{len(answered)} of {len(results)} endpoint(s) answered. Report: {args.report}")

        if not args.live:
            print("Dry run: nothing was transmitted. Add --live to probe for real.")
            return 0

        if args.emit_config and answered:
            output_dir = resolve_path(root, args.emit_config)
            print()
            for record in answered:
                for path in emit_configs(
                    root, output_dir, record, base_backend, base_profile, profile_id
                ):
                    print(f"  wrote {path}")
            print(
                "\nEach emitted backend config inherits its session setup from the reference "
                "ECU. Confirm that sequence before running a capture mode."
            )
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
