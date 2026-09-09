from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests.test_phase_c import current_repository
from toyota_telemetry.cli import main as cli_main
from toyota_telemetry.repository import ProjectRepository
from toyota_telemetry.tui import (
    CollectionTask,
    OperatorTui,
    PolicyDraft,
    ReviewDraft,
    StopController,
    TransportDraft,
    TransportMonitor,
    main,
)


def event(name: str, **fields) -> str:
    return json.dumps({"event": name, "schema_version": 1, "run_id": "run-a", **fields})


class TransportMonitorTests(unittest.TestCase):
    def test_states_remain_distinct_and_stale_is_parent_inferred(self) -> None:
        monitor = TransportMonitor(stale_after_ms=3000)
        monitor.accept(event("started", backend_kind="synthetic"), 0)
        self.assertEqual(monitor.state, "connected")
        monitor.accept(event("response_pending", pending_count=1, pending_duration_ms=0), 100)
        self.assertEqual(monitor.state, "response-pending")
        self.assertEqual(monitor.poll(2999), "response-pending")
        self.assertEqual(monitor.poll(3000), "stale-state")
        monitor.accept(event("error", detail="lost"), 3100)
        self.assertEqual(monitor.state, "transport-error")
        monitor.accept(event("finished", reason="transport_lost"), 3200)
        self.assertEqual(monitor.state, "disconnected")

    def test_event_schema_mismatch_hard_fails(self) -> None:
        monitor = TransportMonitor()
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            monitor.accept(event("started", backend_kind="x").replace(
                '"schema_version": 1', '"schema_version": 2'
            ), 0)

    def test_all_stop_inputs_share_one_idempotent_write(self) -> None:
        for source in ("button", "hotkey", "ctrl_c"):
            with self.subTest(source=source):
                stop_event = threading.Event()
                controller = StopController(stop_event)
                self.assertTrue(controller.request(source))
                self.assertFalse(controller.request("button"))
                self.assertTrue(stop_event.is_set())


class OperatorTuiTests(unittest.TestCase):
    def test_tui_is_not_a_telemetry_subcommand(self) -> None:
        for command in ("workspace", "review"):
            with self.subTest(command=command), mock.patch(
                "sys.argv", ["telemetry", command]
            ), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as raised:
                    cli_main()
                self.assertEqual(raised.exception.code, 2)

    def test_tui_has_a_standalone_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            current_repository(root)
            arguments = ["telemetry-tui", "--project-root", str(root)]
            with mock.patch("sys.argv", arguments), mock.patch(
                "builtins.input", return_value="q"
            ):
                self.assertEqual(main(), 0)

    def test_open_and_quit_never_starts_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, _ = current_repository(root)
            with mock.patch("builtins.input", return_value="q"), mock.patch(
                "toyota_telemetry.tui.run_collection"
            ) as collect:
                self.assertEqual(OperatorTui(repository).run(), 0)
                collect.assert_not_called()

    def test_collection_task_calls_library_service_and_consumes_events(self) -> None:
        monitor = TransportMonitor()

        def collect(*args, **kwargs):
            sink = kwargs["event_sink"]
            sink({"event": "started", "schema_version": 1, "run_id": "run-a", "backend_kind": "synthetic"})
            sink({"event": "finished", "schema_version": 1, "run_id": "run-a", "reason": "plan_complete"})
            return "plan_complete"

        with mock.patch("toyota_telemetry.tui.run_collection", side_effect=collect) as service:
            task = CollectionTask(Path("vehicle"), Path("plan.json"), "ecu", monitor)
            task.start()
            self.assertEqual(task.wait(timeout=1), 0)
        service.assert_called_once()
        self.assertEqual(monitor.finish_reason, "plan_complete")

    def test_review_is_memory_only_until_explicit_atomic_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, _ = current_repository(root)
            path = repository.paths.signal_review
            before = path.read_bytes()
            signal_ref = repository.review["signals"][0]["signal_ref"]
            draft = ReviewDraft(repository)
            draft.update(signal_ref, notes="TUI test edit")
            self.assertEqual(path.read_bytes(), before)
            draft.save()
            saved = ProjectRepository(repository.root).review
            self.assertEqual(saved["signals"][0]["notes"], "TUI test edit")

    def test_policy_is_memory_only_until_explicit_atomic_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = current_repository(root)
            from tests.test_phase_c import author_collection_policy
            repository = author_collection_policy(repository, rows, minimum_observations=2)
            path = repository.paths.collection_policy
            before = path.read_bytes()
            row = repository.policy["objectives"][0]
            draft = PolicyDraft(repository)
            draft.update(row["signal_ref"], row["objective_id"], priority=75)
            self.assertEqual(path.read_bytes(), before)
            draft.save()
            self.assertEqual(ProjectRepository(repository.root).policy["objectives"][0]["priority"], 75)

    def test_endpoint_promotion_is_memory_only_until_explicit_atomic_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, _ = current_repository(root)
            candidate = {
                "candidate_id": "7d2-7da-session-a1769d23a560",
                "tester_can_id": "0x7D2",
                "response_can_id": "0x7DA",
                "session_recipe_id": "session-a1769d23a560",
                "responded": True,
                "observed_utc": "2026-01-01T00:00:00Z",
                "result": {},
            }
            repository.record_discovery_candidate(candidate)
            path = repository.paths.transport
            before = path.read_bytes()
            draft = TransportDraft(repository)
            draft.promote(
                candidate["candidate_id"], [397], ["docs/findings/example.md"]
            )
            self.assertEqual(path.read_bytes(), before)
            draft.save()
            saved = ProjectRepository(repository.root).transport
            promoted = next(
                row for row in saved["endpoints"] if row["id"] == candidate["candidate_id"]
            )
            self.assertEqual(promoted["ecu_keys"], [397])


if __name__ == "__main__":
    unittest.main()
