from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
import tempfile
import unittest
import uuid

from toyota_telemetry.capture_store import canonical_document_bytes, load_capture_tree
from toyota_telemetry.capture_writer import NativeRunWriter
from toyota_telemetry.collector import CollectionCrashed, CollectionRunner, parse_public_event


def admitted_plan(*, segment_seconds: int = 60, minimum_observations: int = 100) -> tuple[dict, bytes]:
    body = {
        "document_type": "telemetry.admitted-plan",
        "schema_version": 1,
        "project_id": "test-project",
        "profile_id": "test-profile",
        "created_utc": "2026-01-01T00:00:00Z",
        "source_hashes": {"policy": "a" * 64},
        "admitted": True,
        "live_authorized": True,
        "admission": {},
        "stress": {},
        "storage": {
            "segment_seconds": segment_seconds,
            "segment_max_mib": 16,
            "capture_tree_max_mib": 32,
            "disk_reserve_mib": 1,
        },
        "endpoint_sessions": [{
            "endpoint_id": "700-708-session",
            "tester_can_id": "0x700",
            "response_can_id": "0x708",
            "session_recipe_id": "extended",
            "network": {"type": "classical-can", "bitrate": 500000, "addressing": "normal"},
            "request_start_floor_ms": 10,
            "initial_response_timeout_ms": 100,
            "response_pending_budget_ms": 1000,
            "transport_retries": 1,
            "session_setup": [{
                "request_hex": "3E 00", "expected_prefix_hex": "7E 00",
                "label": "tester present", "tolerate_negative": True,
            }],
        }],
        "acquisitions": [{
            "acquisition_id": "700-708-session/0x1001",
            "endpoint_id": "700-708-session",
            "data_id": "0x1001",
            "priority": 50,
            "target_interval_ms": 100,
            "maximum_interval_ms": 200,
            "cost": {"latency_p95_ms": 10},
            "request_hex": "22 10 01",
            "objective_ids": ["372/0x1001/0..15"],
            "dependency_for": [],
        }],
        "objectives": [{
            "signal_ref": "372/0x1001/0..15",
            "included": True,
            "requirements": {"minimum_samples": minimum_observations},
            "minimum_difference": None,
            "condition": None,
            "acquisition_id": "700-708-session/0x1001",
            "condition_dependencies": [],
            "prior_progress": None,
        }],
        "decodes": [{
            "acquisition_id": "700-708-session/0x1001",
            "signal_ref": "372/0x1001/0..15",
            "first_bit": 0,
            "last_bit": 15,
            "formula": "raw / 10",
        }],
    }
    body["content_sha256"] = hashlib.sha256(canonical_document_bytes(body)).hexdigest()
    return body, canonical_document_bytes(body)


def result(outcome: str = "positive") -> dict:
    return {
        "event": "result",
        "schema_version": 1,
        "sequence": 0,
        "outcome": outcome,
        "response_hex": "62 10 01 00 7B" if outcome == "positive" else None,
        "nrc": None,
        "pending_count": 0,
        "pending_duration_ms": 0,
        "latency_ms": 10,
        "detail": None,
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 0

    def monotonic_ms(self) -> int:
        return self.now

    def wait_ms(self, delay: int) -> None:
        self.now += delay


class ScriptedWorker:
    def __init__(
        self,
        script: list[dict | Exception],
        *,
        clock: FakeClock | None = None,
        advance_ms: int = 10,
        stop_event: threading.Event | None = None,
        stop_after: int | None = None,
        pending: bool = False,
        real_delay: float = 0,
    ) -> None:
        self.script = list(script)
        self.clock = clock
        self.advance_ms = advance_ms
        self.stop_event = stop_event
        self.stop_after = stop_after
        self.pending = pending
        self.real_delay = real_delay
        self.start_count = 0
        self.exchange_count = 0
        self.stop_count = 0
        self.terminated = False

    def start(self) -> dict:
        self.start_count += 1
        return {"event": "ready", "schema_version": 1,
                "endpoint_id": "700-708-session", "backend_kind": "synthetic"}

    def exchange(self, sequence: int, transaction_id: str, event_sink) -> dict:
        self.exchange_count += 1
        if self.pending:
            event_sink({"event": "response_pending", "schema_version": 1,
                        "sequence": sequence, "pending_count": 1, "pending_duration_ms": 0})
        if self.real_delay:
            time.sleep(self.real_delay)
        if self.clock is not None:
            self.clock.now += self.advance_ms
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        value = dict(item)
        value["sequence"] = sequence
        if self.stop_after == self.exchange_count and self.stop_event is not None:
            self.stop_event.set()
        return value

    def request_stop(self) -> None:
        self.stop_count += 1

    def close(self) -> None:
        pass

    def terminate(self) -> None:
        self.terminated = True


class StopAwareWorker(ScriptedWorker):
    def exchange(self, sequence: int, transaction_id: str, event_sink) -> dict:
        self.exchange_count += 1
        deadline = time.monotonic() + 1
        while self.stop_count == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        value = result("abandoned")
        value["sequence"] = sequence
        return value


class CollectorTests(unittest.TestCase):
    def test_public_event_schema_is_exact(self) -> None:
        line = ('{"event":"heartbeat","schema_version":1,"run_id":"run-a",'
                '"state":"connected","current_acquisition_id":null}')
        self.assertEqual(parse_public_event(line)["state"], "connected")
        with self.assertRaisesRegex(ValueError, "fields mismatch"):
            parse_public_event(line[:-1] + ',"extra":1}')
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            parse_public_event(line.replace('"schema_version":1', '"schema_version":2'))

    def make_runner(self, root: Path, worker: ScriptedWorker, stop: threading.Event,
                    clock: FakeClock | None = None, segment_seconds: int = 60,
                    heartbeat_ms: int = 1000,
                    minimum_observations: int = 100,
                    max_duration_ms: int | None = None) -> tuple[CollectionRunner, list[dict]]:
        plan, encoded = admitted_plan(
            segment_seconds=segment_seconds,
            minimum_observations=minimum_observations,
        )
        writer = NativeRunWriter(
            root / "captures", plan, encoded, run_id=str(uuid.uuid4()),
            created_utc="2026-01-01T00:00:00Z", backend_kind="synthetic",
            disk_free_bytes=lambda _: 100_000_000,
        )
        events: list[dict] = []
        runner = CollectionRunner(
            plan, writer, worker, event_sink=events.append, stop_event=stop,
            monotonic_ms=clock.monotonic_ms if clock else None,
            wait_ms=clock.wait_ms if clock else None,
            utc_now=lambda: "2026-01-01T00:00:01Z", heartbeat_ms=heartbeat_ms,
            max_duration_ms=max_duration_ms,
        )
        return runner, events

    def test_timeout_retries_then_records_decoded_positive_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            clock = FakeClock()
            worker = ScriptedWorker(
                [result("timeout"), result()], clock=clock, stop_event=stop, stop_after=2
            )
            runner, _ = self.make_runner(Path(directory), worker, stop, clock)
            self.assertEqual(runner.run(), "operator_stop")
            run = load_capture_tree(Path(directory) / "captures", Path(directory))[0]
            exchange = run.segments[0].exchanges[0]
            self.assertEqual(exchange["retry_count"], 1)
            self.assertEqual(exchange["observations"][0]["raw_hex"], "00 7B")
            self.assertEqual(exchange["observations"][0]["decoded_value"], 12.3)

    def test_rotation_does_not_restart_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            clock = FakeClock()
            worker = ScriptedWorker(
                [result(), result()], clock=clock, advance_ms=1100,
                stop_event=stop, stop_after=2,
            )
            runner, _ = self.make_runner(
                Path(directory), worker, stop, clock, segment_seconds=1
            )
            runner.run()
            run = load_capture_tree(Path(directory) / "captures", Path(directory))[0]
            self.assertEqual(worker.start_count, 1)
            self.assertEqual([s.end["reason"] for s in run.segments], ["rotation", "operator_stop"])

    def test_a_time_slice_ends_the_run_as_a_complete_hand_over(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            clock = FakeClock()
            worker = ScriptedWorker([result()] * 5, clock=clock, advance_ms=1100)
            runner, events = self.make_runner(Path(directory), worker, stop, clock, max_duration_ms=2000)
            self.assertEqual(runner.run(), "slice_end")
            self.assertFalse(stop.is_set())
            run = load_capture_tree(Path(directory) / "captures", Path(directory))[0]
            self.assertEqual(run.segments[-1].end["reason"], "slice_end")
            run_end = Path(directory) / "captures" / run.run_id / "run-end.json"
            self.assertIn(b"\"slice_end\"", run_end.read_bytes())
            self.assertEqual(len(run.segments[0].exchanges), 2)
            self.assertEqual(events[-1]["reason"], "slice_end")

    def test_transport_error_keeps_clean_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            clock = FakeClock()
            worker = ScriptedWorker([result("transport_error")], clock=clock)
            runner, _ = self.make_runner(Path(directory), worker, stop, clock)
            self.assertEqual(runner.run(), "transport_lost")
            run = load_capture_tree(Path(directory) / "captures", Path(directory))[0]
            self.assertEqual(run.segments[0].end["reason"], "transport_lost")

    def test_objective_completion_stops_plan_without_operator_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            clock = FakeClock()
            worker = ScriptedWorker([result()], clock=clock)
            runner, _ = self.make_runner(
                Path(directory), worker, stop, clock, minimum_observations=1
            )
            self.assertEqual(runner.run(), "plan_complete")
            run = load_capture_tree(Path(directory) / "captures", Path(directory))[0]
            self.assertEqual(run.segments[0].end["reason"], "plan_complete")

    def test_run_completes_on_its_own_endpoints_targets_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = admitted_plan(minimum_observations=1)
            plan["endpoint_sessions"].append({
                **plan["endpoint_sessions"][0], "endpoint_id": "710-718-session",
                "tester_can_id": "0x710", "response_can_id": "0x718",
            })
            plan["acquisitions"].append({
                **plan["acquisitions"][0], "acquisition_id": "710-718-session/0x2001",
                "endpoint_id": "710-718-session", "data_id": "0x2001", "request_hex": "22 20 01",
                "objective_ids": ["373/0x2001/0..15"],
            })
            plan["objectives"].append({
                **plan["objectives"][0], "signal_ref": "373/0x2001/0..15",
                "acquisition_id": "710-718-session/0x2001",
            })
            plan.pop("content_sha256")
            plan["content_sha256"] = hashlib.sha256(canonical_document_bytes(plan)).hexdigest()
            encoded = canonical_document_bytes(plan)
            writer = NativeRunWriter(
                Path(directory) / "captures", plan, encoded, run_id=str(uuid.uuid4()),
                created_utc="2026-01-01T00:00:00Z", backend_kind="synthetic",
                endpoint_id="700-708-session", disk_free_bytes=lambda _: 100_000_000,
            )
            clock = FakeClock()
            events: list[dict] = []
            runner = CollectionRunner(
                plan, writer, ScriptedWorker([result()], clock=clock), event_sink=events.append,
                stop_event=threading.Event(), monotonic_ms=clock.monotonic_ms, wait_ms=clock.wait_ms,
                utc_now=lambda: "2026-01-01T00:00:01Z",
            )
            self.assertEqual(runner.run(), "plan_complete")
            progress = [event for event in events if event["event"] == "progress"][-1]
            self.assertEqual((progress["targets"], progress["targets_complete"], progress["exchanges"]), (1, 1, 1))

    def test_worker_crash_leaves_incomplete_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            clock = FakeClock()
            worker = ScriptedWorker([RuntimeError("synthetic crash")], clock=clock)
            runner, _ = self.make_runner(Path(directory), worker, stop, clock)
            with self.assertRaisesRegex(CollectionCrashed, "synthetic crash"):
                runner.run()
            self.assertTrue(worker.terminated)
            self.assertEqual(load_capture_tree(Path(directory) / "captures", Path(directory)), ())
            self.assertEqual(len(list((Path(directory) / "captures").rglob("*.incomplete.jsonl"))), 1)

    def test_operator_stop_abandons_inflight_request_through_one_stop_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            worker = StopAwareWorker([])
            runner, _ = self.make_runner(Path(directory), worker, stop, heartbeat_ms=20)
            threading.Timer(0.03, stop.set).start()
            self.assertEqual(runner.run(), "operator_stop")
            self.assertGreaterEqual(worker.stop_count, 1)
            run = load_capture_tree(Path(directory) / "captures", Path(directory))[0]
            self.assertEqual(run.segments[0].exchanges[0]["outcome"], "abandoned")
            self.assertEqual(run.segments[0].end["reason"], "operator_stop")

    def test_heartbeat_continues_while_response_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            worker = ScriptedWorker(
                [result()], stop_event=stop, stop_after=1, pending=True, real_delay=0.12
            )
            runner, events = self.make_runner(
                Path(directory), worker, stop, heartbeat_ms=25
            )
            runner.run()
            heartbeats = [event for event in events if event["event"] == "heartbeat"]
            self.assertGreaterEqual(len(heartbeats), 2)
            self.assertTrue(any(event["state"] == "response-pending" for event in heartbeats))


if __name__ == "__main__":
    unittest.main()
