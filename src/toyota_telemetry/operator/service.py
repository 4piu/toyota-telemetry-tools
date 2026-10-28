"""The one operator facade shared by the ``telemetry`` CLI and ``telemetry-ui``.

Front ends only parse input and format output. Every decision a user can
observe lives here or in the modules this facade composes, and every user
operation is listed in ``OPERATIONS`` with its CLI command and desktop action
so a parity test can prove both interfaces are complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import threading
from typing import Any, Callable, Iterable, Mapping, TextIO

from .. import __version__
from ..discovery import ProbeWorkerFactory, promote_confirmed_endpoint
from ..exporting import export_project
from ..project import PROJECT_MARKER
from ..repository import ProjectRepository
from . import editing
from .collection import CollectionReadiness, collection_readiness, start_collection
from .connector import ConnectorCheck, check_connector, connector_document
from .editing import UNSET, EditResult
from .inventory import (
    ImportPreview,
    InventoryTree,
    add_manual_endpoint,
    apply_import,
    build_tree,
    cache_rows,
    load_inventory,
    preview_import,
    progress_by_signal,
    rebuild_cache,
    refresh_cache,
)
from .live import LiveBusy, LiveLock, live_busy
from .recordings import (
    NEEDS_RECOVERY,
    RunSummary,
    export_observations,
    list_runs,
    quarantine_run,
    recover_run,
    run_detail,
    signal_series,
)
from .selection import Selection, resolve_selection
from .sources import SourceRows
from .verification import VerificationResult, VerificationScope, build_verification_scope, run_verification


@dataclass(frozen=True)
class Operation:
    id: str
    title: str
    cli: str
    gui: str
    live: bool = False


OPERATIONS: tuple[Operation, ...] = (
    Operation("status", "Show readiness and the next action", "telemetry status", "banner.next"),
    Operation("check", "Validate project files", "telemetry check", "setup.check"),
    Operation("connector.show", "Show connector settings", "telemetry connector show", "setup.connector.form"),
    Operation("connector.set", "Save connector settings", "telemetry connector set", "setup.connector.save"),
    Operation("connector.check", "Check connector offline", "telemetry connector check", "setup.connector.check"),
    Operation("source.preview", "Preview a candidate source", "telemetry source import", "setup.source.preview"),
    Operation("source.apply", "Apply a candidate source", "telemetry source import --apply", "setup.source.apply"),
    Operation("cache.rebuild", "Rebuild the derived cache", "telemetry cache rebuild", "setup.cache.rebuild"),
    Operation("signals.list", "List endpoints, DIDs, and signals", "telemetry signals", "signals.tree"),
    Operation("signals.show", "Show one DID or signal", "telemetry signals show", "signals.detail"),
    Operation("endpoint.add", "Add a manual endpoint candidate", "telemetry endpoint add", "signals.endpoint.add"),
    Operation("endpoint.confirm", "Confirm a responding endpoint", "telemetry endpoint confirm", "signals.endpoint.confirm"),
    Operation("verify", "Verify endpoints and DIDs", "telemetry verify", "signals.verify", live=True),
    Operation("signal.set", "Edit a signal definition", "telemetry signal set", "signals.definition.apply"),
    Operation("did.set", "Edit shared DID settings", "telemetry did set", "signals.did.apply"),
    Operation("target.set", "Set collection targets", "telemetry target set", "signals.target.edit"),
    Operation("target.clear", "Remove collection targets", "telemetry target clear", "signals.target.clear"),
    Operation("collect.readiness", "Check collection readiness", "telemetry collect --dry-run", "collect.readiness"),
    Operation("collect", "Collect data", "telemetry collect", "collect.start", live=True),
    Operation("recordings.list", "List recorded runs", "telemetry recordings", "recordings.runs"),
    Operation("recordings.show", "Show one run", "telemetry recordings show", "recordings.run.detail"),
    Operation("recordings.recover", "Recover an interrupted run", "telemetry recordings recover", "recordings.recover"),
    Operation("recordings.quarantine", "Quarantine an interrupted run", "telemetry recordings quarantine", "recordings.quarantine"),
    Operation("recordings.export", "Export signal data as CSV", "telemetry recordings export", "recordings.export"),
    Operation("result.generate", "Generate the project result", "telemetry result generate", "recordings.result"),
)
OPERATION_IDS = frozenset(operation.id for operation in OPERATIONS)


@dataclass
class NextAction:
    text: str
    command: str
    page: str

    def as_dict(self) -> dict[str, str]:
        return {"text": self.text, "command": self.command, "page": self.page}


@dataclass
class WorkspaceReadiness:
    project_root: str
    valid: bool
    error: str | None = None
    project_id: str | None = None
    profile_id: str | None = None
    connector: str | None = None
    sources: int = 0
    candidate_rows: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    targets_complete: int = 0
    collection: dict[str, Any] | None = None
    runs: int = 0
    recoverable_runs: list[str] = field(default_factory=list)
    live_busy: bool = False
    next_action: NextAction | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "valid": self.valid,
            "error": self.error,
            "project_id": self.project_id,
            "profile_id": self.profile_id,
            "connector": self.connector,
            "sources": self.sources,
            "candidate_rows": self.candidate_rows,
            "counts": dict(self.counts),
            "targets_complete": self.targets_complete,
            "collection": self.collection,
            "runs": self.runs,
            "recoverable_runs": list(self.recoverable_runs),
            "live_busy": self.live_busy,
            "next_action": self.next_action.as_dict() if self.next_action else None,
        }


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": list(self.problems), "notes": list(self.notes)}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OperatorService:
    """Stateless facade: each call rereads tracked and ignored project state."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        if not (self.root / PROJECT_MARKER).is_file():
            raise FileNotFoundError(f"{self.root} has no {PROJECT_MARKER}")

    def repository(self) -> ProjectRepository:
        return ProjectRepository(self.root)

    def inventory(self, repository: ProjectRepository | None = None) -> dict[str, Any]:
        return load_inventory(repository or self.repository())

    # Status and validation -------------------------------------------------

    def status(self) -> WorkspaceReadiness:
        status = WorkspaceReadiness(str(self.root), True)
        try:
            repository = self.repository()
            inventory = self.inventory(repository)
        except (OSError, ValueError) as exc:
            status.valid = False
            status.error = str(exc)
            status.next_action = NextAction("Fix the project files", "telemetry check", "setup")
            return status
        status.project_id = repository.project_id
        status.profile_id = repository.profile_id
        status.live_busy = live_busy(repository)
        if repository.local_settings is not None:
            backend = repository.local_settings["backend"]
            device = " ".join(f"{key}={value}" for key, value in sorted(backend["settings"].items()))
            status.connector = f"{backend['kind']} {device}".strip()
        status.sources = len(inventory["sources"])
        status.candidate_rows = len(inventory["rows"])
        runs = list_runs(repository)
        status.runs = sum(1 for run in runs if run.state == "complete")
        if not status.live_busy:
            status.recoverable_runs = [run.run_id for run in runs if run.state == NEEDS_RECOVERY]
        try:
            refresh_cache(repository, inventory)
            progress = progress_by_signal(repository)
        except (OSError, ValueError) as exc:
            status.valid = False
            status.error = f"cache could not be refreshed: {exc}"
            status.next_action = NextAction("Fix the project files", "telemetry check", "setup")
            return status
        tree = build_tree(repository, inventory, progress=progress)
        status.counts = tree.counts()
        status.targets_complete = sum(1 for row in progress.values() if row["complete"])
        if any(row["included"] for row in repository.policy["targets"]):
            readiness = collection_readiness(repository, inventory)
            status.collection = {
                "ready": readiness.ready,
                "problems": [problem.as_dict() for problem in readiness.problems],
                "endpoints": readiness.endpoints,
            }
        status.next_action = self._next_action(repository, status, tree)
        return status

    def _next_action(
        self, repository: ProjectRepository, status: WorkspaceReadiness, tree: InventoryTree
    ) -> NextAction:
        if status.recoverable_runs:
            run_id = status.recoverable_runs[0]
            return NextAction(
                f"Recover interrupted run {run_id[:8]}", f"telemetry recordings recover {run_id}",
                "recordings",
            )
        if status.connector is None:
            return NextAction("Configure the connector", "telemetry connector set KIND ...", "setup")
        if not status.candidate_rows:
            return NextAction(
                "Import a candidate signal list", "telemetry source import file PATH --apply", "setup",
            )
        responded = [row for row in tree.endpoints if row.state == "responded"]
        if responded:
            row = responded[0]
            ecus = " ".join(f"--ecu {key}" for key in row.ecu_keys) or "--ecu KEY"
            return NextAction(
                f"Confirm responding endpoint {row.endpoint_id}",
                f"telemetry endpoint confirm {row.endpoint_id} {ecus} --source docs/PATH.md",
                "signals",
            )
        if not status.counts.get("verified_dids"):
            pending = next(
                (row for row in tree.endpoints if row.dids and row.tester_can_id), None
            )
            selector = pending.key if pending else "ENDPOINT"
            return NextAction(
                "Verify endpoints and DIDs with the vehicle",
                f"telemetry verify {selector} --dry-run", "signals",
            )
        if not status.counts.get("targets"):
            return NextAction(
                "Set collection targets", "telemetry target set SELECTOR --min-samples N", "signals",
            )
        if status.collection is not None and status.collection["problems"]:
            problem = status.collection["problems"][0]
            return NextAction(problem["message"], problem["fix"], problem["page"])
        if status.collection is not None and status.collection["ready"]:
            return NextAction("Collect data", "telemetry collect --dry-run", "collect")
        return NextAction("Analyze recordings", "telemetry recordings", "recordings")

    def check(self) -> CheckResult:
        result = CheckResult()
        try:
            repository = self.repository()
        except (OSError, ValueError) as exc:
            result.problems.append(str(exc))
            return result
        result.notes.append(
            f"project {repository.project_id} profile {repository.profile_id}: tracked files are valid"
        )
        try:
            inventory = self.inventory(repository)
        except (OSError, ValueError) as exc:
            result.problems.append(f"candidate inventory: {exc}")
            return result
        try:
            repository.discovery_candidates()
        except (OSError, ValueError) as exc:
            result.problems.append(f"verification facts: {exc}")
        try:
            repository._validate_catalog_rows(cache_rows(repository, inventory))
        except ValueError as exc:
            result.problems.append(f"candidate list: {exc}")
        if repository.local_settings is None:
            result.notes.append("no connector configured yet")
        return result

    # Connector -------------------------------------------------------------

    def connector(self) -> dict[str, Any] | None:
        return self.repository().local_settings

    def connector_document(
        self, kind: str, settings: Mapping[str, str], executable: str | None = None
    ) -> dict[str, Any]:
        return connector_document(self.repository(), kind, settings, executable)

    def save_connector(self, document: Mapping[str, Any]) -> None:
        self.repository().write_authored_document("local_settings", document)

    def check_connector(
        self, document: Mapping[str, Any] | None = None, *, run_self_test: bool = True
    ) -> ConnectorCheck:
        repository = self.repository()
        return check_connector(
            repository, document if document is not None else repository.local_settings,
            run_self_test=run_self_test,
        )

    # Sources and cache -----------------------------------------------------

    def preview_source(self, source: SourceRows) -> ImportPreview:
        repository = self.repository()
        return preview_import(repository, self.inventory(repository), source)

    def apply_source(self, preview: ImportPreview) -> dict[str, Any]:
        repository = self.repository()
        return apply_import(repository, self.inventory(repository), preview)

    def rebuild_cache(self) -> None:
        repository = self.repository()
        rebuild_cache(repository, self.inventory(repository))

    # Inventory and selection ----------------------------------------------

    def tree(
        self,
        *,
        review: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> InventoryTree:
        """Build the tree from saved authorities, or from unsaved draft documents."""

        repository = self.repository()
        inventory = self.inventory(repository)
        try:
            refresh_cache(repository, inventory)
        except ValueError:
            pass
        progress = progress_by_signal(repository)
        if review is not None:
            repository.review = dict(review)
        if policy is not None:
            repository.policy = dict(policy)
        return build_tree(repository, inventory, progress=progress)

    @staticmethod
    def select(tree: InventoryTree, tokens: Iterable[str] = (), **filters: Any) -> Selection:
        return resolve_selection(tree, tokens, **filters)

    def add_endpoint(
        self, tester_can_id: str, response_can_id: str, session_recipe_id: str, ecu_keys: Iterable[int]
    ) -> dict[str, Any]:
        repository = self.repository()
        return add_manual_endpoint(
            repository, self.inventory(repository), tester_can_id, response_can_id,
            session_recipe_id, ecu_keys,
        )

    def confirm_endpoint(
        self,
        candidate_id: str,
        ecu_keys: Iterable[int],
        provenance: Iterable[str],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        repository = self.repository()
        candidates = {row["candidate_id"]: row for row in repository.discovery_candidates()}
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise ValueError(
                f"{candidate_id} has no verification result; verify it before confirming"
            )
        document = promote_confirmed_endpoint(
            repository.transport, candidate, sorted(set(ecu_keys)), list(provenance)
        )
        endpoint = next(row for row in document["endpoints"] if row["id"] == candidate_id)
        if not dry_run:
            repository.write_authored_document("transport", document)
            refreshed = self.repository()
            rebuild_cache(refreshed, self.inventory(refreshed))
        return endpoint

    # Verification ----------------------------------------------------------

    def verification_scope(
        self, tree: InventoryTree, selection: Selection, *, session_recipe_id: str | None = None
    ) -> VerificationScope:
        return build_verification_scope(
            self.repository(), tree, selection, session_recipe_id=session_recipe_id
        )

    def verify(
        self,
        scope: VerificationScope,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        stop_event: threading.Event | None = None,
        probe_worker_factory: ProbeWorkerFactory | None = None,
    ) -> VerificationResult:
        repository = self.repository()
        with LiveLock(repository):
            return run_verification(
                repository, scope, utc_now=utc_now, on_event=on_event,
                stop_event=stop_event, probe_worker_factory=probe_worker_factory,
            )

    # Review and targets ----------------------------------------------------

    def edit_signals(self, tree: InventoryTree, signal_refs: Iterable[str], **fields: Any) -> EditResult:
        return editing.edit_signals(self.repository(), tree, signal_refs, **fields)

    def edit_dids(self, tree: InventoryTree, did_keys: Iterable[str], **fields: Any) -> EditResult:
        return editing.edit_did_settings(self.repository(), tree, did_keys, **fields)

    def edit_targets(self, tree: InventoryTree, signal_refs: Iterable[str], **fields: Any) -> EditResult:
        return editing.edit_targets(self.repository(), tree, signal_refs, **fields)

    def clear_targets(self, signal_refs: Iterable[str], **fields: Any) -> EditResult:
        return editing.clear_targets(self.repository(), signal_refs, **fields)

    def save(
        self,
        *,
        review: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> None:
        repository = self.repository()
        editing.save_documents(repository, review=review, policy=policy)
        refreshed = self.repository()
        try:
            refresh_cache(refreshed, self.inventory(refreshed))
        except ValueError:
            pass

    def authority_bytes(self) -> dict[str, bytes]:
        """Current tracked bytes, used by drafts to detect external edits."""

        repository = self.repository()
        return {
            "signal_review": repository.paths.signal_review.read_bytes(),
            "collection_policy": repository.paths.collection_policy.read_bytes(),
            "transport": repository.paths.transport.read_bytes(),
        }

    # Collection ------------------------------------------------------------

    def readiness(self, endpoint_id: str | None = None) -> CollectionReadiness:
        repository = self.repository()
        return collection_readiness(repository, self.inventory(repository), endpoint_id=endpoint_id)

    def collect(
        self,
        readiness: CollectionReadiness,
        *,
        note: str | None,
        event_sink: Callable[[dict], None],
        stop_event: threading.Event,
    ) -> str:
        repository = self.repository()
        return start_collection(
            repository, self.inventory(repository), readiness,
            note=note, event_sink=event_sink, stop_event=stop_event,
        )

    # Recordings and result -------------------------------------------------

    def runs(self) -> list[RunSummary]:
        return list_runs(self.repository())

    def run(self, run_id: str) -> RunSummary:
        return run_detail(self.repository(), run_id)

    def recover(self, run_id: str) -> Path:
        path = recover_run(self.repository(), run_id)
        self._refresh_quietly()
        return path

    def quarantine(self, run_id: str) -> Path:
        path = quarantine_run(self.repository(), run_id)
        self._refresh_quietly()
        return path

    def export(self, signal_refs: Iterable[str], stream: TextIO, *, run_ids: Iterable[str] = ()) -> int:
        repository = self.repository()
        return export_observations(
            repository, self.inventory(repository), signal_refs, stream, run_ids=run_ids
        )

    def observation_summary(self) -> dict[str, dict[str, Any]]:
        repository = self.repository()
        refresh_cache(repository, self.inventory(repository))
        return repository.signal_observation_summary()

    def series(self, signal_ref: str) -> list[dict[str, Any]]:
        repository = self.repository()
        return signal_series(repository, self.inventory(repository), signal_ref)

    def generate_result(self) -> dict[str, Any]:
        repository = self.repository()
        refresh_cache(repository, self.inventory(repository))
        return export_project(
            repository,
            toolkit_version=__version__,
            toolkit_commit=_toolkit_commit(repository),
            generated_at_utc=utc_now(),
        )

    def _refresh_quietly(self) -> None:
        repository = self.repository()
        try:
            refresh_cache(repository, self.inventory(repository))
        except ValueError:
            pass


def _toolkit_commit(repository: ProjectRepository) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository.paths.toolkit), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"cannot read the toolkit commit at {repository.paths.toolkit}; "
            "the result records which toolkit produced it"
        ) from exc
    return completed.stdout.strip()


__all__ = [
    "OPERATIONS",
    "OPERATION_IDS",
    "CheckResult",
    "LiveBusy",
    "NextAction",
    "Operation",
    "OperatorService",
    "UNSET",
    "WorkspaceReadiness",
]
