from __future__ import annotations

from contextlib import contextmanager
import threading
import unittest
from unittest import mock

from toyota_telemetry import keep_awake as module
from toyota_telemetry.operator import collection
from toyota_telemetry.operator.collection import CollectionReadiness


class KeepAwakeTests(unittest.TestCase):
    def test_windows_holds_off_sleep_until_the_block_ends_even_on_error(self) -> None:
        calls: list[int] = []
        with mock.patch.object(module.os, "name", "nt"), \
                mock.patch.object(module, "_set_execution_state", side_effect=lambda flags: calls.append(flags) or 1):
            with self.assertRaises(RuntimeError):
                with module.keep_awake():
                    self.assertEqual(calls, [module.ES_CONTINUOUS | module.ES_SYSTEM_REQUIRED | module.ES_DISPLAY_REQUIRED])
                    raise RuntimeError("worker crashed")
        # Released: only ES_CONTINUOUS, so the display and system may sleep again.
        self.assertEqual(calls[-1], module.ES_CONTINUOUS)

    def test_collection_runs_inside_keep_awake(self) -> None:
        state = {"awake": False, "seen": None}

        @contextmanager
        def awake():
            state["awake"] = True
            try:
                yield
            finally:
                state["awake"] = False

        def run(*_args, **_kwargs):
            state["seen"] = state["awake"]
            return "plan_complete"

        readiness = CollectionReadiness(True, endpoint_id="A")
        repository = mock.MagicMock()
        with mock.patch.object(collection, "keep_awake", awake), \
                mock.patch.object(collection, "LiveLock", mock.MagicMock()), \
                mock.patch.object(collection, "run_collection", run), \
                mock.patch.object(collection, "refresh_cache"), \
                mock.patch.object(collection, "ProjectRepository"):
            reason = collection.start_collection(
                repository, {}, readiness, note=None, event_sink=lambda _event: None, stop_event=threading.Event(),
            )
        self.assertEqual(reason, "plan_complete")
        self.assertTrue(state["seen"])
        self.assertFalse(state["awake"])


if __name__ == "__main__":
    unittest.main()
