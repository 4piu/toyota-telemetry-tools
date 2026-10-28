"""Deterministic collection-target and condition replay."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Iterable, Mapping

from .identity import SignalRef


@dataclass(frozen=True)
class ReplayObservation:
    run_id: str
    run_sequence: int
    response_elapsed_ms: int
    response_utc: str | None
    signal_ref: str
    raw_value: int
    decoded_value: float | None
    segment_index: int = 1


def evaluate_condition(
    node: Mapping[str, Any],
    context: Mapping[str, ReplayObservation],
    run_id: str,
    elapsed_ms: int,
) -> str:
    """Return ``true``, ``false``, or ``unknown`` for one condition tree."""

    if "all" in node:
        states = [
            evaluate_condition(child, context, run_id, elapsed_ms)
            for child in node["all"]
        ]
        if "false" in states:
            return "false"
        return "unknown" if "unknown" in states else "true"
    if "any" in node:
        states = [
            evaluate_condition(child, context, run_id, elapsed_ms)
            for child in node["any"]
        ]
        if "true" in states:
            return "true"
        return "unknown" if "unknown" in states else "false"

    observation = context.get(str(SignalRef.parse(node["signal_ref"])))
    if (
        observation is None
        or observation.run_id != run_id
        or elapsed_ms < observation.response_elapsed_ms
        or elapsed_ms - observation.response_elapsed_ms > int(node["maximum_age_ms"])
    ):
        return "unknown"
    actual: Any = observation.decoded_value
    if actual is None or (isinstance(actual, float) and not math.isfinite(actual)):
        return "unknown"
    expected = node["value"]
    operator = node["operator"]
    try:
        if operator == "eq":
            result = actual == expected
        elif operator == "ne":
            result = actual != expected
        elif operator == "lt":
            result = actual < expected
        elif operator == "lte":
            result = actual <= expected
        elif operator == "gt":
            result = actual > expected
        elif operator == "gte":
            result = actual >= expected
        elif operator == "in":
            result = actual in expected
        else:
            raise ValueError(f"unsupported condition operator: {operator}")
    except TypeError:
        return "unknown"
    return "true" if result else "false"


def condition_refs(node: Mapping[str, Any]) -> set[str]:
    if "all" in node:
        return set().union(*(condition_refs(child) for child in node["all"]))
    if "any" in node:
        return set().union(*(condition_refs(child) for child in node["any"]))
    return {str(SignalRef.parse(node["signal_ref"]))}


def condition_leaves(node: Mapping[str, Any]) -> list[tuple[str, int]]:
    if "all" in node:
        return [item for child in node["all"] for item in condition_leaves(child)]
    if "any" in node:
        return [item for child in node["any"] for item in condition_leaves(child)]
    return [(str(SignalRef.parse(node["signal_ref"])), int(node["maximum_age_ms"]))]


def requirement_values(counters: Mapping[str, Any]) -> dict[str, Any]:
    """Map recorded counters onto the requirement names they satisfy."""

    return {
        "minimum_samples": counters["samples"],
        "minimum_runs": counters["runs"],
        "minimum_span_seconds": counters["span_seconds"],
        "required_states": set(counters["states"]),
        "minimum_transitions": counters["transitions"],
        "minimum_changes": counters["changes"],
        "minimum_applicable_seconds": counters["applicable_seconds"],
    }


def requirements_met(
    requirements: Mapping[str, Any], counters: Mapping[str, Any]
) -> dict[str, bool]:
    values = requirement_values(counters)
    met = {}
    for name, required in requirements.items():
        actual = values[name]
        if name == "required_states":
            met[name] = set(required).issubset(actual)
        else:
            met[name] = actual >= required
    return met


def requirement_deficit(requirements: Mapping[str, Any], counters: Mapping[str, Any]) -> float:
    """Return the largest unmet fraction across requirements, from 0 to 1."""

    values = requirement_values(counters)
    deficits = []
    for name, required in requirements.items():
        actual = values[name]
        if name == "required_states":
            wanted = set(required)
            deficits.append(len(wanted - actual) / len(wanted))
        else:
            deficits.append(max(0.0, (float(required) - float(actual)) / float(required)))
    return max(deficits, default=0.0)


def replay_targets(
    targets: Iterable[Mapping[str, Any]],
    observations: Iterable[ReplayObservation],
) -> list[dict[str, Any]]:
    """Replay observations into target progress without mutating review."""

    target_rows = sorted(
        (dict(row) for row in targets), key=lambda row: SignalRef.parse(row["signal_ref"])
    )
    by_exchange: dict[tuple[str, int], list[ReplayObservation]] = defaultdict(list)
    for observation in observations:
        by_exchange[(observation.run_id, observation.run_sequence)].append(observation)

    state = {row["signal_ref"]: _new_state() for row in target_rows}
    targets_by_signal = {row["signal_ref"]: row for row in target_rows}

    context: dict[str, ReplayObservation] = {}
    prior: dict[tuple[str, str], ReplayObservation] = {}
    current_run: str | None = None
    for (run_id, _sequence), exchange_observations in sorted(by_exchange.items()):
        if run_id != current_run:
            context.clear()
            current_run = run_id
        ordered = sorted(exchange_observations, key=lambda item: SignalRef.parse(item.signal_ref))
        for observation in ordered:
            context[observation.signal_ref] = observation
        for observation in ordered:
            target = targets_by_signal.get(observation.signal_ref)
            if target is None:
                continue
            condition = (
                "true"
                if target["condition"] is None
                else evaluate_condition(
                    target["condition"], context, run_id, observation.response_elapsed_ms
                )
            )
            progress = state[target["signal_ref"]]
            progress["condition_counts"][condition] += 1
            previous = prior.get((run_id, observation.signal_ref))
            prior[(run_id, observation.signal_ref)] = observation
            if condition != "true":
                continue
            _credit(progress, target, observation, previous)

    return [_finish(row, state[row["signal_ref"]]) for row in target_rows]


def _new_state() -> dict[str, Any]:
    return {
        "samples": 0,
        "changes": 0,
        "transitions": 0,
        "states": set(),
        "runs": set(),
        "span_by_run": {},
        "utc": [],
        "condition_counts": {"true": 0, "false": 0, "unknown": 0},
    }


def _credit(
    progress: dict[str, Any],
    target: Mapping[str, Any],
    observation: ReplayObservation,
    previous: ReplayObservation | None,
) -> None:
    progress["samples"] += 1
    progress["states"].add(observation.raw_value)
    progress["runs"].add(observation.run_id)
    first, last = progress["span_by_run"].get(
        observation.run_id,
        (observation.response_elapsed_ms, observation.response_elapsed_ms),
    )
    progress["span_by_run"][observation.run_id] = (
        min(first, observation.response_elapsed_ms),
        max(last, observation.response_elapsed_ms),
    )
    if observation.response_utc is not None:
        try:
            progress["utc"].append(
                datetime.fromisoformat(observation.response_utc.replace("Z", "+00:00"))
            )
        except ValueError:
            pass
    if previous is None or previous.raw_value == observation.raw_value:
        return
    progress["transitions"] += 1
    difference = target.get("minimum_difference")
    if difference is None:
        return
    if previous.decoded_value is not None and observation.decoded_value is not None:
        if abs(observation.decoded_value - previous.decoded_value) >= float(difference):
            progress["changes"] += 1


def _finish(target: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    applicable_ms = sum(last - first for first, last in state["span_by_run"].values())
    span = 0.0
    if len(state["utc"]) >= 2:
        span = (max(state["utc"]) - min(state["utc"])).total_seconds()
    elif state["span_by_run"]:
        span = max(last - first for first, last in state["span_by_run"].values()) / 1000
    counters = {
        "samples": state["samples"],
        "runs": len(state["runs"]),
        "span_seconds": span,
        "states": sorted(state["states"]),
        "transitions": state["transitions"],
        "changes": state["changes"],
        "applicable_seconds": applicable_ms / 1000,
    }
    met = requirements_met(target["requirements"], counters)
    return {
        "signal_ref": target["signal_ref"],
        "included": target["included"],
        "condition_counts": dict(state["condition_counts"]),
        "counters": counters,
        "requirements_met": met,
        "complete": bool(target["included"] and met and all(met.values())),
    }
