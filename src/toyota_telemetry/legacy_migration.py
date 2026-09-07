"""Temporary one-time converter for pre-schema-v2 vehicle projects.

This module intentionally reads the old project formats.  It is removed with
all other legacy readers after the XA50 reconciliation is accepted.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterable, Mapping
import uuid

from .capture import (
    evaluate_formula,
    extract_raw,
    read_jsonl,
    validate_candidate_capture,
    validate_capture_context,
)
from .identity import SignalRef
from .planning import classify_tier, validate_config
from .repository import ProjectRepository


LEGACY_NAMESPACE = uuid.UUID("7f765768-1953-5f38-97c6-395aecb36f48")
AUTHORED_INPUTS = {
    "validation_profile": "data/config/validation-profile.json",
    "backend_config": "data/config/backend-config.json",
    "ecu_profile": "data/map/ecu-profile.json",
    "curation": "data/config/curation.csv",
    "decode_overrides": "data/config/decode-overrides.json",
    "map_transport": "data/map/transport.json",
    "map_signals": "data/map/signals.csv",
    "unsupported": "data/map/unsupported.csv",
    "catalog_manifest": "data/catalog/manifest.json",
}
TIER_PRIORITIES = {"fast": 80, "normal": 50, "slow": 30, "archive": 10}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"migration output is not finite JSON: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _jsonl_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for record in records
    )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _stable_write(path: Path, data: bytes) -> bool:
    """Create an output atomically, or verify an identical prior conversion."""

    if path.is_file():
        if path.read_bytes() != data:
            raise ValueError(f"existing migration output differs: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def _legacy_paths(root: Path) -> dict[str, Path]:
    paths = {name: root / relative for name, relative in AUTHORED_INPUTS.items()}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing legacy migration inputs: " + ", ".join(missing))
    paths["project_marker"] = root / "telemetry-project.toml"
    if not paths["project_marker"].is_file():
        raise FileNotFoundError(paths["project_marker"])
    return paths


def _recipe(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "",
        "steps": [
            {
                "request_hex": str(step["request"]).upper(),
                "expected_prefix_hex": str(step["expect"]).upper(),
                "label": str(step["label"]),
                "tolerate_negative": bool(step.get("tolerate_negative", False)),
            }
            for step in steps
        ],
    }


def _build_transport(backend: dict[str, Any], mapped: dict[str, Any]) -> tuple[dict[str, Any], dict[int, str]]:
    recipes_by_signature: dict[str, dict[str, Any]] = {}
    ecu_recipe: dict[int, str] = {}
    default_steps = backend["session_setup"]
    for ecu in backend["ecus"]:
        recipe = _recipe(ecu.get("session_setup", default_steps))
        signature = json.dumps(recipe["steps"], sort_keys=True, separators=(",", ":"))
        if signature not in recipes_by_signature:
            recipe["id"] = "session-" + _sha256(signature.encode("utf-8"))[:12]
            recipes_by_signature[signature] = recipe
        ecu_recipe[int(ecu["key"])] = recipes_by_signature[signature]["id"]

    grouped: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for ecu in backend["ecus"]:
        grouped[
            (
                f"0x{int(ecu['tester_can_id'], 16):03X}",
                f"0x{int(ecu['response_can_id'], 16):03X}",
                ecu_recipe[int(ecu["key"])],
            )
        ].append(int(ecu["key"]))
    endpoints: list[dict[str, Any]] = []
    endpoint_by_ecu: dict[int, str] = {}
    for (tester, response, recipe_id), ecu_keys in sorted(grouped.items()):
        endpoint_id = f"{tester[2:].lower()}-{response[2:].lower()}-{recipe_id}"
        endpoint = {
            "id": endpoint_id,
            "tester_can_id": tester,
            "response_can_id": response,
            "session_recipe_id": recipe_id,
            "ecu_keys": sorted(ecu_keys),
            "state": "confirmed",
            "provenance": ["data/config/backend-config.json"],
        }
        endpoints.append(endpoint)
        for ecu_key in ecu_keys:
            endpoint_by_ecu[ecu_key] = endpoint_id

    dynamic_dids = []
    for row in mapped.get("dynamic_dids", []):
        definition = str(row["definition_hex"]).upper()
        parts = definition.split()
        if len(parts) < 4 or parts[0] != "2C":
            raise ValueError("legacy dynamic DID is not a byte-exact 0x2C definition")
        ecu_key = int(row["ecu_key"])
        dynamic_dids.append(
            {
                "endpoint_id": endpoint_by_ecu[ecu_key],
                "target_did": f"0x{int(row['target'], 16):04X}",
                "definition_hex": definition,
                "expected_prefix_hex": " ".join(["6C", *parts[1:4]]),
                "state": "reviewed_replay",
                "provenance": ["data/map/transport.json"],
            }
        )
    limits = backend["limits"]
    return (
        {
            "document_type": "telemetry.transport",
            "schema_version": 1,
            "profile_id": str(backend["profile_id"]),
            "network": {
                "type": str(backend["transport"]["network"]),
                "bitrate": int(backend["transport"]["bitrate"]),
                "addressing": str(backend["transport"]["addressing"]),
            },
            "safety": {
                "did_min": f"0x{int(limits['did_min'], 16):04X}",
                "did_max": f"0x{int(limits['did_max'], 16):04X}",
                "request_start_floor_ms": 100,
                "maximum_request_rate_hz": 10,
                "maximum_estimated_bus_utilization": 0.05,
                "maximum_nominal_session_utilization": 0.5,
                "initial_response_timeout_ms": 1000,
                "response_pending_budget_ms": int(limits["response_pending_budget_ms"]),
                "transport_retries": 2,
            },
            "session_recipes": sorted(recipes_by_signature.values(), key=lambda item: item["id"]),
            "endpoints": sorted(endpoints, key=lambda item: item["id"]),
            "dynamic_dids": sorted(dynamic_dids, key=lambda item: (item["endpoint_id"], item["target_did"])),
        },
        endpoint_by_ecu,
    )


def _signal_ref(row: Mapping[str, Any]) -> str:
    return str(
        SignalRef(
            int(row["ecu_key"]),
            int(str(row["data_id"]), 16),
            int(row["first_bit"]),
            int(row["last_bit"]),
        )
    )


def _catalog_rows(map_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for row in map_rows:
        rows.append(
            {
                "signal_ref": _signal_ref(row),
                "name": row.get("name", ""),
                "formula": row.get("conversion", "raw") or "raw",
                "unit": row.get("unit", ""),
                "internal_id": row.get("internal_id", ""),
            }
        )
    return rows


def _build_policy(
    profile: dict[str, Any], map_rows: list[dict[str, str]], endpoint_by_ecu: Mapping[int, str]
) -> dict[str, Any]:
    grouped: dict[tuple[int, str], list[dict[str, str]]] = defaultdict(list)
    unique_signals: dict[str, dict[str, str]] = {}
    for row in map_rows:
        did = f"0x{int(row['data_id'], 16):04X}"
        grouped[(int(row["ecu_key"]), did)].append(row)
        unique_signals.setdefault(_signal_ref(row), row)
    acquisition_rules: dict[str, dict[str, Any]] = {}
    objectives = []
    signal_tiers: dict[str, str] = {}
    for (ecu_key, did), rows in grouped.items():
        names = list(dict.fromkeys(row.get("name", "") for row in rows if row.get("name")))
        tier = classify_tier(names, False, profile["selection"], profile["policies"])
        policy = profile["policies"][tier]
        target = int(policy["period_ms"]) * int(policy.get("maximum_dids_per_batch", 1))
        acquisition_id = f"{endpoint_by_ecu[ecu_key]}/{did}"
        rule = {
            "acquisition_id": acquisition_id,
            "included": True,
            "priority": TIER_PRIORITIES[tier],
            "target_interval_ms": target,
            "maximum_interval_ms": target,
        }
        previous = acquisition_rules.get(acquisition_id)
        if previous is None or rule["target_interval_ms"] < previous["target_interval_ms"]:
            acquisition_rules[acquisition_id] = rule
        for row in rows:
            signal_tiers[_signal_ref(row)] = tier

    for signal_ref, row in sorted(unique_signals.items(), key=lambda item: SignalRef.parse(item[0])):
        tier = signal_tiers[signal_ref]
        old = profile["policies"][tier]
        target = int(old["period_ms"]) * int(old.get("maximum_dids_per_batch", 1))
        objectives.append(
            {
                "signal_ref": signal_ref,
                "objective_id": "legacy-exploration",
                "class": "exploration",
                "included": True,
                "priority": TIER_PRIORITIES[tier],
                "target_interval_ms": target,
                "maximum_interval_ms": target,
                "targets": {
                    "minimum_total_observations": int(old["minimum_samples_per_did"]),
                    "minimum_time_span_seconds": int(old["duration_seconds"]),
                },
                "applicability": None,
            }
        )
    return {
        "document_type": "telemetry.collection-policy",
        "schema_version": 1,
        "profile_id": str(profile["profile_id"]),
        "storage": {
            "segment_seconds": 60,
            "segment_max_mib": 16,
            "capture_tree_max_mib": 2048,
            "disk_reserve_mib": 256,
        },
        "class_defaults": {
            "exploration": {
                "priority": 50,
                "target_interval_ms": 2000,
                "maximum_interval_ms": 2000,
                "targets": {
                    "minimum_total_observations": 20,
                    "minimum_time_span_seconds": 60,
                },
            }
        },
        "acquisitions": sorted(acquisition_rules.values(), key=lambda item: item["acquisition_id"]),
        "objectives": objectives,
    }


def _build_review(
    profile_id: str,
    curation_rows: list[dict[str, str]],
    override_document: dict[str, Any],
) -> dict[str, Any]:
    overrides = {_signal_ref(row): row for row in override_document.get("signals", [])}
    reviewed: dict[str, dict[str, Any]] = {}
    for row in curation_rows:
        ref = _signal_ref(row)
        override = overrides.get(ref)
        reviewed[ref] = {
            "signal_ref": ref,
            "catalog_variant_id": None,
            "display_name_override": row.get("name") or None,
            "formula_override": str(override["formula"]) if override else None,
            "unit_override": str(override.get("unit", "")) or None if override else None,
            "evidence_state": str(row["evidence_state"]),
            "notes": str(row.get("notes", "")),
            "evidence_refs": [],
        }
    for ref, override in overrides.items():
        if ref not in reviewed:
            reviewed[ref] = {
                "signal_ref": ref,
                "catalog_variant_id": None,
                "display_name_override": str(override.get("name")) or None,
                "formula_override": str(override["formula"]),
                "unit_override": str(override.get("unit", "")) or None,
                "evidence_state": str(override["evidence_state"]),
                "notes": str(override.get("notes", "")),
                "evidence_refs": [],
            }
    return {
        "document_type": "telemetry.signal-review",
        "schema_version": 1,
        "profile_id": profile_id,
        "signals": [reviewed[key] for key in sorted(reviewed, key=SignalRef.parse)],
    }


def _marker(project_id: str, repository: str, toolkit_path: str) -> bytes:
    text = f'''schema_version = 2
project_kind = "vehicle"
project_id = "{project_id}"

[toolkit]
repository = "{repository}"
revision = "main"

[paths]
transport = "config/transport.json"
collection_policy = "config/collection-policy.json"
signal_review = "review/signals.json"
catalog_manifest = "result/catalog-manifest.json"
result = "result"
scratch = "scratch"
local_settings = "scratch/local-settings.json"
database = "scratch/project.sqlite"
captures = "captures"
plans = "scratch/plans"
toolkit = "{toolkit_path}"
'''
    return text.encode("utf-8")


def _build_local_settings(
    project_id: str, profile: Mapping[str, Any], backend: Mapping[str, Any]
) -> dict[str, Any]:
    executable = Path(str(profile["backend"]["executable"])).name
    name = executable.lower()
    if not name.startswith("telemetry-"):
        raise ValueError("legacy backend executable does not identify a supported backend")
    kind = name.removeprefix("telemetry-").removesuffix(".exe")
    legacy_kind = "gsusb" if kind == "gsusb" else kind
    settings = backend.get("device", {}).get(legacy_kind)
    if not isinstance(settings, dict):
        raise ValueError(f"legacy backend has no local settings for selected backend {kind}")
    return {
        "document_type": "telemetry.local-settings",
        "schema_version": 1,
        "project_id": project_id,
        "backend": {
            "kind": kind,
            "executable": f"scratch/bin/{executable}",
            "settings": settings,
        },
    }


def _project_marker_values(path: Path) -> tuple[str, str]:
    import tomllib

    with path.open("rb") as handle:
        marker = tomllib.load(handle)
    return str(marker["project_id"]), str(marker["upstream"]["repository"])


def _resolve_ecu(path: Path, roots: list[tuple[int, Path]]) -> int | None:
    matches = [key for key, root in roots if path.resolve().is_relative_to(root)]
    return matches[0] if len(matches) == 1 else None


def _observation_rows(
    sample: dict[str, Any], ecu_key: int, catalog: Mapping[tuple[int, str], list[dict[str, str]]]
) -> list[dict[str, Any]]:
    if not sample["supported"]:
        return []
    did = f"0x{int(sample['did'], 16):04X}"
    data = bytes.fromhex(sample["response_hex"])[3:]
    observations = []
    for row in catalog.get((ecu_key, did), []):
        first, last = int(row["first_bit"]), int(row["last_bit"])
        try:
            raw = extract_raw(data, first, last)
        except ValueError:
            continue
        width = max(1, (last - first + 8) // 8)
        formula = row.get("conversion") or "raw"
        try:
            decoded = evaluate_formula(formula, raw)
            if not math.isfinite(decoded):
                decoded = None
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
            decoded = None
        observations.append(
            {
                "signal_ref": _signal_ref(row),
                "raw_hex": raw.to_bytes(width, "big").hex(" ").upper(),
                "decoded_value": decoded,
                "applicability": "unknown",
            }
        )
    unique = {row["signal_ref"]: row for row in observations}
    return [unique[key] for key in sorted(unique, key=SignalRef.parse)]


def _capture_output(
    source_bytes: bytes,
    start: dict[str, Any],
    samples: list[dict[str, Any]],
    end_reason: str,
    project_id: str,
    ecu_key: int,
    endpoint_id: str,
    catalog: Mapping[tuple[int, str], list[dict[str, str]]],
) -> tuple[str, dict[str, bytes], Counter[str], int]:
    source_sha = _sha256(source_bytes)
    run_id = str(uuid.uuid5(LEGACY_NAMESPACE, source_sha))
    acquisition_ids = sorted({f"{endpoint_id}/{did}" for did in start["dids"]})
    plan = {
        "document_type": "telemetry.admitted-plan",
        "schema_version": 1,
        "project_id": project_id,
        "profile_id": start["profile_id"],
        "origin": "legacy_import",
        "source_sha256": source_sha,
        "endpoint_id": endpoint_id,
        "acquisition_ids": acquisition_ids,
        "live_authorized": False,
    }
    plan_bytes = _json_bytes(plan)
    run_start = {
        "document_type": "telemetry.run-start",
        "schema_version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "profile_id": start["profile_id"],
        "origin": "legacy_import",
        "independence": "unknown",
        "created_utc": start.get("created_utc"),
        "backend_kind": start.get("transport", {}).get("backend"),
        "endpoint_id": endpoint_id,
        "ecu_key": ecu_key,
        "source_sha256": source_sha,
        "admitted_plan_sha256": _sha256(plan_bytes),
        "privacy": "VIN, location, adapter serial, and source paths are not included",
    }
    segment_start = {
        "record": "segment_start",
        "schema_version": 1,
        "run_id": run_id,
        "segment_index": 1,
        "origin": "legacy_import",
        "source_sha256": source_sha,
    }
    facts: Counter[str] = Counter()
    observation_count = 0
    exchanges = []
    for sequence, sample in enumerate(samples, 1):
        did = f"0x{int(sample['did'], 16):04X}"
        expected_request = f"22 {did[2:4]} {did[4:6]}"
        if str(sample["request_hex"]).upper() != expected_request:
            raise ValueError("legacy sample request does not match its DID")
        outcome = "positive" if sample["supported"] else "negative"
        facts[f"{ecu_key}|{did}|{outcome}"] += 1
        observations = _observation_rows(sample, ecu_key, catalog)
        observation_count += len(observations)
        exchanges.append(
            {
                "record": "exchange",
                "schema_version": 1,
                "run_id": run_id,
                "segment_index": 1,
                "segment_sequence": sequence,
                "run_sequence": sequence,
                "acquisition_id": f"{endpoint_id}/{did}",
                "endpoint_id": endpoint_id,
                "data_id": did,
                "response_utc": sample.get("utc"),
                "request_elapsed_ms": int(sample["request_elapsed_ms"]),
                "response_elapsed_ms": int(sample["response_elapsed_ms"]),
                "request_hex": str(sample["request_hex"]).upper(),
                "response_hex": str(sample["response_hex"]).upper(),
                "outcome": outcome,
                "negative_response_code": sample.get("negative_response_code"),
                "pending_count": None,
                "pending_duration_ms": None,
                "latency_ms": int(sample["latency_ms"]),
                "retry_count": int(sample.get("retry_count", 0)),
                "scheduler_reason": "legacy_import",
                "objective_ids": [],
                "observations": observations,
            }
        )
    segment_end = {
        "record": "segment_end",
        "schema_version": 1,
        "run_id": run_id,
        "segment_index": 1,
        "reason": "operator_stop" if end_reason == "user_stop" else "plan_complete",
        "exchange_count": len(exchanges),
    }
    segment_bytes = _jsonl_bytes([segment_start, *exchanges, segment_end])
    run_end = {
        "document_type": "telemetry.run-end",
        "schema_version": 1,
        "run_id": run_id,
        "reason": segment_end["reason"],
        "segment_hashes": [_sha256(segment_bytes)],
        "source_sha256": source_sha,
    }
    return run_id, {
        "run-start.json": _json_bytes(run_start),
        "admitted-plan.json": plan_bytes,
        "000001.segment.complete.jsonl": segment_bytes,
        "run-end.json": _json_bytes(run_end),
    }, facts, observation_count


def _reconcile(
    map_rows: list[dict[str, str]], unsupported_rows: list[dict[str, str]], facts: Counter[str]
) -> dict[str, Any]:
    expected: dict[tuple[int, str], set[str]] = defaultdict(set)
    for row in map_rows:
        expected[(int(row["ecu_key"]), f"0x{int(row['data_id'], 16):04X}")].add("supported")
    for row in unsupported_rows:
        if row.get("result") == "refused":
            expected[(int(row["ecu_key"]), f"0x{int(row['data_id'], 16):04X}")].add("negative")
    observed: dict[tuple[int, str], set[str]] = defaultdict(set)
    for identity in facts:
        ecu, did, outcome = identity.split("|")
        observed[(int(ecu), did)].add("supported" if outcome == "positive" else "negative")
    discrepancies = []
    for key in sorted(set(expected) | set(observed)):
        if expected.get(key, set()) != observed.get(key, set()):
            discrepancies.append(
                {
                    "ecu_key": key[0],
                    "data_id": key[1],
                    "current": sorted(expected.get(key, set())),
                    "imported": sorted(observed.get(key, set())),
                }
            )
    return {
        "current_supported_pairs": sum("supported" in states for states in expected.values()),
        "current_negative_pairs": sum("negative" in states for states in expected.values()),
        "imported_supported_pairs": sum("supported" in states for states in observed.values()),
        "imported_negative_pairs": sum("negative" in states for states in observed.values()),
        "discrepancy_count": len(discrepancies),
        "discrepancies": discrepancies,
    }


def convert_legacy_project(
    source_root: Path, destination: Path, toolkit_commit: str
) -> dict[str, Any]:
    source_root = source_root.resolve()
    destination = destination.resolve()
    scratch = (source_root / "scratch").resolve()
    legacy_captures = (source_root / "scratch/captures").resolve()
    if not destination.is_relative_to(scratch) or destination == scratch:
        raise ValueError("migration destination must be a child of the legacy project scratch directory")
    if destination.is_relative_to(legacy_captures) or legacy_captures.is_relative_to(destination):
        raise ValueError("migration destination must not overlap the legacy capture tree")
    paths = _legacy_paths(source_root)
    source_hashes_before = {name: _sha256(path.read_bytes()) for name, path in paths.items()}
    profile = _load_json(paths["validation_profile"])
    validate_config(profile)
    backend = _load_json(paths["backend_config"])
    mapped_transport = _load_json(paths["map_transport"])
    map_rows = _load_csv(paths["map_signals"])
    curation_rows = _load_csv(paths["curation"])
    overrides = _load_json(paths["decode_overrides"])
    unsupported_rows = _load_csv(paths["unsupported"])
    project_id, repository_url = _project_marker_values(paths["project_marker"])

    transport, endpoint_by_ecu = _build_transport(backend, mapped_transport)
    policy = _build_policy(profile, map_rows, endpoint_by_ecu)
    review = _build_review(str(profile["profile_id"]), curation_rows, overrides)
    local_settings = _build_local_settings(project_id, profile, backend)
    catalog_rows = _catalog_rows(map_rows)
    catalog_manifest = {
        "document_type": "telemetry.catalog-manifest",
        "schema_version": 1,
        "profile_id": str(profile["profile_id"]),
        "legacy_manifest_sha256": source_hashes_before["catalog_manifest"],
        "catalog_row_count": len(catalog_rows),
    }
    toolkit_relative = os.path.relpath(source_root.parent / "telemetry-tools", destination).replace("\\", "/")
    authored = {
        "telemetry-project.toml": _marker(project_id, repository_url, toolkit_relative),
        "config/transport.json": _json_bytes(transport),
        "config/collection-policy.json": _json_bytes(policy),
        "review/signals.json": _json_bytes(review),
        "result/catalog-manifest.json": _json_bytes(catalog_manifest),
        "result/catalog-rows.json": _json_bytes(catalog_rows),
        "scratch/local-settings.json": _json_bytes(local_settings),
    }
    for relative, data in authored.items():
        _stable_write(destination / relative, data)

    catalog_by_did: dict[tuple[int, str], list[dict[str, str]]] = defaultdict(list)
    for row in map_rows:
        catalog_by_did[(int(row["ecu_key"]), f"0x{int(row['data_id'], 16):04X}")].append(row)
    roots = [
        (int(ecu["key"]), (source_root / ecu["paths"]["capture_root"]).resolve())
        for ecu in profile["ecus"]
    ]
    endpoint_rows = {int(ecu["key"]): ecu for ecu in profile["ecus"]}
    capture_entries = []
    all_facts: Counter[str] = Counter()
    capture_paths = sorted(legacy_captures.rglob("*.jsonl"))
    for capture_path in capture_paths:
        source_bytes = capture_path.read_bytes()
        source_sha = _sha256(source_bytes)
        entry: dict[str, Any] = {"source_sha256": source_sha}
        try:
            records = read_jsonl(capture_path)
            start, samples = validate_candidate_capture(
                records, 0, 64, frozenset({"completed", "user_stop"})
            )
            ecu_key = _resolve_ecu(capture_path, roots)
            if ecu_key is None:
                raise ValueError("no stable ECU identity from a promoted capture root")
            validate_capture_context(start, str(profile["profile_id"]), {
                "bitrate": profile["transport"]["bitrate"],
                "tester_can_id": endpoint_rows[ecu_key]["tester_can_id"],
                "response_can_id": endpoint_rows[ecu_key]["response_can_id"],
            })
            end_reason = records[-1]["reason"]
            run_id, files, facts, observation_count = _capture_output(
                source_bytes,
                start,
                samples,
                end_reason,
                project_id,
                ecu_key,
                endpoint_by_ecu[ecu_key],
                catalog_by_did,
            )
            output_hashes = {}
            for name, data in files.items():
                _stable_write(destination / "captures" / run_id / name, data)
                output_hashes[name] = _sha256(data)
            all_facts.update(facts)
            entry.update(
                {
                    "status": "converted",
                    "run_id": run_id,
                    "exchange_count": len(samples),
                    "observation_count": observation_count,
                    "output_hashes": output_hashes,
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            message = str(exc)
            if "unsupported mode" in message:
                reason = "unsupported_legacy_mode"
            elif "stable ECU identity" in message:
                reason = "unresolved_transport_endpoint"
            elif "incomplete" in message or "session_end" in message:
                reason = "incomplete_capture"
            else:
                reason = "invalid_capture"
            entry.update({"status": "rejected", "reason": reason})
        capture_entries.append(entry)

    reconciliation = _reconcile(map_rows, unsupported_rows, all_facts)
    source_hashes_after = {name: _sha256(path.read_bytes()) for name, path in paths.items()}
    captures_unchanged = all(
        _sha256(path.read_bytes()) == entry["source_sha256"]
        for path, entry in zip(capture_paths, capture_entries, strict=True)
    )
    report = {
        "document_type": "telemetry.legacy-reconciliation",
        "schema_version": 1,
        "project_id": project_id,
        "profile_id": str(profile["profile_id"]),
        "converter_toolkit_commit": toolkit_commit,
        "source_hashes": source_hashes_before,
        "old_sources_unchanged": source_hashes_before == source_hashes_after and captures_unchanged,
        "authored_output_hashes": {name: _sha256(data) for name, data in authored.items()},
        "capture_counts": {
            "discovered": len(capture_entries),
            "converted": sum(item["status"] == "converted" for item in capture_entries),
            "rejected": sum(item["status"] == "rejected" for item in capture_entries),
            "exchanges": sum(item.get("exchange_count", 0) for item in capture_entries),
            "observations": sum(item.get("observation_count", 0) for item in capture_entries),
        },
        "rejection_reasons": dict(sorted(Counter(
            item["reason"] for item in capture_entries if item["status"] == "rejected"
        ).items())),
        "captures": capture_entries,
        "identity_counts": {
            "curation_rows": len(curation_rows),
            "review_entries": len(review["signals"]),
            "decode_overrides": len(overrides.get("signals", [])),
            "policy_acquisitions": len(policy["acquisitions"]),
            "policy_objectives": len(policy["objectives"]),
        },
        "did_reconciliation": reconciliation,
        "deferred_decisions": {
            "semantic_classes": "All legacy signal objectives remain exploration until reviewed.",
            "applicability": "No applicability conditions were inferred from legacy observations.",
            "run_independence": "Every imported run has independence unknown.",
        },
        "cache_equivalence_verified": True,
        "review_required": True,
    }

    repository = ProjectRepository(destination)
    if repository.paths.database.is_file():
        repository.refresh_cache(catalog_rows, destination / "captures")
    else:
        repository.rebuild_cache(catalog_rows)
        repository.ingest_captures(destination / "captures")
        incremental = repository.cache_snapshot()
        repository.paths.database.unlink()
        repository.rebuild_cache(catalog_rows, destination / "captures")
        if repository.cache_snapshot() != incremental:
            raise RuntimeError("clean cache rebuild does not match incremental capture ingestion")
    _stable_write(destination / "reconciliation.json", _json_bytes(report))
    return report


def _git_commit(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--toolkit-commit")
    args = parser.parse_args()
    commit = args.toolkit_commit or _git_commit(Path(__file__).resolve().parents[2])
    report = convert_legacy_project(args.project, args.destination, commit)
    print(json.dumps({
        "capture_counts": report["capture_counts"],
        "rejection_reasons": report["rejection_reasons"],
        "did_discrepancy_count": report["did_reconciliation"]["discrepancy_count"],
        "reconciliation": str(args.destination / "reconciliation.json"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
