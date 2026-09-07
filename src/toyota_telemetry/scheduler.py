"""Deterministic, single-endpoint mixed-cadence collection scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .identity import AcquisitionRef


@dataclass(frozen=True)
class SchedulerDecision:
    acquisition_id: str
    reason: str
    selected_at_ms: int
    target_due_ms: int
    maximum_due_ms: int
    score: float


@dataclass
class _State:
    acquisition_id: str
    endpoint_id: str
    data_id: int
    priority: int
    target_interval_ms: int
    maximum_interval_ms: int
    cost_ms: int
    objective_ids: tuple[str, ...]
    target_due_ms: int
    maximum_due_ms: int
    deficit: float = 1.0
    opportunity: float = 1.0
    active: bool = True
    failures: int = 0
    backoff_until_ms: int = 0


class DeterministicScheduler:
    """Select one due acquisition at a time using only supplied state and time.

    Completing a request schedules its next deadline from completion time. This
    intentionally drops missed periods instead of producing a catch-up burst.
    """

    def __init__(
        self,
        acquisitions: Iterable[Mapping[str, Any]],
        *,
        start_ms: int = 0,
        request_start_floor_ms: int = 1,
    ) -> None:
        if start_ms < 0:
            raise ValueError("scheduler start_ms must be nonnegative")
        if request_start_floor_ms < 1:
            raise ValueError("request-start floor must be positive")
        self.request_start_floor_ms = request_start_floor_ms
        self._states: dict[str, _State] = {}
        endpoints = set()
        for row in acquisitions:
            ref = AcquisitionRef.parse(str(row["acquisition_id"]))
            endpoint_id = str(row["endpoint_id"])
            if ref.endpoint_id != endpoint_id:
                raise ValueError(f"scheduler acquisition endpoint mismatch: {ref}")
            target = _positive_int(row["target_interval_ms"], "target interval")
            maximum = _positive_int(row["maximum_interval_ms"], "maximum interval")
            if target > maximum:
                raise ValueError(f"scheduler target exceeds maximum interval: {ref}")
            priority = _bounded_int(row["priority"], "priority", 0, 100)
            cost = _positive_int(row["cost"]["latency_p95_ms"], "p95 cost")
            acquisition_id = str(ref)
            if acquisition_id in self._states:
                raise ValueError(f"duplicate scheduler acquisition: {acquisition_id}")
            endpoints.add(endpoint_id)
            self._states[acquisition_id] = _State(
                acquisition_id=acquisition_id,
                endpoint_id=endpoint_id,
                data_id=ref.data_id,
                priority=priority,
                target_interval_ms=target,
                maximum_interval_ms=maximum,
                cost_ms=cost,
                objective_ids=tuple(sorted(str(value) for value in row["objective_ids"])),
                target_due_ms=start_ms,
                maximum_due_ms=start_ms + maximum,
            )
        if not self._states:
            raise ValueError("scheduler requires at least one acquisition")
        if len(endpoints) != 1:
            raise ValueError("Phase D scheduler accepts exactly one endpoint")
        self.endpoint_id = next(iter(endpoints))
        self._pending: tuple[str, int] | None = None
        self._last_request_start_ms: int | None = None

    @property
    def pending_acquisition_id(self) -> str | None:
        return self._pending[0] if self._pending is not None else None

    def update_acquisition(
        self,
        acquisition_id: str,
        *,
        deficit: float | None = None,
        opportunity: float | None = None,
        active: bool | None = None,
    ) -> None:
        state = self._state(acquisition_id)
        if deficit is not None:
            if not 0.0 <= deficit <= 1.0:
                raise ValueError("scheduler deficit must be between 0 and 1")
            state.deficit = float(deficit)
        if opportunity is not None:
            if opportunity < 0:
                raise ValueError("scheduler opportunity must be nonnegative")
            state.opportunity = float(opportunity)
        if active is not None:
            state.active = bool(active)

    def select(self, now_ms: int, *, slack_ms: int = 0) -> SchedulerDecision | None:
        if now_ms < 0 or slack_ms < 0:
            raise ValueError("scheduler time values must be nonnegative")
        if self._pending is not None:
            raise RuntimeError("scheduler already has an outstanding request")
        if (
            self._last_request_start_ms is not None
            and now_ms - self._last_request_start_ms < self.request_start_floor_ms
        ):
            return None
        eligible = [
            state
            for state in self._states.values()
            if state.active and state.deficit > 0 and now_ms >= state.backoff_until_ms
        ]
        overdue = [state for state in eligible if now_ms >= state.maximum_due_ms]
        if overdue:
            selected = min(overdue, key=lambda state: (state.maximum_due_ms, *self._stable_key(state)))
            reason = "maximum_overdue"
            score = self._score(selected, now_ms)
        else:
            due = [state for state in eligible if now_ms >= state.target_due_ms]
            if due:
                selected = min(
                    due,
                    key=lambda state: (-self._score(state, now_ms), *self._stable_key(state)),
                )
                reason = "target_due"
                score = self._score(selected, now_ms)
            else:
                fitting = [state for state in eligible if state.cost_ms <= slack_ms]
                if not fitting:
                    return None
                selected = min(
                    fitting,
                    key=lambda state: (state.target_due_ms, *self._stable_key(state)),
                )
                reason = "slack_fill"
                score = self._score(selected, now_ms)
        self._pending = (selected.acquisition_id, now_ms)
        self._last_request_start_ms = now_ms
        return SchedulerDecision(
            selected.acquisition_id,
            reason,
            now_ms,
            selected.target_due_ms,
            selected.maximum_due_ms,
            score,
        )

    def complete(self, acquisition_id: str, completed_ms: int, outcome: str) -> None:
        if self._pending is None or self._pending[0] != acquisition_id:
            raise RuntimeError("scheduler completion does not match the outstanding request")
        _, started_ms = self._pending
        if completed_ms < started_ms:
            raise ValueError("scheduler completion precedes request start")
        if outcome not in {"positive", "negative", "timeout", "transport_error", "abandoned"}:
            raise ValueError(f"unsupported scheduler outcome: {outcome}")
        state = self._state(acquisition_id)
        state.target_due_ms = completed_ms + state.target_interval_ms
        state.maximum_due_ms = completed_ms + state.maximum_interval_ms
        if outcome in {"timeout", "transport_error"}:
            state.failures += 1
            delay = min(
                state.maximum_interval_ms,
                self.request_start_floor_ms * (2 ** min(state.failures - 1, 10)),
            )
            state.backoff_until_ms = completed_ms + delay
        else:
            state.failures = 0
            state.backoff_until_ms = completed_ms
        self._pending = None

    def next_due_ms(self) -> int | None:
        candidates = [
            max(state.target_due_ms, state.backoff_until_ms)
            for state in self._states.values()
            if state.active and state.deficit > 0
        ]
        return min(candidates) if candidates else None

    def _score(self, state: _State, now_ms: int) -> float:
        overdue_ms = max(0, now_ms - state.target_due_ms)
        aging = 1.0 + overdue_ms / state.maximum_interval_ms
        return (
            (state.priority + 1)
            * state.deficit
            * state.opportunity
            * aging
            / state.cost_ms
        )

    def _state(self, acquisition_id: str) -> _State:
        try:
            return self._states[str(AcquisitionRef.parse(acquisition_id))]
        except KeyError as exc:
            raise ValueError(f"unknown scheduler acquisition: {acquisition_id}") from exc

    @staticmethod
    def _stable_key(state: _State) -> tuple[str, int]:
        return state.endpoint_id, state.data_id


def _positive_int(value: Any, label: str) -> int:
    return _bounded_int(value, label, 1, None)


def _bounded_int(value: Any, label: str, minimum: int, maximum: int | None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"scheduler {label} is invalid")
    if maximum is not None and value > maximum:
        raise ValueError(f"scheduler {label} is invalid")
    return value
