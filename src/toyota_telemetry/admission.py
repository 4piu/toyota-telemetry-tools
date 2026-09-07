"""Offline compilation and safety admission for collection plans."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .identity import AcquisitionRef, SignalRef
from .repository import ProjectRepository


REQUIRED_TARGETS = {
    "descriptive": ({"minimum_total_observations", "minimum_distinct_runs"},),
    "snapshot": ({"minimum_total_observations", "minimum_distinct_runs"},),
    "switch_or_enum": ({"required_raw_states"}, {"minimum_transitions"}),
    "fast_dynamic": ({"minimum_changed_observations", "minimum_applicable_seconds"},),
    "slow_trend": ({"minimum_time_span_seconds", "minimum_total_observations"},),
    "counter_or_history": ({"minimum_distinct_runs", "minimum_total_observations"},),
    "exploration": ({"minimum_total_observations"},),
}


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


def validate_objective_shape(objective: Mapping[str, Any]) -> None:
    target_keys = set(objective["targets"])
    choices = REQUIRED_TARGETS[objective["class"]]
    if not any(required <= target_keys for required in choices):
        alternatives = " or ".join("+".join(sorted(required)) for required in choices)
        raise ValueError(
            f"{objective['signal_ref']}#{objective['objective_id']} lacks required "
            f"{objective['class']} targets: {alternatives}"
        )
    if objective["class"] == "fast_dynamic" and objective.get("deadband") is None:
        raise ValueError(
            f"{objective['signal_ref']}#{objective['objective_id']} requires a reviewed deadband"
        )


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
    progress_by_objective = {
        (row["signal_ref"], row["objective_id"]): row
        for row in repository.replay_progress()
    }
    compiled_objectives = []
    objective_by_acquisition: dict[str, list[dict[str, Any]]] = {}
    dependency_by_acquisition: dict[str, list[dict[str, Any]]] = {}
    for authored in policy["objectives"]:
        if not authored["included"]:
            continue
        default = policy["class_defaults"].get(authored["class"], {})
        merged = {**default, **authored}
        merged["targets"] = {**default.get("targets", {}), **authored["targets"]}
        validate_objective_shape(merged)
        prior_progress = progress_by_objective.get(
            (merged["signal_ref"], merged["objective_id"])
        )
        if prior_progress is not None and prior_progress["complete"]:
            continue
        signal = SignalRef.parse(merged["signal_ref"])
        endpoint = endpoint_by_ecu[signal.ecu_key]
        acquisition_id = f"{endpoint['id']}/0x{signal.data_id:04X}"
        if acquisition_id not in acquisition_rules:
            raise ValueError(f"objective has no included acquisition rule: {merged['signal_ref']}")
        dependencies = sorted(_applicability_refs(merged["applicability"])) if merged["applicability"] else []
        compiled = {
            **merged,
            "acquisition_id": acquisition_id,
            "applicability_dependencies": dependencies,
            "prior_progress": prior_progress,
        }
        compiled_objectives.append(compiled)
        objective_by_acquisition.setdefault(acquisition_id, []).append(compiled)
        if merged["applicability"] is not None:
            for dependency_ref, maximum_age_ms in _applicability_leaves(merged["applicability"]):
                dependency = SignalRef.parse(dependency_ref)
                dependency_endpoint = endpoint_by_ecu[dependency.ecu_key]
                dependency_id = f"{dependency_endpoint['id']}/0x{dependency.data_id:04X}"
                if dependency_id not in acquisition_rules:
                    raise ValueError(
                        f"applicability dependency has no included acquisition rule: {dependency_ref}"
                    )
                dependency_by_acquisition.setdefault(dependency_id, []).append(
                    {
                        "objective": f"{merged['signal_ref']}#{merged['objective_id']}",
                        "maximum_age_ms": maximum_age_ms,
                        "priority": merged["priority"],
                    }
                )

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
        objective_rows = objective_by_acquisition.get(acquisition_id, [])
        dependency_rows = dependency_by_acquisition.get(acquisition_id, [])
        if not objective_rows and not dependency_rows:
            continue
        facts = costs.get(acquisition_id)
        if facts is None or facts["sample_count"] < 1 or facts["maximum_response_bytes"] < 1:
            raise ValueError(f"missing latency/response-size cost facts: {acquisition_id}")
        target = min(
            [rule["target_interval_ms"]]
            + [row["target_interval_ms"] for row in objective_rows]
            + [row["maximum_age_ms"] for row in dependency_rows]
        )
        maximum = min(
            [rule["maximum_interval_ms"]]
            + [row["maximum_interval_ms"] for row in objective_rows]
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
                    [rule["priority"]]
                    + [row["priority"] for row in objective_rows]
                    + [row["priority"] for row in dependency_rows]
                ),
                "target_interval_ms": target,
                "maximum_interval_ms": maximum,
                "cost": facts,
                "request_hex": f"22 {ref.data_id >> 8:02X} {ref.data_id & 0xFF:02X}",
                "objective_ids": sorted(
                    f"{row['signal_ref']}#{row['objective_id']}"
                    for row in objective_rows
                ),
                "dependency_for": sorted(row["objective"] for row in dependency_rows),
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
    dynamic_by_endpoint: dict[str, list[str]] = {}
    for dynamic in transport["dynamic_dids"]:
        dynamic_by_endpoint.setdefault(dynamic["endpoint_id"], []).append(dynamic["definition_hex"])
    for endpoint_id in sorted({row["endpoint_id"] for row in acquisitions}):
        endpoint = endpoint_rows[endpoint_id]
        recipe = recipe_rows[endpoint["session_recipe_id"]]
        endpoint_sessions.append(
            {
                "endpoint_id": endpoint_id,
                "tester_can_id": endpoint["tester_can_id"],
                "response_can_id": endpoint["response_can_id"],
                "session_recipe_id": recipe["id"],
                "session_request_hex": [step["request_hex"] for step in recipe["steps"]],
                "dynamic_definition_hex": sorted(dynamic_by_endpoint.get(endpoint_id, [])),
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
        "objectives": compiled_objectives,
    }
    body["content_sha256"] = hashlib.sha256(_json_bytes(body)).hexdigest()
    encoded = _json_bytes(body)
    file_sha256 = hashlib.sha256(encoded).hexdigest()
    if output is not None:
        _atomic_write(output, encoded)
    return body, file_sha256


def _applicability_refs(node: Mapping[str, Any]) -> set[str]:
    if "all" in node:
        return set().union(*(_applicability_refs(child) for child in node["all"]))
    if "any" in node:
        return set().union(*(_applicability_refs(child) for child in node["any"]))
    return {str(SignalRef.parse(node["signal_ref"]))}


def _applicability_leaves(node: Mapping[str, Any]) -> list[tuple[str, int]]:
    if "all" in node:
        return [item for child in node["all"] for item in _applicability_leaves(child)]
    if "any" in node:
        return [item for child in node["any"] for item in _applicability_leaves(child)]
    return [(str(SignalRef.parse(node["signal_ref"])), int(node["maximum_age_ms"]))]
