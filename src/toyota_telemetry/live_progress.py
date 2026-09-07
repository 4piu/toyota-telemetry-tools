"""Conservative in-run objective progress for scheduler feedback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class AcquisitionProgress:
    active: bool
    background: bool
    deficit: float
    opportunity: float


class _ObjectiveState:
    def __init__(self, objective: Mapping[str, Any]) -> None:
        self.objective = dict(objective)
        prior = objective.get("prior_progress") or {}
        counters = prior.get("counters") or {}
        self.total = int(counters.get("total_observations", 0))
        self.changed = int(counters.get("changed_observations", 0))
        self.transitions = int(counters.get("transitions", 0))
        self.states = set(int(value) for value in counters.get("raw_states", []))
        self.prior_applicable_seconds = float(counters.get("applicable_seconds", 0))
        self.prior_time_span_seconds = float(counters.get("time_span_seconds", 0))
        self.distinct_runs = int(counters.get("distinct_reviewed_runs", 0))
        self.first_applicable_ms: int | None = None
        self.last_applicable_ms: int | None = None
        self.first_observation_ms: int | None = None
        self.last_observation_ms: int | None = None
        self.last_raw: int | None = None
        self.last_decoded: float | None = None
        self.applicability = "true" if objective["applicability"] is None else "unknown"
        self.opportunity = 1.0

    @property
    def key(self) -> str:
        return f"{self.objective['signal_ref']}#{self.objective['objective_id']}"

    def observe(
        self, raw_value: int, decoded_value: float | None, applicability: str, elapsed_ms: int
    ) -> None:
        self.applicability = applicability
        previous_raw = self.last_raw
        previous_decoded = self.last_decoded
        is_new_state = raw_value not in self.states
        changed = previous_raw is not None and previous_raw != raw_value
        self.last_raw = raw_value
        self.last_decoded = decoded_value
        if applicability != "true":
            self.opportunity = 0.25
            return
        self.total += 1
        self.states.add(raw_value)
        if self.first_applicable_ms is None:
            self.first_applicable_ms = elapsed_ms
        self.last_applicable_ms = elapsed_ms
        if self.first_observation_ms is None:
            self.first_observation_ms = elapsed_ms
        self.last_observation_ms = elapsed_ms
        if changed:
            self.transitions += 1
            deadband = self.objective.get("deadband")
            if (
                "minimum_changed_observations" in self.objective["targets"]
                and deadband is not None
                and previous_decoded is not None
                and decoded_value is not None
                and abs(decoded_value - previous_decoded) >= float(deadband)
            ):
                self.changed += 1
        class_name = self.objective["class"]
        if class_name == "fast_dynamic":
            self.opportunity = 1.0 if changed else 0.25
        elif class_name == "switch_or_enum":
            self.opportunity = 1.0 if is_new_state else 0.25
        else:
            self.opportunity = 1.0

    def values(self) -> dict[str, Any]:
        applicable = self.prior_applicable_seconds
        if self.first_applicable_ms is not None and self.last_applicable_ms is not None:
            applicable += (self.last_applicable_ms - self.first_applicable_ms) / 1000
        current_span = 0.0
        if self.first_observation_ms is not None and self.last_observation_ms is not None:
            current_span = (self.last_observation_ms - self.first_observation_ms) / 1000
        return {
            "minimum_total_observations": self.total,
            "minimum_changed_observations": self.changed,
            "minimum_distinct_raw_values": len(self.states),
            "maximum_distinct_raw_values": len(self.states),
            "minimum_transitions": self.transitions,
            "required_raw_states": self.states,
            "minimum_applicable_seconds": applicable,
            "minimum_time_span_seconds": max(self.prior_time_span_seconds, current_span),
            "minimum_distinct_runs": self.distinct_runs,
        }

    def achieved(self) -> bool:
        values = self.values()
        for name, required in self.objective["targets"].items():
            actual = values[name]
            if name == "maximum_distinct_raw_values":
                if self.total == 0 or actual > required:
                    return False
            elif name == "required_raw_states":
                if not set(required).issubset(actual):
                    return False
            elif actual < required:
                return False
        return True

    def deficit(self) -> float:
        values = self.values()
        deficits = []
        for name, required in self.objective["targets"].items():
            actual = values[name]
            if name == "maximum_distinct_raw_values":
                deficits.append(0.0 if self.total and actual <= required else 1.0)
            elif name == "required_raw_states":
                wanted = set(required)
                deficits.append(len(wanted - actual) / len(wanted))
            else:
                deficits.append(max(0.0, (float(required) - float(actual)) / float(required)))
        return max(deficits, default=0.0)


class LiveObjectiveLedger:
    """Advance only counters that can be proven from the current run."""

    def __init__(self, objectives: Iterable[Mapping[str, Any]]) -> None:
        self.states = {
            state.key: state
            for state in (_ObjectiveState(objective) for objective in objectives)
        }
        self.by_signal: dict[str, list[_ObjectiveState]] = {}
        for state in self.states.values():
            self.by_signal.setdefault(state.objective["signal_ref"], []).append(state)

    def observe(
        self,
        signal_ref: str,
        raw_value: int,
        decoded_value: float | None,
        applicability_by_objective: Mapping[str, str],
        elapsed_ms: int,
    ) -> None:
        for state in self.by_signal.get(signal_ref, []):
            state.observe(
                raw_value,
                decoded_value,
                applicability_by_objective[state.key],
                elapsed_ms,
            )

    def set_applicability(self, objective_key: str, applicability: str) -> None:
        if applicability not in {"true", "false", "unknown"}:
            raise ValueError("live applicability state is invalid")
        self.states[objective_key].applicability = applicability

    def all_complete(self) -> bool:
        return bool(self.states) and all(state.achieved() for state in self.states.values())

    def acquisition_progress(self, acquisition: Mapping[str, Any]) -> AcquisitionProgress:
        direct = [self.states[key] for key in acquisition["objective_ids"]]
        dependencies = [self.states[key] for key in acquisition.get("dependency_for", [])]
        unfinished_direct = [state for state in direct if not state.achieved()]
        unfinished_dependencies = [state for state in dependencies if not state.achieved()]
        active = bool(unfinished_direct or unfinished_dependencies)
        relevant = unfinished_direct + unfinished_dependencies
        deficit = max((state.deficit() for state in relevant), default=0.0)
        opportunity = max((state.opportunity for state in relevant), default=1.0)
        foreground = bool(unfinished_dependencies) or any(
            state.objective["applicability"] is None or state.applicability == "true"
            for state in unfinished_direct
        )
        return AcquisitionProgress(active, active and not foreground, deficit, opportunity)
