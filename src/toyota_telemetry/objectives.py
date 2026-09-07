"""Deterministic objective and applicability replay."""

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


@dataclass(frozen=True)
class ReplayRun:
    run_id: str
    independence: str


def evaluate_applicability(
    node: Mapping[str, Any],
    context: Mapping[str, ReplayObservation],
    run_id: str,
    elapsed_ms: int,
) -> str:
    """Return ``true``, ``false``, or ``unknown`` for one policy tree."""

    if "all" in node:
        states = [
            evaluate_applicability(child, context, run_id, elapsed_ms)
            for child in node["all"]
        ]
        if "false" in states:
            return "false"
        return "unknown" if "unknown" in states else "true"
    if "any" in node:
        states = [
            evaluate_applicability(child, context, run_id, elapsed_ms)
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
            raise ValueError(f"unsupported applicability operator: {operator}")
    except TypeError:
        return "unknown"
    return "true" if result else "false"


def replay_objectives(
    objectives: Iterable[Mapping[str, Any]],
    runs: Iterable[ReplayRun],
    observations: Iterable[ReplayObservation],
) -> list[dict[str, Any]]:
    """Replay observations without mutating semantic review conclusions."""

    objective_rows = sorted(
        (dict(row) for row in objectives),
        key=lambda row: (SignalRef.parse(row["signal_ref"]), row["objective_id"]),
    )
    independence = {run.run_id: run.independence for run in runs}
    by_exchange: dict[tuple[str, int], list[ReplayObservation]] = defaultdict(list)
    for observation in observations:
        by_exchange[(observation.run_id, observation.run_sequence)].append(observation)

    state = {
        (row["signal_ref"], row["objective_id"]): _new_state()
        for row in objective_rows
    }
    objectives_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in objective_rows:
        objectives_by_signal[row["signal_ref"]].append(row)

    context: dict[str, ReplayObservation] = {}
    prior: dict[tuple[str, str], ReplayObservation] = {}
    current_run: str | None = None
    for (run_id, sequence), exchange_observations in sorted(by_exchange.items()):
        if run_id != current_run:
            context.clear()
            current_run = run_id
        ordered = sorted(exchange_observations, key=lambda item: SignalRef.parse(item.signal_ref))
        for observation in ordered:
            context[observation.signal_ref] = observation
        for observation in ordered:
            for objective in objectives_by_signal.get(observation.signal_ref, []):
                key = (objective["signal_ref"], objective["objective_id"])
                applicability = (
                    "true"
                    if objective["applicability"] is None
                    else evaluate_applicability(
                        objective["applicability"],
                        context,
                        run_id,
                        observation.response_elapsed_ms,
                    )
                )
                progress = state[key]
                progress["applicability_counts"][applicability] += 1
                previous = prior.get((run_id, observation.signal_ref))
                prior[(run_id, observation.signal_ref)] = observation
                if applicability != "true":
                    continue
                _credit(progress, objective, observation, previous, independence)

    return [_finish(row, state[(row["signal_ref"], row["objective_id"])]) for row in objective_rows]


def _new_state() -> dict[str, Any]:
    return {
        "total": 0,
        "changed": 0,
        "transitions": 0,
        "distinct": set(),
        "states": set(),
        "reviewed_runs": set(),
        "span_by_run": {},
        "utc": [],
        "last_offset": {},
        "intervals": [],
        "applicability_counts": Counter3(),
    }


class Counter3(dict[str, int]):
    def __init__(self) -> None:
        super().__init__({"true": 0, "false": 0, "unknown": 0})


def _credit(
    progress: dict[str, Any],
    objective: Mapping[str, Any],
    observation: ReplayObservation,
    previous: ReplayObservation | None,
    independence: Mapping[str, str],
) -> None:
    progress["total"] += 1
    progress["distinct"].add(observation.raw_value)
    progress["states"].add(observation.raw_value)
    if independence.get(observation.run_id) == "reviewed_independent":
        progress["reviewed_runs"].add(observation.run_id)
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
            progress["utc"].append(datetime.fromisoformat(observation.response_utc.replace("Z", "+00:00")))
        except ValueError:
            pass
    last_offset = progress["last_offset"].get(observation.run_id)
    if last_offset is not None:
        progress["intervals"].append(observation.response_elapsed_ms - last_offset)
    progress["last_offset"][observation.run_id] = observation.response_elapsed_ms
    if previous is None or previous.raw_value == observation.raw_value:
        return
    progress["transitions"] += 1
    if "minimum_changed_observations" not in objective["targets"]:
        return
    deadband = objective.get("deadband")
    if deadband is None:
        return
    if previous.decoded_value is not None and observation.decoded_value is not None:
        if abs(observation.decoded_value - previous.decoded_value) >= float(deadband):
            progress["changed"] += 1


def _finish(objective: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    applicable_ms = sum(last - first for first, last in state["span_by_run"].values())
    utc_span = 0.0
    if len(state["utc"]) >= 2:
        utc_span = (max(state["utc"]) - min(state["utc"])).total_seconds()
    elif state["span_by_run"]:
        utc_span = max(last - first for first, last in state["span_by_run"].values()) / 1000
    intervals = state["intervals"]
    counters = {
        "total_observations": state["total"],
        "changed_observations": state["changed"],
        "distinct_raw_values": len(state["distinct"]),
        "transitions": state["transitions"],
        "raw_states": sorted(state["states"]),
        "applicable_seconds": applicable_ms / 1000,
        "time_span_seconds": utc_span,
        "distinct_reviewed_runs": len(state["reviewed_runs"]),
        "intervals": {
            "count": len(intervals),
            "maximum_ms": max(intervals) if intervals else None,
            "within_target": sum(value <= objective["target_interval_ms"] for value in intervals),
            "within_maximum": sum(value <= objective["maximum_interval_ms"] for value in intervals),
        },
    }
    achieved: dict[str, bool] = {}
    for target, required in objective["targets"].items():
        if target == "minimum_total_observations":
            achieved[target] = counters["total_observations"] >= required
        elif target == "minimum_changed_observations":
            achieved[target] = counters["changed_observations"] >= required
        elif target == "minimum_distinct_raw_values":
            achieved[target] = counters["distinct_raw_values"] >= required
        elif target == "maximum_distinct_raw_values":
            achieved[target] = counters["distinct_raw_values"] <= required and counters["total_observations"] > 0
        elif target == "minimum_transitions":
            achieved[target] = counters["transitions"] >= required
        elif target == "required_raw_states":
            achieved[target] = set(required).issubset(state["states"])
        elif target == "minimum_applicable_seconds":
            achieved[target] = counters["applicable_seconds"] >= required
        elif target == "minimum_time_span_seconds":
            achieved[target] = counters["time_span_seconds"] >= required
        elif target == "minimum_distinct_runs":
            achieved[target] = counters["distinct_reviewed_runs"] >= required
    return {
        "signal_ref": objective["signal_ref"],
        "objective_id": objective["objective_id"],
        "class": objective["class"],
        "included": objective["included"],
        "applicability_counts": dict(state["applicability_counts"]),
        "counters": counters,
        "targets_achieved": achieved,
        "complete": bool(objective["included"] and achieved and all(achieved.values())),
    }
