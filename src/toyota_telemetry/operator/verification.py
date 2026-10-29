"""One orchestrated, bounded verification job over an explicit selection.

Each selected endpoint is probed first unless it is confirmed or already
responded; DIDs are read only on endpoints that respond. The scope is fixed
and hashed before anything opens, so the confirmation shows exactly what will
be transmitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Callable

from ..discovery import ProbeWorkerFactory, _candidate_id, discover_endpoints, preflight_dids
from ..identity import canonical_did
from ..repository import ProjectRepository
from .inventory import InventoryTree
from .live import scope_hash
from .selection import Selection


class RecipeRequired(ValueError):
    """An unverified endpoint needs the operator to choose a session recipe."""

    def __init__(self, endpoint_key: str, recipes: list[str]) -> None:
        super().__init__(f"{endpoint_key} needs a session recipe; choose one of {', '.join(recipes)}")
        self.endpoint_key = endpoint_key
        self.recipes = recipes


@dataclass
class EndpointProbe:
    key: str
    tester_can_id: str
    response_can_id: str
    session_recipe_id: str
    target_id: str
    probe_endpoint: bool
    reason: str
    dids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tester_can_id": self.tester_can_id,
            "response_can_id": self.response_can_id,
            "session_recipe_id": self.session_recipe_id,
            "target_id": self.target_id,
            "probe_endpoint": self.probe_endpoint,
            "reason": self.reason,
            "dids": list(self.dids),
        }


@dataclass
class VerificationScope:
    connector_kind: str
    connector_settings: dict[str, str]
    endpoints: list[EndpointProbe]
    probe_count: int
    upper_bound_seconds: float
    digest: str

    @property
    def did_count(self) -> int:
        return sum(len(row.dids) for row in self.endpoints)

    @property
    def endpoint_probe_count(self) -> int:
        return sum(1 for row in self.endpoints if row.probe_endpoint)

    def summary(self) -> str:
        return (
            f"LIVE verification of {len(self.endpoints)} endpoint(s) "
            f"({self.endpoint_probe_count} to probe) and {self.did_count} DID(s) "
            f"on {self.connector_kind} {_device(self.connector_settings)}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": {"kind": self.connector_kind, "settings": dict(self.connector_settings)},
            "endpoints": [row.as_dict() for row in self.endpoints],
            "probe_count": self.probe_count,
            "did_count": self.did_count,
            "upper_bound_seconds": round(self.upper_bound_seconds, 1),
            "scope_sha256": self.digest,
        }


def build_verification_scope(
    repository: ProjectRepository,
    tree: InventoryTree,
    selection: Selection,
    *,
    session_recipe_id: str | None = None,
) -> VerificationScope:
    if repository.local_settings is None:
        raise ValueError("no connector is configured")
    if not selection.dids:
        raise ValueError("verification needs at least one selected DID")
    recipes = {row["id"]: row for row in repository.transport["session_recipes"]}
    if session_recipe_id is not None and session_recipe_id not in recipes:
        raise ValueError(f"unknown session recipe {session_recipe_id!r}")
    safety = repository.transport["safety"]
    did_min, did_max = int(safety["did_min"], 16), int(safety["did_max"], 16)
    per_step = (safety["initial_response_timeout_ms"] + safety["response_pending_budget_ms"]) / 1000

    grouped: dict[str, EndpointProbe] = {}
    for key in selection.dids:
        endpoint_key, _, data_id = key.rpartition("/")
        endpoint = tree.endpoint(endpoint_key)
        if endpoint is None:
            raise ValueError(f"unknown endpoint in selection: {endpoint_key}")
        data_id = canonical_did(data_id)
        if not did_min <= int(data_id, 16) <= did_max:
            raise ValueError(
                f"{key} is outside the transport DID safety range {safety['did_min']}..{safety['did_max']}"
            )
        probe = grouped.get(endpoint.key)
        if probe is None:
            probe = _endpoint_probe(endpoint, recipes, session_recipe_id)
            grouped[endpoint.key] = probe
        if data_id not in probe.dids:
            probe.dids.append(data_id)

    endpoints = list(grouped.values())
    probe_count = 0
    seconds = 0.0
    for row in endpoints:
        steps = len(recipes[row.session_recipe_id]["steps"])
        if row.probe_endpoint:
            probe_count += 1
            seconds += 3 + steps * per_step
        probe_count += len(row.dids)
        seconds += len(row.dids) * (3 + (steps + 1) * per_step)
    backend = repository.local_settings["backend"]
    body = {
        "connector": {"kind": backend["kind"], "settings": backend["settings"]},
        "endpoints": [row.as_dict() for row in endpoints],
    }
    return VerificationScope(
        backend["kind"], dict(backend["settings"]), endpoints, probe_count, seconds, scope_hash(body)
    )


def _endpoint_probe(endpoint, recipes, session_recipe_id: str | None) -> EndpointProbe:
    if endpoint.tester_can_id is None or endpoint.response_can_id is None:
        raise ValueError(
            f"{endpoint.key} has no known CAN IDs; add a manual endpoint with its tester and "
            "response CAN IDs"
        )
    if endpoint.confirmed:
        return EndpointProbe(
            endpoint.key, endpoint.tester_can_id, endpoint.response_can_id,
            endpoint.session_recipe_id, endpoint.endpoint_id, False, "confirmed endpoint",
        )
    if endpoint.state == "responded":
        return EndpointProbe(
            endpoint.key, endpoint.tester_can_id, endpoint.response_can_id,
            endpoint.session_recipe_id, endpoint.endpoint_id, False, "responded earlier",
        )
    recipe = session_recipe_id or endpoint.session_recipe_id
    if recipe is None:
        if len(recipes) != 1:
            raise RecipeRequired(endpoint.key, sorted(recipes))
        recipe = next(iter(recipes))
    target = _candidate_id(endpoint.tester_can_id, endpoint.response_can_id, recipe)
    return EndpointProbe(
        endpoint.key, endpoint.tester_can_id, endpoint.response_can_id, recipe, target, True,
        "endpoint not verified yet",
    )


@dataclass
class VerificationResult:
    endpoints: list[dict[str, Any]] = field(default_factory=list)
    dids: list[dict[str, Any]] = field(default_factory=list)
    stopped: bool = False

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in self.dids:
            counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
        return {
            "endpoints": list(self.endpoints),
            "dids": list(self.dids),
            "outcomes": counts,
            "stopped": self.stopped,
        }


def run_verification(
    repository: ProjectRepository,
    scope: VerificationScope,
    *,
    utc_now: Callable[[], str],
    probe_worker_factory: ProbeWorkerFactory | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> VerificationResult:
    """Run the fixed scope; stops between probes when stop_event is set."""

    emit = on_event or (lambda event: None)
    stop = stop_event or threading.Event()
    result = VerificationResult()
    root = repository.root
    for row in scope.endpoints:
        if stop.is_set():
            result.stopped = True
            break
        responded = True
        if row.probe_endpoint:
            fact = discover_endpoints(
                repository, root,
                [(row.tester_can_id, row.response_can_id, row.session_recipe_id)],
                utc_now=utc_now, probe_worker_factory=probe_worker_factory,
            )[0]
            responded = bool(fact["responded"])
            event = {"event": "endpoint", "key": row.key, "target_id": row.target_id, "responded": responded}
            result.endpoints.append(event)
            emit(event)
        for data_id in row.dids:
            did_key = f"{row.key}/{data_id}"
            if stop.is_set():
                result.stopped = True
                break
            if not responded:
                event = {"event": "did", "key": did_key, "outcome": "skipped", "detail": "endpoint did not respond"}
            else:
                fact = preflight_dids(
                    repository, root, row.target_id, [data_id],
                    utc_now=utc_now, probe_worker_factory=probe_worker_factory,
                )[0]
                read = fact["result"].get("read") or {}
                event = {
                    "event": "did",
                    "key": did_key,
                    "outcome": fact["outcome"],
                    "detail": read.get("nrc") or "",
                    "response_hex": read.get("response_hex"),
                    "latency_ms": read.get("latency_ms"),
                }
            result.dids.append(event)
            emit(event)
        if result.stopped:
            break
    return result


def _device(settings: dict[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in sorted(settings.items()))
