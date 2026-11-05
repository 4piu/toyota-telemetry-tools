"""Automatic collection readiness and the supervised live collection run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import shutil
import threading
from typing import Any, Callable, Mapping

from ..admission import ALL_COMPLETE, compile_plan
from ..collection_service import run_collection
from ..keep_awake import keep_awake
from ..repository import ProjectRepository
from .inventory import refresh_cache
from .live import LiveLock, scope_hash


PLAN_FILE = "admitted-plan.json"


@dataclass
class Problem:
    """A readiness problem with an interface-neutral hint and a CLI command."""

    message: str
    hint: str
    command: str
    page: str

    def as_dict(self) -> dict[str, str]:
        return {"message": self.message, "hint": self.hint, "command": self.command, "page": self.page}


SLICES = "slices"
SEQUENTIAL = "sequential"
# How one confirmed collection takes several endpoints: name -> label. A
# strategy only decides each turn's time limit (and, later, the next
# endpoint); confirmation and the per-turn scope check are shared.
STRATEGIES = {
    SLICES: "Switch every few minutes",
    SEQUENTIAL: "One after another, each until done",
}
MIN_SLICE_SECONDS = 60


@dataclass(frozen=True)
class Rotation:
    """A request to take several endpoints in turn."""

    strategy: str = SLICES
    slice_seconds: int | None = 300  # SLICES only
    endpoints: tuple[str, ...] = ()  # the order; empty means every ready endpoint

    def turn_limit_ms(self) -> int | None:
        return self.slice_seconds * 1000 if self.strategy == SLICES and self.slice_seconds else None


@dataclass
class CollectionReadiness:
    ready: bool
    problems: list[Problem] = field(default_factory=list)
    complete: bool = False  # every included target is met; nothing to collect
    endpoints: list[str] = field(default_factory=list)
    endpoint_id: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    digest: str | None = None
    # Set when one confirmed collection takes several endpoints in turn:
    # {"strategy", "slice_seconds", "order"}, as confirmed in the scope.
    rotation: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "complete": self.complete,
            "rotation": self.rotation,
            "problems": [problem.as_dict() for problem in self.problems],
            "endpoints": list(self.endpoints),
            "endpoint_id": self.endpoint_id,
            "summary": dict(self.summary),
            "scope": dict(self.scope),
            "scope_sha256": self.digest,
        }


def collection_readiness(
    repository: ProjectRepository,
    inventory: Mapping[str, Any],
    *,
    endpoint_id: str | None = None,
    rotation: Rotation | None = None,
    available_disk_mib: int | None = None,
) -> CollectionReadiness:
    """Compile the current collection scope without opening a connector.

    With ``rotation``, the scope covers the chosen endpoints and one
    collection takes them in turn, one run per endpoint and turn.
    """

    readiness = CollectionReadiness(False)
    if repository.local_settings is None:
        readiness.problems.append(Problem(
            "no connector is configured", "Configure the connector",
            "telemetry connector set KIND ...", "setup",
        ))
    refresh_cache(repository, inventory)
    if available_disk_mib is None:
        repository.paths.state.mkdir(parents=True, exist_ok=True)
        available_disk_mib = shutil.disk_usage(repository.paths.state).free // (1024 * 1024)
    try:
        plan, plan_sha256 = compile_plan(
            repository,
            available_disk_mib=available_disk_mib,
            created_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            output=repository.paths.plans / PLAN_FILE,
        )
    except ValueError as exc:
        if ALL_COMPLETE in str(exc):
            readiness.complete = True
        else:
            readiness.problems.append(_problem_for(str(exc)))
        return readiness

    readiness.endpoints = [row["endpoint_id"] for row in plan["endpoint_sessions"]]
    if rotation is not None:
        return _rotation_readiness(repository, readiness, plan, plan_sha256, rotation)
    if endpoint_id is None and len(readiness.endpoints) == 1:
        endpoint_id = readiness.endpoints[0]
    if endpoint_id is None:
        readiness.problems.append(Problem(
            f"targets span {len(readiness.endpoints)} endpoints; one run collects one endpoint",
            "Choose the endpoint, or take them in turn", "telemetry collect --endpoint ENDPOINT", "collect",
        ))
    elif endpoint_id not in readiness.endpoints:
        readiness.problems.append(Problem(
            f"{endpoint_id} has no unfinished targets ready to collect",
            "Choose one of: " + ", ".join(readiness.endpoints),
            "telemetry collect --endpoint " + readiness.endpoints[0], "collect",
        ))
        endpoint_id = None
    readiness.endpoint_id = endpoint_id
    acquisitions = [
        row for row in plan["acquisitions"] if endpoint_id is None or row["endpoint_id"] == endpoint_id
    ]
    loads = {row["endpoint_id"]: row for row in plan["stress"]["endpoint_loads"]}
    readiness.summary = {
        "unfinished_targets": len({ref for row in acquisitions for ref in row["objective_ids"]}),
        "dids": len(acquisitions),
        "endpoint_count": len(readiness.endpoints),
        "endpoint_targets": _endpoint_targets(plan),
        "estimated_bus_utilization": (
            loads[endpoint_id]["bus_utilization"] if endpoint_id in loads else None
        ),
        "request_rate_hz": loads[endpoint_id]["request_rate_hz"] if endpoint_id in loads else None,
        "capture_tree_max_mib": plan["storage"]["capture_tree_max_mib"],
        "plan_sha256": plan_sha256,
    }
    if endpoint_id is not None and repository.local_settings is not None:
        readiness.scope = {
            "connector": _connector(repository),
            **_endpoint_scope(plan, endpoint_id),
            "storage": plan["storage"],
            "source_hashes": plan["source_hashes"],
        }
        readiness.digest = scope_hash(readiness.scope)
    readiness.ready = not readiness.problems
    return readiness


def _rotation_readiness(
    repository: ProjectRepository,
    readiness: CollectionReadiness,
    plan: Mapping[str, Any],
    plan_sha256: str,
    rotation: Rotation,
) -> CollectionReadiness:
    """One scope over the chosen endpoints, collected in turn."""

    order = list(dict.fromkeys(rotation.endpoints)) or list(readiness.endpoints)
    for endpoint_id in order:
        if endpoint_id not in readiness.endpoints:
            readiness.problems.append(Problem(
                f"{endpoint_id} has no unfinished targets ready to collect",
                "Leave it out", "telemetry collect --rotate --endpoint ENDPOINT ...", "collect",
            ))
    order = [endpoint_id for endpoint_id in order if endpoint_id in readiness.endpoints]
    if rotation.strategy not in STRATEGIES:
        readiness.problems.append(Problem(
            f"unknown strategy {rotation.strategy!r}", "Choose " + " or ".join(STRATEGIES),
            "telemetry collect --rotate --strategy " + SLICES, "collect",
        ))
    slice_seconds = rotation.slice_seconds if rotation.strategy == SLICES else None
    if rotation.strategy == SLICES and (slice_seconds or 0) < MIN_SLICE_SECONDS:
        readiness.problems.append(Problem(
            f"a time slice of {slice_seconds or 0} s is shorter than {MIN_SLICE_SECONDS} s",
            "Switch ECUs less often", "telemetry collect --rotate --slice 5m", "collect",
        ))
    readiness.rotation = {"strategy": rotation.strategy, "slice_seconds": slice_seconds, "order": order}
    acquisitions = [row for row in plan["acquisitions"] if row["endpoint_id"] in order]
    loads = [row for row in plan["stress"]["endpoint_loads"] if row["endpoint_id"] in order]
    readiness.summary = {
        "unfinished_targets": len({ref for row in acquisitions for ref in row["objective_ids"]}),
        "dids": len(acquisitions),
        "endpoint_count": len(order),
        "endpoint_targets": _endpoint_targets(plan),
        # One endpoint is read at a time, so the busiest one sets the peak.
        "estimated_bus_utilization": max((row["bus_utilization"] for row in loads), default=None),
        "request_rate_hz": max((row["request_rate_hz"] for row in loads), default=None),
        "capture_tree_max_mib": plan["storage"]["capture_tree_max_mib"],
        "plan_sha256": plan_sha256,
    }
    if repository.local_settings is not None and order:
        endpoints = [_endpoint_scope(plan, endpoint_id) for endpoint_id in order]
        readiness.scope = {
            "connector": _connector(repository),
            "rotation": dict(readiness.rotation),
            "endpoints": endpoints,
            "targets": sorted({ref for row in endpoints for ref in row["targets"]}),
            "storage": plan["storage"],
            "source_hashes": plan["source_hashes"],
        }
        readiness.digest = scope_hash(readiness.scope)
    readiness.ready = not readiness.problems
    return readiness


def _endpoint_targets(plan: Mapping[str, Any]) -> dict[str, int]:
    """Unfinished targets per ready endpoint, for choosing which to collect."""

    targets: dict[str, set[str]] = {row["endpoint_id"]: set() for row in plan["endpoint_sessions"]}
    for row in plan["acquisitions"]:
        targets[row["endpoint_id"]].update(row["objective_ids"])
    return {endpoint_id: len(refs) for endpoint_id, refs in targets.items()}


def _connector(repository: ProjectRepository) -> dict[str, Any]:
    backend = repository.local_settings["backend"]
    return {"kind": backend["kind"], "settings": dict(backend["settings"])}


def _endpoint_scope(plan: Mapping[str, Any], endpoint_id: str) -> dict[str, Any]:
    """What one endpoint's run may send: its session and its DID reads."""

    acquisitions = [row for row in plan["acquisitions"] if row["endpoint_id"] == endpoint_id]
    return {
        "endpoint": next(row for row in plan["endpoint_sessions"] if row["endpoint_id"] == endpoint_id),
        "acquisitions": [
            {
                "acquisition_id": row["acquisition_id"],
                "request_hex": row["request_hex"],
                "priority": row["priority"],
                "target_interval_ms": row["target_interval_ms"],
                "maximum_interval_ms": row["maximum_interval_ms"],
            }
            for row in acquisitions
        ],
        "targets": sorted(ref for row in acquisitions for ref in row["objective_ids"]),
    }


def start_collection(
    repository: ProjectRepository,
    inventory: Mapping[str, Any],
    readiness: CollectionReadiness,
    *,
    note: str | None,
    event_sink: Callable[[dict], None],
    stop_event: threading.Event,
) -> str:
    """Run one live collection for a ready scope while owning the project."""

    if readiness.ready and readiness.rotation is not None:
        return _rotate(repository, inventory, readiness, note=note, event_sink=event_sink, stop_event=stop_event)
    if not readiness.ready or readiness.endpoint_id is None:
        raise ValueError("collection is not ready; resolve the readiness problems first")
    with LiveLock(repository), keep_awake():
        reason = run_collection(
            repository.root,
            plan_path=repository.paths.plans / PLAN_FILE,
            endpoint_id=readiness.endpoint_id,
            run_note=note,
            event_sink=event_sink,
            stop_event=stop_event,
        )
    refresh_cache(ProjectRepository(repository.root), inventory)
    return reason


def _rotate(
    repository: ProjectRepository,
    inventory: Mapping[str, Any],
    readiness: CollectionReadiness,
    *,
    note: str | None,
    event_sink: Callable[[dict], None],
    stop_event: threading.Event,
) -> str:
    """Take the confirmed endpoints in turn until every target is met.

    Each turn is an ordinary run on one endpoint that ends when its targets
    are met or, with the slices strategy, its time slice is over. Before each
    turn the scope is compiled again; it may only shrink as targets complete,
    never send anything the operator did not confirm.
    """

    confirmed = {row["endpoint"]["endpoint_id"]: row for row in readiness.scope["endpoints"]}
    rotation = readiness.scope["rotation"]
    limit_ms = Rotation(rotation["strategy"], rotation["slice_seconds"]).turn_limit_ms()
    with LiveLock(repository), keep_awake():
        while not stop_event.is_set():
            collected = False
            for endpoint_id in readiness.scope["rotation"]["order"]:
                if stop_event.is_set():
                    break
                current = collection_readiness(ProjectRepository(repository.root), inventory, endpoint_id=endpoint_id)
                if current.complete or endpoint_id not in current.endpoints:
                    continue
                if not current.ready:
                    raise ValueError(f"{endpoint_id} is no longer ready: {current.problems[0].message}")
                _require_confirmed(current.scope, readiness.scope["connector"], confirmed[endpoint_id])
                reason = run_collection(
                    repository.root,
                    plan_path=repository.paths.plans / PLAN_FILE,
                    endpoint_id=endpoint_id,
                    run_note=note,
                    event_sink=event_sink,
                    stop_event=stop_event,
                    max_duration_ms=limit_ms,
                )
                collected = True
                if reason not in ("slice_end", "plan_complete"):
                    refresh_cache(ProjectRepository(repository.root), inventory)
                    return reason
            if not collected:
                return "plan_complete"
    refresh_cache(ProjectRepository(repository.root), inventory)
    return "operator_stop"


def _require_confirmed(current: Mapping[str, Any], connector: Mapping[str, Any], confirmed: Mapping[str, Any]) -> None:
    """A turn may send only what the operator confirmed for that endpoint."""

    allowed = {(row["acquisition_id"], row["request_hex"]) for row in confirmed["acquisitions"]}
    sends = {(row["acquisition_id"], row["request_hex"]) for row in current["acquisitions"]}
    if current["connector"] != connector or current["endpoint"] != confirmed["endpoint"] or not sends <= allowed:
        raise ValueError(
            f"the scope of {confirmed['endpoint']['endpoint_id']} changed since it was confirmed; "
            "check readiness and start again"
        )


_FIXES = (
    (re.compile(r"verify (\S+) first"), "Verify {0}", "telemetry verify {0}", "signals"),
    (re.compile(r"needs (\S+/0x[0-9A-F]{4})"), "Include the DID settings for {0}",
     "telemetry did set {0} --include", "signals"),
    (re.compile(r"no included targets"), "Set collection targets",
     "telemetry target set SELECTOR --min-samples N", "signals"),
    (re.compile(r"insufficient disk"), "Free disk space", "telemetry status", "setup"),
    (re.compile(r"endpoint (\S+) .*exceeds tracked limit"), "Lengthen DID intervals on {0}",
     "telemetry did set {0} --interval MS", "signals"),
    (re.compile(r"request-start floor"), "Lengthen DID intervals",
     "telemetry did set SELECTOR --interval MS", "signals"),
    (re.compile(r"(?:formula is ambiguous|does not resolve catalog signal): (\S+)"),
     "Choose a formula for {0}", "telemetry signal set {0} --formula EXPR", "signals"),
)


def _problem_for(message: str) -> Problem:
    for pattern, hint, command, page in _FIXES:
        match = pattern.search(message)
        if match:
            return Problem(message, hint.format(*match.groups()), command.format(*match.groups()), page)
    return Problem(message, "Check the project files", "telemetry check", "setup")
