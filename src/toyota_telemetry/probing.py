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
from typing import Any

from .console import (
    LOG,
    Progress,
    add_output_arguments,
    configure,
    emit,
    fields,
    heading,
    table,
)
from .project import atomic_json, load_json, project_root, resolve_path


SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# A probe needs a session only far enough to ask "are you there?".  Tester
# present is the least intrusive request in UDS and the one the adapter
# acceptance test already uses.
PROBE_SESSION = [{"request": "3E 00", "expect": "7E 00", "label": "tester present"}]


def slug(name: str, key: int) -> str:
    text = SLUG_STRIP.sub("-", str(name).lower()).strip("-")
    return text or f"ecu-{key}"


def read_profile_endpoints(profile_path: Path) -> list[dict[str, Any]]:
    """Endpoints from an ecu-profile.json.

    This is the wide net: a profile imported from the Techstream ECU catalog
    lists every ECU the installation knows about for this generation, most of
    which a given vehicle will not have. Probing is how that list gets cut down.
    """
    document = load_json(profile_path)
    found: list[dict[str, Any]] = []
    for ecu in document.get("ecus", []):
        tester = str(ecu.get("tester_can_id") or "").strip()
        response = str(ecu.get("response_can_id") or "").strip()
        if not tester or not response:
            continue
        found.append({
            "ecu_key": int(ecu["key"]),
            "ecu_name": str(ecu.get("name", "")),
            "tester_can_id": tester,
            "response_can_id": response,
            "endpoint_state": str(ecu.get("endpoint_state", "")),
            "catalog_rows": 0,
        })
    return found


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
    config["session_setup"] = json.loads(json.dumps(PROBE_SESSION))
    config["ecus"] = [{
        "key": max(endpoint["ecu_key"], 0),
        "name": endpoint["ecu_name"] or f"ecu-{endpoint['ecu_key']}",
        "tester_can_id": endpoint["tester_can_id"],
        "response_can_id": endpoint["response_can_id"],
    }]
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
        # An ECU that refuses the request still answered it. Both outcomes mean
        # something is present at this address, which is what a probe asks.
        for line in stdout.splitlines():
            if line.startswith("PRESENT:"):
                return {
                    "answered": True,
                    "outcome": "answered_negative",
                    "detail": line.strip(),
                }
        return {"answered": True, "outcome": "answered", "detail": _reply_line(stdout)}
    detail = stderr.splitlines()[-1] if stderr else "no output"
    outcome = "no_reply" if "within" in detail or "No CAN frame" in detail else "error"
    return {"answered": False, "outcome": outcome, "detail": detail}


def _reply_line(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("RX "):
            return line.strip()
    return ""


def ecus_fragment(answered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `ecus` entries for endpoints that answered.

    Configuration is not written directly: a single file now describes every ECU
    behind the gateway, and rewriting it from a probe would discard the plan
    identifiers, capture paths, and notes a person put there. The fragment is
    printed for review and pasting instead.
    """
    return [
        {
            "key": record["ecu_key"],
            "name": record["ecu_name"],
            "tester_can_id": record["tester_can_id"],
            "response_can_id": record["response_can_id"],
        }
        for record in sorted(answered, key=lambda r: r["ecu_key"])
    ]


OUTCOME_LABEL = {
    "answered": "answered",
    "answered_negative": "answered (negative)",
    "no_reply": "no reply",
    "error": "error",
    "backend_error": "backend error",
    "dry_run": "not transmitted",
}


def render_results(report: dict[str, Any]) -> str:
    """Answering endpoints first: they are the ones worth configuring."""
    order = {"answered": 0, "answered_negative": 1, "no_reply": 2}
    rows = [
        [
            record["ecu_key"], record["ecu_name"][:24],
            f"{record['tester_can_id']} -> {record['response_can_id']}",
            record["catalog_rows"],
            OUTCOME_LABEL.get(record["outcome"], record["outcome"]),
            record["detail"][:32] if record["outcome"] != "answered" else "",
        ]
        for record in sorted(
            report["results"],
            key=lambda r: (order.get(r["outcome"], 3), r["ecu_key"]),
        )
    ]
    return (
        heading("Endpoint probe" + ("" if report["live"] else " - dry run"))
        + "\n" + fields([
            ("probed", report["ecus_probed"]),
            ("answered", report["ecus_answered"]),
        ])
        + "\n\n" + table(
            rows,
            ["ecu", "name", "endpoint", "rows", "result", "detail"],
            ["<", "<", "<", ">", "<", "<"],
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument(
        "--profile", type=Path,
        help="ecu-profile.json carrying tester/response identifiers; the usual "
             "source when sweeping a freshly imported ECU list",
    )
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
        "--emit-ecus", type=Path,
        help="write the `ecus` entries for answering endpoints to this file for review",
    )
    parser.add_argument("--report", type=Path, default=Path("scratch/analysis/probe-report.json"))
    add_output_arguments(parser)
    args = parser.parse_args()
    configure(args)

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
            backend_value = Path(base_profile.get("backend", {}).get("executable", "")) or None
        if backend_value is None:
            raise ValueError("supply --backend or --base-profile so the executable is known")
        backend = resolve_path(root, backend_value)
        if args.live and not backend.is_file():
            raise FileNotFoundError(f"backend executable not found: {backend}")

        endpoints = read_endpoints(catalog_path) if args.catalog else []
        if args.profile:
            known = {e["ecu_key"] for e in endpoints}
            endpoints.extend(
                e for e in read_profile_endpoints(resolve_path(root, args.profile))
                if e["ecu_key"] not in known
            )
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
            raise ValueError("supply --catalog, --profile, or --endpoint")

        staging = resolve_path(root, Path("scratch/probe"))
        staging.mkdir(parents=True, exist_ok=True)

        results: list[dict[str, Any]] = []
        sweep = Progress(
            len(endpoints), label="probing",
            enabled=not args.no_progress and not args.quiet,
        )
        for endpoint in endpoints:
            name = slug(endpoint["ecu_name"], endpoint["ecu_key"])
            config_path = staging / f"{name}-probe.json"
            config_path.write_text(
                json.dumps(probe_config(base_backend, endpoint, profile_id), indent=2) + "\n",
                encoding="utf-8")
            sweep.render(f"{endpoint['ecu_key']} {endpoint['ecu_name'][:24]}")
            outcome = run_probe(backend, config_path, root, args.live, args.timeout_ms)
            record = {**endpoint, **outcome}
            results.append(record)
            sweep.advance()
            LOG.debug(
                "%s %s -> %s: %s %s", endpoint["ecu_key"], endpoint["tester_can_id"],
                endpoint["response_can_id"],
                OUTCOME_LABEL.get(outcome["outcome"], outcome["outcome"]),
                outcome["detail"],
            )
        sweep.close()

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
        if not args.live:
            # A dry run measured nothing. Writing a report anyway would overwrite
            # the results of a real sweep with a file full of nulls, and the real
            # sweep is the one that needed a vehicle.
            emit(report, render_results(report), args.as_json)
            LOG.info(
                "Dry run: nothing was transmitted and no report was written. "
                "Add --live to probe for real."
            )
            return 0

        atomic_json(resolve_path(root, args.report), report)
        emit(report, render_results(report), args.as_json)
        LOG.info("Report: %s", args.report)

        if answered:
            fragment = ecus_fragment(answered)
            rendered = json.dumps(fragment, indent=2, ensure_ascii=False)
            if args.emit_ecus:
                target = resolve_path(root, args.emit_ecus)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(rendered + "\n", encoding="utf-8")
                LOG.info(
                    "Wrote %d `ecus` entr(ies) to %s.", len(fragment), args.emit_ecus)
            elif not args.as_json:
                print("\n`ecus` entries for the endpoints that answered:")
                print(rendered)
            LOG.info(
                "Several entries may share one endpoint: Techstream models some ECUs as "
                "separate databases over the same diagnostic address. Those are one ECU "
                "to capture from, with more than one catalog to read it with."
            )
            LOG.info(
                "Add these to backend-config.json, and to validation-profile.json with a "
                "plan_id and capture paths. They inherit the shared session sequence, "
                "which is a guess for an ECU never captured from; confirm it first."
            )
        return 0
    except (OSError, ValueError, KeyError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
