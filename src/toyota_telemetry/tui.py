"""Dependency-free operator workspace over the public collector contract."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable, TextIO

from .collector import parse_public_event
from .capture_store import load_capture_tree
from .repository import ProjectRepository


class TransportMonitor:
    """Strict public-event consumer with parent-side stale inference."""

    def __init__(self, *, stale_after_ms: int = 3000) -> None:
        if stale_after_ms < 1:
            raise ValueError("stale threshold must be positive")
        self.stale_after_ms = stale_after_ms
        self.state = "disconnected"
        self.run_id: str | None = None
        self.current_acquisition_id: str | None = None
        self.last_heartbeat_ms: int | None = None
        self.last_event: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.error_detail: str | None = None

    def accept(self, line: str, now_ms: int) -> dict[str, Any]:
        event = parse_public_event(line)
        run_id = event["run_id"]
        if run_id is not None:
            if self.run_id is not None and run_id != self.run_id:
                raise ValueError("collector event changed run_id")
            self.run_id = run_id
        event_type = event["event"]
        if event_type == "started":
            self.state = "connected"
            self.last_heartbeat_ms = now_ms
        elif event_type == "heartbeat":
            self.state = event["state"]
            self.current_acquisition_id = event["current_acquisition_id"]
            self.last_heartbeat_ms = now_ms
        elif event_type == "response_pending":
            self.state = "response-pending"
        elif event_type == "exchange":
            self.state = "connected"
            self.current_acquisition_id = event["acquisition_id"]
        elif event_type == "error":
            self.state = "transport-error"
            self.error_detail = event["detail"]
        elif event_type == "finished":
            self.state = "disconnected"
            self.current_acquisition_id = None
            self.finish_reason = event["reason"]
        self.last_event = event
        return event

    def poll(self, now_ms: int) -> str:
        if (
            self.state not in {"disconnected", "transport-error", "stale-state"}
            and self.last_heartbeat_ms is not None
            and now_ms - self.last_heartbeat_ms >= self.stale_after_ms
        ):
            self.state = "stale-state"
        return self.state


class StopController:
    """Map button, hotkey, and Ctrl+C to one idempotent public stop write."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.requested = False
        self.source: str | None = None
        self._lock = threading.Lock()

    def request(self, source: str) -> bool:
        if source not in {"button", "hotkey", "ctrl_c"}:
            raise ValueError("unknown TUI stop source")
        with self._lock:
            if self.requested:
                return False
            self.requested = True
            self.source = source
            self.stream.write("stop\n")
            self.stream.flush()
            return True


class ReviewDraft:
    """In-memory review edits whose only persistent action is explicit save."""

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository
        self.document = copy.deepcopy(repository.review)

    def update(self, signal_ref: str, **changes: Any) -> None:
        allowed = {
            "catalog_variant_id", "display_name_override", "formula_override",
            "unit_override", "evidence_state", "notes", "evidence_refs",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported review edit fields: {sorted(unknown)}")
        matches = [row for row in self.document["signals"] if row["signal_ref"] == signal_ref]
        if len(matches) != 1:
            raise ValueError(f"review signal must resolve exactly once: {signal_ref}")
        matches[0].update(changes)

    def save(self) -> None:
        self.repository.write_authored_document("signal_review", self.document)


class PolicyDraft:
    """In-memory edits to existing reviewed collection objectives."""

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository
        self.document = copy.deepcopy(repository.policy)

    def update(self, signal_ref: str, objective_id: str, **changes: Any) -> None:
        allowed = {
            "included", "class", "priority", "target_interval_ms",
            "maximum_interval_ms", "deadband", "targets", "applicability",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported policy edit fields: {sorted(unknown)}")
        matches = [
            row for row in self.document["objectives"]
            if row["signal_ref"] == signal_ref and row["objective_id"] == objective_id
        ]
        if len(matches) != 1:
            raise ValueError("policy objective must resolve exactly once")
        matches[0].update(changes)

    def save(self) -> None:
        self.repository.write_authored_document("collection_policy", self.document)


class CollectorChild:
    """One `telemetry collect --json` child; never speaks to C# directly."""

    def __init__(
        self,
        project_root: Path,
        plan_path: Path,
        endpoint_id: str,
        monitor: TransportMonitor,
        *,
        note: str | None = None,
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        self.monitor = monitor
        self.monotonic_ms = monotonic_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self.command = [
            sys.executable, "-m", "toyota_telemetry.cli", "collect",
            "--project-root", str(project_root), "--plan", str(plan_path),
            "--endpoint", endpoint_id, "--json",
        ]
        if note:
            self.command.extend(("--note", note))
        self.process: subprocess.Popen[str] | None = None
        self.stop_controller: StopController | None = None
        self.failure: Exception | None = None
        self.stderr_lines: list[str] = []

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("collector child is already started")
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self.process.stdin is not None
        self.stop_controller = StopController(self.process.stdin)
        threading.Thread(target=self._read_events, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def request_stop(self, source: str) -> bool:
        if self.stop_controller is None:
            raise RuntimeError("collector child is not started")
        return self.stop_controller.request(source)

    def wait(self, timeout: float | None = None) -> int:
        if self.process is None:
            raise RuntimeError("collector child is not started")
        try:
            result = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            result = self.process.wait()
        if self.failure is not None:
            raise RuntimeError(f"collector event contract failed: {self.failure}") from self.failure
        return result

    def _read_events(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.monitor.accept(line, self.monotonic_ms())
        except Exception as exc:
            self.failure = exc
            if self.process.poll() is None:
                self.process.kill()

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        self.stderr_lines.extend(line.rstrip("\r\n") for line in self.process.stderr)


class OperatorWorkspace:
    """Small interactive shell around offline project services and CollectorChild."""

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository
        self.monitor = TransportMonitor()

    def plan_summary(self, plan_path: Path) -> dict[str, Any]:
        data = plan_path.read_bytes()
        plan = json.loads(data.decode("utf-8"))
        return {
            "admitted": plan.get("admitted") is True,
            "plan_sha256": hashlib.sha256(data).hexdigest(),
            "endpoints": [row["endpoint_id"] for row in plan["endpoint_sessions"]],
            "acquisition_count": len(plan["acquisitions"]),
            "objective_count": len(plan["objectives"]),
            "objective_progress": [
                {
                    "objective": f"{row['signal_ref']}#{row['objective_id']}",
                    "prior_progress": row.get("prior_progress"),
                }
                for row in plan["objectives"]
            ],
            "capture_root": self.repository.paths.captures.relative_to(self.repository.root).as_posix(),
            "backend_kind": (
                self.repository.local_settings["backend"]["kind"]
                if self.repository.local_settings is not None else None
            ),
        }

    def evidence_review(self, signal_ref: str) -> dict[str, Any]:
        progress = [
            row for row in self.repository.replay_progress()
            if row["signal_ref"] == signal_ref
        ]
        return {
            "signal_ref": signal_ref,
            "observations": self.repository.signal_observations(signal_ref),
            "objective_progress": progress,
        }

    def schedule_replay(self, run_id: str) -> list[dict[str, Any]]:
        runs = load_capture_tree(self.repository.paths.captures, self.repository.root)
        matches = [run for run in runs if run.run_id == run_id]
        if len(matches) != 1:
            raise ValueError(f"capture run must resolve exactly once: {run_id}")
        return [
            {
                "run_sequence": exchange["run_sequence"],
                "acquisition_id": exchange["acquisition_id"],
                "scheduler_reason": exchange["scheduler_reason"],
                "objective_ids": exchange["objective_ids"],
            }
            for segment in matches[0].segments
            for exchange in segment.exchanges
        ]

    def run(self) -> int:
        plan_path = self.repository.paths.plans / "admitted-plan.json"
        while True:
            print("\nTelemetry workspace")
            print(
                "  [p] run plan  [c] collection goals  [s] collect  "
                "[e] evidence  [h] history  [r] signal curation  [q] quit"
            )
            choice = input("> ").strip().lower()
            if choice == "q":
                return 0
            if choice == "p":
                self._show_plan(plan_path)
            elif choice == "c":
                self._edit_policy()
            elif choice == "s":
                self._start_collection(plan_path)
            elif choice == "r":
                self._edit_review()
            elif choice == "e":
                print(json.dumps(self.evidence_review(input("Signal reference: ").strip()), indent=2))
            elif choice == "h":
                print(json.dumps(self.schedule_replay(input("Run ID: ").strip()), indent=2))

    def _show_plan(self, plan_path: Path) -> None:
        summary = self.plan_summary(plan_path)
        print(json.dumps(summary, indent=2))

    def _start_collection(self, plan_path: Path) -> None:
        summary = self.plan_summary(plan_path)
        if not summary["admitted"] or summary["backend_kind"] is None:
            raise ValueError("an admitted plan and local adapter settings are required")
        endpoint = summary["endpoints"][0]
        if len(summary["endpoints"]) > 1:
            endpoint = input("Endpoint: ").strip()
            if endpoint not in summary["endpoints"]:
                raise ValueError("selected endpoint is not in the admitted plan")
        print(json.dumps({**summary, "selected_endpoint": endpoint}, indent=2))
        if input("Start live collection? Type YES: ").strip() != "YES":
            return
        note = input("Optional run note: ") or None
        child = CollectorChild(
            self.repository.root, plan_path, endpoint, self.monitor, note=note
        )
        child.start()
        print("Live collection. Press S to stop.")
        try:
            import msvcrt
            last_state = None
            while child.process is not None and child.process.poll() is None:
                state = self.monitor.poll(time.monotonic_ns() // 1_000_000)
                if state != last_state:
                    print("Transport:", state)
                    last_state = state
                if msvcrt.kbhit() and msvcrt.getwch().lower() == "s":
                    child.request_stop("hotkey")
                time.sleep(0.05)
        except KeyboardInterrupt:
            child.request_stop("ctrl_c")
        exit_code = child.wait(timeout=3)
        print("Collection exited:", exit_code, self.monitor.finish_reason)

    def _edit_review(self) -> None:
        draft = ReviewDraft(self.repository)
        signal_ref = input("Signal reference: ").strip()
        matches = [row for row in draft.document["signals"] if row["signal_ref"] == signal_ref]
        if len(matches) != 1:
            raise ValueError("signal is not present in review source")
        current = matches[0]
        evidence = input(f"Evidence state [{current['evidence_state']}]: ").strip()
        notes = input(f"Notes [{current['notes']}]: ").strip()
        draft.update(
            signal_ref,
            evidence_state=evidence or current["evidence_state"],
            notes=notes or current["notes"],
        )
        if input("Save tracked review? Type SAVE: ").strip() == "SAVE":
            draft.save()

    def _edit_policy(self) -> None:
        draft = PolicyDraft(self.repository)
        signal_ref = input("Signal reference: ").strip()
        objective_id = input("Objective ID: ").strip()
        matches = [
            row for row in draft.document["objectives"]
            if row["signal_ref"] == signal_ref and row["objective_id"] == objective_id
        ]
        if len(matches) != 1:
            raise ValueError("objective is not present in collection policy")
        current = matches[0]
        included = input(f"Included [{'yes' if current['included'] else 'no'}]: ").strip().lower()
        target = input(f"Desired interval ms [{current['target_interval_ms']}]: ").strip()
        changes: dict[str, Any] = {}
        if included in {"yes", "no"}:
            changes["included"] = included == "yes"
        if target:
            changes["target_interval_ms"] = int(target)
        draft.update(signal_ref, objective_id, **changes)
        if input("Save tracked policy? Type SAVE: ").strip() == "SAVE":
            draft.save()
