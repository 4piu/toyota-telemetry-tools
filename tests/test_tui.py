from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_phase_c import current_repository
from toyota_telemetry.repository import ProjectRepository
from toyota_telemetry.tui import (
    OperatorTui,
    PolicyDraft,
    ReviewDraft,
    StopController,
    TransportMonitor,
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
                stream = io.StringIO()
                controller = StopController(stream)
                self.assertTrue(controller.request(source))
                self.assertFalse(controller.request("button"))
                self.assertEqual(stream.getvalue(), "stop\n")


class ReviewDraftTests(unittest.TestCase):
    def test_open_and_quit_never_starts_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, _ = current_repository(root)
            with mock.patch("builtins.input", return_value="q"), mock.patch(
                "toyota_telemetry.tui.subprocess.Popen"
            ) as popen:
                self.assertEqual(OperatorTui(repository).run(), 0)
                popen.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
