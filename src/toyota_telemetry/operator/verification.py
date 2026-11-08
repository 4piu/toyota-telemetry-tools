"""One orchestrated, bounded verification job over an explicit selection.

Each selected endpoint is probed first unless it is confirmed or already
responded; DIDs are read only on endpoints that respond. The scope is fixed
and hashed before anything opens, so the confirmation shows exactly what will
be transmitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Callable, Mapping

from ..discovery import ProbeWorkerFactory, _candidate_id, discover_endpoints, preflight_dids
from ..identity import canonical_did, within_read_range
from ..repository import ProjectRepository
from .inventory import InventoryTree, session_opened
from .live import scope_hash
from .selection import Selection


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
    target_address: str | None = None
    # Further reviewed session recipes tried, in order, if the first does
    # not open a session (automatic choice for an endpoint never verified).
    fallback_recipes: list[str] = field(default_factory=list)

    @property
    def session_text(self) -> str:
        """The session used, or the automatic order for an endpoint never verified."""

        if not self.fallback_recipes:
            return f"session {self.session_recipe_id}"
        order = ", then ".join([self.session_recipe_id, *self.fallback_recipes])
        return f"session automatic (tries {order})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tester_can_id": self.tester_can_id,
            "response_can_id": self.response_can_id,
            **({} if self.target_address is None else {"target_address": self.target_address}),
            **({"fallback_recipes": list(self.fallback_recipes)} if self.fallback_recipes else {}),
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
    if not recipes:
        raise ValueError("the transport settings have no session recipe yet")
    # Automatic order: the recipe most confirmed endpoints use comes first.
    usage = {recipe: 0 for recipe in recipes}
    for row in repository.transport["endpoints"]:
        usage[row["session_recipe_id"]] = usage.get(row["session_recipe_id"], 0) + 1
    recipe_order = sorted(recipes, key=lambda recipe: (-usage[recipe], recipe))
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
        if not within_read_range(int(data_id, 16), did_min, did_max):
            raise ValueError(
                f"{key} is outside the transport DID safety range {safety['did_min']}..{safety['did_max']}"
            )
        probe = grouped.get(endpoint.key)
        if probe is None:
            probe = _endpoint_probe(endpoint, recipe_order, session_recipe_id)
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
            seconds += (1 + len(row.fallback_recipes)) * (3 + steps * per_step)
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


def _endpoint_probe(endpoint, recipe_order: list[str], session_recipe_id: str | None) -> EndpointProbe:
    if endpoint.tester_can_id is None or endpoint.response_can_id is None:
        raise ValueError(
            f"{endpoint.key} has no known CAN IDs; add a manual endpoint with its tester and "
            "response CAN IDs"
        )
    if endpoint.confirmed:
        return EndpointProbe(
            endpoint.key, endpoint.tester_can_id, endpoint.response_can_id,
            endpoint.session_recipe_id, endpoint.endpoint_id, False, "confirmed endpoint",
            target_address=endpoint.target_address,
        )
    if endpoint.state == "responded" and endpoint.session_open:
        return EndpointProbe(
            endpoint.key, endpoint.tester_can_id, endpoint.response_can_id,
            endpoint.session_recipe_id, endpoint.endpoint_id, False, "responded earlier",
            target_address=endpoint.target_address,
        )
    # An ECU that answered but whose session never opened is retried with
    # every recipe rather than the one that failed.
    recipe = session_recipe_id or (endpoint.session_recipe_id if endpoint.state != "responded" else None)
    fallbacks: list[str] = []
    if recipe is None:
        recipe, *fallbacks = recipe_order
    target = _candidate_id(
        endpoint.tester_can_id, endpoint.response_can_id, recipe, endpoint.target_address
    )
    return EndpointProbe(
        endpoint.key, endpoint.tester_can_id, endpoint.response_can_id, recipe, target, True,
        "endpoint not verified yet", target_address=endpoint.target_address,
        fallback_recipes=fallbacks,
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

    emit_event = on_event or (lambda event: None)
    stop = stop_event or threading.Event()
    result = VerificationResult()
    done = 0

    def emit(event: dict[str, Any]) -> None:
        # Every endpoint probe and every DID (read or skipped) is one step of
        # the scope's probe count, so done/total always reaches total.
        nonlocal done
        done += 1
        event["done"] = done
        event["total"] = scope.probe_count
        emit_event(event)

    root = repository.root
    for row in scope.endpoints:
        if stop.is_set():
            result.stopped = True
            break
        responded = True
        target_id = row.target_id
        if row.probe_endpoint:
            fact = _probe_sessions(repository, row, utc_now, probe_worker_factory)
            responded = bool(fact["responded"])
            target_id = fact["candidate_id"]
            event = {
                "event": "endpoint", "key": row.key, "target_id": target_id, "responded": responded,
                "session_recipe_id": fact["session_recipe_id"],
            }
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
                    repository, root, target_id, [data_id],
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


def _probe_sessions(repository, row: EndpointProbe, utc_now, probe_worker_factory) -> dict[str, Any]:
    """Probe with each candidate recipe until one opens the whole session.

    Every recipe is a reviewed, allowlisted session setup; a probe sends only
    its steps. Returns the fact of the first recipe that opened a session, or
    else of the first one the ECU answered at all, or else the last attempt.
    """

    recipes = {recipe["id"]: recipe for recipe in repository.transport["session_recipes"]}
    answered = None
    fact: dict[str, Any] = {}
    for recipe_id in [row.session_recipe_id, *row.fallback_recipes]:
        fact = discover_endpoints(
            repository, repository.root,
            [(row.tester_can_id, row.response_can_id, recipe_id, row.target_address)],
            utc_now=utc_now, probe_worker_factory=probe_worker_factory,
        )[0]
        if session_opened(fact["result"], recipes[recipe_id]):
            return fact
        if fact["responded"] and answered is None:
            answered = fact
    return answered or fact


def progress_text(event: Mapping[str, Any]) -> str:
    """``(done/total percent%)`` for a verification event, or "" without counts."""

    done, total = event.get("done"), event.get("total")
    if not isinstance(done, int) or not isinstance(total, int) or total <= 0:
        return ""
    return f"({done}/{total} {done * 100 // total}%)"


def _device(settings: dict[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in sorted(settings.items()))
