"""Offline compilation and safety admission for collection plans.

The compiled plan keeps its schema-1 ``objectives``/``objective_ids`` field
names so previously captured native runs remain valid. Each compiled entry is
one collection target, identified by its signal reference.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from .identity import AcquisitionRef, SignalRef
from .objectives import condition_leaves, condition_refs
from .repository import ProjectRepository

ALL_COMPLETE = "all included collection targets are already complete"


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
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


def compile_plan(
    repository: ProjectRepository,
    *,
    available_disk_mib: int,
    created_utc: str,
    output: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Compile an admitted plan without opening or resolving an adapter."""

    policy = repository.policy
    transport = repository.transport
    if not any(row["included"] for row in policy["targets"]):
        raise ValueError("collection policy has no included targets")
    storage = policy["storage"]
    required_disk = storage["disk_reserve_mib"] + storage["segment_max_mib"]
    if available_disk_mib < required_disk:
        raise ValueError(
            f"insufficient disk budget: {available_disk_mib} MiB available, {required_disk} MiB required"
        )
    endpoint_by_ecu = {
        int(ecu): endpoint
        for endpoint in transport["endpoints"]
        for ecu in endpoint["ecu_keys"]
    }
    acquisition_rules = {
        row["acquisition_id"]: row for row in policy["acquisitions"] if row["included"]
    }
    progress_by_signal = {row["signal_ref"]: row for row in repository.replay_progress()}
    compiled_targets = []
    target_by_acquisition: dict[str, list[dict[str, Any]]] = {}
    dependency_by_acquisition: dict[str, list[dict[str, Any]]] = {}
    for authored in policy["targets"]:
        if not authored["included"]:
            continue
        prior_progress = progress_by_signal.get(authored["signal_ref"])
        if prior_progress is not None and prior_progress["complete"]:
            continue
        signal = SignalRef.parse(authored["signal_ref"])
        endpoint = endpoint_by_ecu[signal.ecu_key]
        acquisition_id = f"{endpoint['id']}/0x{signal.data_id:04X}"
        if acquisition_id not in acquisition_rules:
            raise ValueError(
                f"target has no included DID settings: {authored['signal_ref']} "
                f"needs {acquisition_id}"
            )
        condition = authored["condition"]
        compiled = {
            "signal_ref": authored["signal_ref"],
            "included": True,
            "condition": condition,
            "requirements": dict(authored["requirements"]),
            "minimum_difference": authored.get("minimum_difference"),
            "acquisition_id": acquisition_id,
            "condition_dependencies": sorted(condition_refs(condition)) if condition else [],
            "prior_progress": prior_progress,
        }
        compiled_targets.append(compiled)
        target_by_acquisition.setdefault(acquisition_id, []).append(compiled)
        if condition is not None:
            for dependency_ref, maximum_age_ms in condition_leaves(condition):
                dependency = SignalRef.parse(dependency_ref)
                dependency_endpoint = endpoint_by_ecu[dependency.ecu_key]
                dependency_id = f"{dependency_endpoint['id']}/0x{dependency.data_id:04X}"
                if dependency_id not in acquisition_rules:
                    raise ValueError(
                        f"condition signal has no included DID settings: {dependency_ref} "
                        f"needs {dependency_id}"
                    )
                dependency_by_acquisition.setdefault(dependency_id, []).append(
                    {
                        "target": authored["signal_ref"],
                        "maximum_age_ms": maximum_age_ms,
                        "priority": acquisition_rules[acquisition_id]["priority"],
                    }
                )

    if not compiled_targets:
        raise ValueError(ALL_COMPLETE)

    costs = repository.acquisition_cost_facts()
    safety = transport["safety"]
    floor = int(safety["request_start_floor_ms"])
    maximum_rate = float(safety["maximum_request_rate_hz"])
    if 1000 / floor > maximum_rate:
        raise ValueError("request-start floor exceeds the tracked maximum request rate")
    endpoint_rows = {row["id"]: row for row in transport["endpoints"]}
    recipe_rows = {row["id"]: row for row in transport["session_recipes"]}
    acquisitions = []
    load_by_endpoint: dict[str, dict[str, float]] = {}
    for acquisition_id, rule in sorted(acquisition_rules.items(), key=lambda item: AcquisitionRef.parse(item[0])):
        target_rows = target_by_acquisition.get(acquisition_id, [])
        dependency_rows = dependency_by_acquisition.get(acquisition_id, [])
        if not target_rows and not dependency_rows:
            continue
        facts = costs.get(acquisition_id)
        if facts is None or facts["sample_count"] < 1 or facts["maximum_response_bytes"] < 1:
            raise ValueError(
                f"missing latency/response-size facts: verify {acquisition_id} first"
            )
        target = min(
            [rule["target_interval_ms"]]
            + [row["maximum_age_ms"] for row in dependency_rows]
        )
        maximum = min(
            [rule["maximum_interval_ms"]]
            + [row["maximum_age_ms"] for row in dependency_rows]
        )
        if target < floor or maximum < floor:
            raise ValueError(f"acquisition interval violates request-start floor: {acquisition_id}")
        response_frames = max(1, math.ceil(facts["maximum_response_bytes"] / 7))
        estimated_bits = (1 + response_frames) * 128
        ref = AcquisitionRef.parse(acquisition_id)
        load = load_by_endpoint.setdefault(
            ref.endpoint_id,
            {
                "request_rate_hz": 0.0,
                "bus_utilization": 0.0,
                "session_utilization": 0.0,
                "stress_session_utilization": 0.0,
            },
        )
        load["request_rate_hz"] += 1000 / target
        load["bus_utilization"] += estimated_bits * (1000 / target) / transport["network"]["bitrate"]
        load["session_utilization"] += facts["latency_p95_ms"] / target
        load["stress_session_utilization"] += safety["response_pending_budget_ms"] / maximum
        acquisitions.append(
            {
                "acquisition_id": acquisition_id,
                "endpoint_id": ref.endpoint_id,
                "data_id": f"0x{ref.data_id:04X}",
                "priority": max(
                    [rule["priority"]] + [row["priority"] for row in dependency_rows]
                ),
                "target_interval_ms": target,
                "maximum_interval_ms": maximum,
                "cost": facts,
                "request_hex": f"22 {ref.data_id >> 8:02X} {ref.data_id & 0xFF:02X}",
                "objective_ids": sorted(
                    (row["signal_ref"] for row in target_rows), key=SignalRef.parse
                ),
                "dependency_for": sorted(
                    {row["target"] for row in dependency_rows}, key=SignalRef.parse
                ),
            }
        )
    for endpoint_id, load in sorted(load_by_endpoint.items()):
        if load["request_rate_hz"] > maximum_rate:
            raise ValueError(
                f"endpoint {endpoint_id} request rate {load['request_rate_hz']:.3f} Hz "
                f"exceeds tracked limit {maximum_rate:.3f} Hz"
            )
        if load["bus_utilization"] > float(safety["maximum_estimated_bus_utilization"]):
            raise ValueError(
                f"endpoint {endpoint_id} estimated bus utilization {load['bus_utilization']:.6f} "
                f"exceeds tracked limit {safety['maximum_estimated_bus_utilization']:.6f}"
            )
        if load["session_utilization"] > float(safety["maximum_nominal_session_utilization"]):
            raise ValueError(
                f"endpoint {endpoint_id} nominal session utilization "
                f"{load['session_utilization']:.6f} exceeds tracked limit "
                f"{safety['maximum_nominal_session_utilization']:.6f}"
            )

    endpoint_sessions = []
    dynamic_by_endpoint: dict[str, list[dict[str, Any]]] = {}
    for dynamic in transport["dynamic_dids"]:
        dynamic_by_endpoint.setdefault(dynamic["endpoint_id"], []).append({
            "request_hex": dynamic["definition_hex"],
            "expected_prefix_hex": dynamic["expected_prefix_hex"],
            "label": f"define dynamic DID {dynamic['target_did']}",
            "tolerate_negative": False,
        })
    for endpoint_id in sorted({row["endpoint_id"] for row in acquisitions}):
        endpoint = endpoint_rows[endpoint_id]
        recipe = recipe_rows[endpoint["session_recipe_id"]]
        endpoint_sessions.append(
            {
                "endpoint_id": endpoint_id,
                "tester_can_id": endpoint["tester_can_id"],
                "response_can_id": endpoint["response_can_id"],
                "session_recipe_id": recipe["id"],
                "network": transport["network"],
                "request_start_floor_ms": safety["request_start_floor_ms"],
                "initial_response_timeout_ms": safety["initial_response_timeout_ms"],
                "response_pending_budget_ms": safety["response_pending_budget_ms"],
                "transport_retries": safety["transport_retries"],
                "session_setup": [
                    dict(step) for step in recipe["steps"]
                ] + sorted(
                    dynamic_by_endpoint.get(endpoint_id, []),
                    key=lambda row: row["request_hex"],
                ),
            }
        )
    stress = {
        "endpoint_loads": [
            {"endpoint_id": endpoint_id, **load}
            for endpoint_id, load in sorted(load_by_endpoint.items())
        ],
        "target_deadlines_guaranteed": all(
            load["request_rate_hz"] <= min(maximum_rate, 1000 / floor)
            and load["stress_session_utilization"] <= 1.0
            for load in load_by_endpoint.values()
        ),
        "response_pending_budget_ms": safety["response_pending_budget_ms"],
    }
    maximum_bus_utilization = max(
        (load["bus_utilization"] for load in load_by_endpoint.values()), default=0.0
    )
    maximum_session_utilization = max(
        (load["session_utilization"] for load in load_by_endpoint.values()), default=0.0
    )
    decodes = _compile_decodes(repository, compiled_targets)
    body = {
        "document_type": "telemetry.admitted-plan",
        "schema_version": 1,
        "project_id": repository.project_id,
        "profile_id": repository.profile_id,
        "created_utc": created_utc,
        "source_hashes": repository.cached_source_hashes(),
        "admitted": True,
        "live_authorized": True,
        "admission": {
            "available_disk_mib": available_disk_mib,
            "required_disk_mib": required_disk,
            "maximum_estimated_bus_utilization": maximum_bus_utilization,
            "maximum_nominal_session_utilization": maximum_session_utilization,
        },
        "stress": stress,
        "storage": storage,
        "endpoint_sessions": endpoint_sessions,
        "acquisitions": acquisitions,
        "objectives": compiled_targets,
        "decodes": decodes,
    }
    body["content_sha256"] = hashlib.sha256(_json_bytes(body)).hexdigest()
    encoded = _json_bytes(body)
    file_sha256 = hashlib.sha256(encoded).hexdigest()
    if output is not None:
        _atomic_write(output, encoded)
    return body, file_sha256


def _compile_decodes(
    repository: ProjectRepository, targets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    required = {row["signal_ref"] for row in targets}
    for target in targets:
        required.update(target["condition_dependencies"])
    by_signal: dict[str, list[dict[str, Any]]] = {}
    for row in repository.catalog_variants():
        by_signal.setdefault(row["signal_ref"], []).append(row)
    reviews = {row["signal_ref"]: row for row in repository.review["signals"]}
    endpoint_by_ecu = {
        int(ecu): endpoint["id"]
        for endpoint in repository.transport["endpoints"]
        for ecu in endpoint["ecu_keys"]
    }
    result = []
    for signal_ref in sorted(required, key=SignalRef.parse):
        variants = by_signal.get(signal_ref, [])
        if not variants:
            raise ValueError(f"collection decode does not resolve catalog signal: {signal_ref}")
        review = reviews.get(signal_ref)
        selected = None
        if review is not None and review["catalog_variant_id"] is not None:
            selected = next(
                (row for row in variants if row["catalog_variant_id"] == review["catalog_variant_id"]),
                None,
            )
        if selected is None:
            formulas = {row.get("formula", "raw") for row in variants}
            if len(formulas) != 1 and not (review and review["formula_override"] is not None):
                raise ValueError(f"collection decode formula is ambiguous: {signal_ref}")
            selected = min(variants, key=lambda row: row["catalog_variant_id"])
        ref = SignalRef.parse(signal_ref)
        formula = (
            review["formula_override"]
            if review is not None and review["formula_override"] is not None
            else selected.get("formula", "raw")
        )
        result.append({
            "acquisition_id": f"{endpoint_by_ecu[ref.ecu_key]}/0x{ref.data_id:04X}",
            "signal_ref": signal_ref,
            "first_bit": ref.first_bit,
            "last_bit": ref.last_bit,
            "formula": formula,
        })
    return result
