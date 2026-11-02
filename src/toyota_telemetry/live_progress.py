"""Conservative in-run target progress for scheduler feedback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .objectives import requirement_deficit, requirements_met


STATE_REQUIREMENTS = frozenset({"required_states", "minimum_transitions"})


@dataclass(frozen=True)
class AcquisitionProgress:
    active: bool
    background: bool
    deficit: float
    opportunity: float


class _TargetState:
    def __init__(self, target: Mapping[str, Any]) -> None:
        self.target = dict(target)
        prior = target.get("prior_progress") or {}
        counters = prior.get("counters") or {}
        self.samples = int(counters.get("samples", 0))
        self.changes = int(counters.get("changes", 0))
        self.transitions = int(counters.get("transitions", 0))
        self.states = set(int(value) for value in counters.get("states", []))
        self.prior_runs = int(counters.get("runs", 0))
        self.prior_applicable_seconds = float(counters.get("applicable_seconds", 0))
        self.prior_span_seconds = float(counters.get("span_seconds", 0))
        self.credited_this_run = False
        self.first_applicable_ms: int | None = None
        self.last_applicable_ms: int | None = None
        self.last_raw: int | None = None
        self.last_decoded: float | None = None
        self.condition = "true" if target["condition"] is None else "unknown"
        self.opportunity = 1.0

    @property
    def key(self) -> str:
        return self.target["signal_ref"]

    def observe(
        self, raw_value: int, decoded_value: float | None, condition: str, elapsed_ms: int
    ) -> None:
        self.condition = condition
        previous_raw = self.last_raw
        previous_decoded = self.last_decoded
        is_new_state = raw_value not in self.states
        changed = previous_raw is not None and previous_raw != raw_value
        self.last_raw = raw_value
        self.last_decoded = decoded_value
        if condition != "true":
            self.opportunity = 0.25
            return
        self.samples += 1
        self.credited_this_run = True
        self.states.add(raw_value)
        if self.first_applicable_ms is None:
            self.first_applicable_ms = elapsed_ms
        self.last_applicable_ms = elapsed_ms
        qualifying_change = False
        if changed:
            self.transitions += 1
            difference = self.target.get("minimum_difference")
            if (
                difference is not None
                and previous_decoded is not None
                and decoded_value is not None
                and abs(decoded_value - previous_decoded) >= float(difference)
            ):
                self.changes += 1
                qualifying_change = True
        requirements = self.target["requirements"]
        if STATE_REQUIREMENTS & requirements.keys():
            self.opportunity = 1.0 if is_new_state or changed else 0.25
        elif "minimum_changes" in requirements:
            self.opportunity = 1.0 if qualifying_change else 0.25
        else:
            self.opportunity = 1.0

    def counters(self) -> dict[str, Any]:
        current_span = 0.0
        if self.first_applicable_ms is not None and self.last_applicable_ms is not None:
            current_span = (self.last_applicable_ms - self.first_applicable_ms) / 1000
        return {
            "samples": self.samples,
            "runs": self.prior_runs + (1 if self.credited_this_run else 0),
            "span_seconds": max(self.prior_span_seconds, current_span),
            "states": sorted(self.states),
            "transitions": self.transitions,
            "changes": self.changes,
            "applicable_seconds": self.prior_applicable_seconds + current_span,
        }

    def achieved(self) -> bool:
        return all(requirements_met(self.target["requirements"], self.counters()).values())

    def deficit(self) -> float:
        return requirement_deficit(self.target["requirements"], self.counters())


class LiveTargetLedger:
    """Advance only counters that can be proven from the current run."""

    def __init__(self, targets: Iterable[Mapping[str, Any]]) -> None:
        self.states = {
            state.key: state for state in (_TargetState(target) for target in targets)
        }

    def observe(
        self,
        signal_ref: str,
        raw_value: int,
        decoded_value: float | None,
        condition: str,
        elapsed_ms: int,
    ) -> None:
        state = self.states.get(signal_ref)
        if state is not None:
            state.observe(raw_value, decoded_value, condition, elapsed_ms)

    def set_condition(self, signal_ref: str, condition: str) -> None:
        if condition not in {"true", "false", "unknown"}:
            raise ValueError("live condition state is invalid")
        self.states[signal_ref].condition = condition

    def all_complete(self, keys: Iterable[str] | None = None) -> bool:
        states = self._select(keys)
        return bool(states) and all(state.achieved() for state in states)

    def summary(self, keys: Iterable[str] | None = None) -> dict[str, int]:
        states = self._select(keys)
        complete = sum(1 for state in states if state.achieved())
        return {"targets": len(states), "complete": complete}

    def _select(self, keys: Iterable[str] | None) -> list[_TargetState]:
        if keys is None:
            return list(self.states.values())
        return [self.states[key] for key in keys]

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
            state.target["condition"] is None or state.condition == "true"
            for state in unfinished_direct
        )
        return AcquisitionProgress(active, active and not foreground, deficit, opportunity)
