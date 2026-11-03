from __future__ import annotations

import copy
import json
import unittest

from toyota_telemetry.worker_protocol import (
    build_worker_config,
    exchange_command,
    parse_event,
    stop_command,
)


def plan() -> dict:
    return {
        "document_type": "telemetry.admitted-plan",
        "schema_version": 1,
        "project_id": "test-project",
        "profile_id": "test-profile",
        "admitted": True,
        "live_authorized": True,
        "endpoint_sessions": [{
            "endpoint_id": "700-708-session",
            "tester_can_id": "0x700",
            "response_can_id": "0x708",
            "session_recipe_id": "extended",
            "network": {"type": "classical-can", "bitrate": 500000, "addressing": "normal"},
            "request_start_floor_ms": 100,
            "initial_response_timeout_ms": 1000,
            "response_pending_budget_ms": 10000,
            "transport_retries": 2,
            "session_setup": [{
                "request_hex": "3E 00",
                "expected_prefix_hex": "7E 00",
                "label": "tester present",
                "tolerate_negative": True,
            }],
        }],
        "acquisitions": [{
            "acquisition_id": "700-708-session/0x1001",
            "endpoint_id": "700-708-session",
            "data_id": "0x1001",
            "request_hex": "22 10 01",
        }],
    }


class WorkerProtocolTests(unittest.TestCase):
    def test_config_contains_only_endpoint_and_local_adapter_scope(self) -> None:
        config = build_worker_config(plan(), "700-708-session", {
            "kind": "pcan",
            "executable": "state/bin/telemetry-pcan.exe",
            "settings": {"channel": "PCAN_USBBUS1"},
        })
        self.assertEqual(config["document_type"], "telemetry.adapter-worker")
        self.assertEqual(config["allowed_transactions"][0]["expected_prefix_hex"], "62 10 01")
        self.assertEqual(
            config["allowed_transactions"][0]["transaction_id"], "700-708-session/0x1001"
        )
        self.assertEqual(config["device"], {"pcan": {"channel": "PCAN_USBBUS1"}})
        encoded = json.dumps(config)
        self.assertNotIn("objective", encoded)
        self.assertNotIn("executable", encoded)

    def test_plan_or_endpoint_mismatch_cannot_authorize_worker(self) -> None:
        invalid = plan()
        invalid["live_authorized"] = False
        with self.assertRaisesRegex(ValueError, "not live-authorized"):
            build_worker_config(invalid, "700-708-session", {
                "kind": "pcan", "executable": "x", "settings": {},
            })
        with self.assertRaisesRegex(ValueError, "resolve exactly once"):
            build_worker_config(plan(), "missing", {
                "kind": "pcan", "executable": "x", "settings": {},
            })

    def test_commands_are_exact_and_canonical(self) -> None:
        self.assertEqual(
            exchange_command(7, "700-708-session/0x1001"),
            '{"command":"exchange","schema_version":1,"sequence":7,'
            '"transaction_id":"700-708-session/0x1001"}',
        )
        self.assertEqual(stop_command(), '{"command":"stop","schema_version":1}')
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            exchange_command(1, "")

    def test_every_worker_event_shape_is_exact(self) -> None:
        events = [
            {"event": "ready", "schema_version": 1, "endpoint_id": "e", "backend_kind": "synthetic"},
            {"event": "response_pending", "schema_version": 1, "sequence": 1,
             "pending_count": 1, "pending_duration_ms": 0},
            {"event": "result", "schema_version": 1, "sequence": 1, "outcome": "positive",
             "response_hex": "62 10 01 12", "nrc": None, "pending_count": 0,
             "pending_duration_ms": 0, "latency_ms": 4, "detail": None},
            {"event": "stopped", "schema_version": 1, "reason": "stop"},
        ]
        for event in events:
            self.assertEqual(parse_event(json.dumps(event)), event)

        extra = copy.deepcopy(events[2])
        extra["unknown"] = 1
        with self.assertRaisesRegex(ValueError, "fields mismatch"):
            parse_event(json.dumps(extra))
        wrong_version = copy.deepcopy(events[0])
        wrong_version["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            parse_event(json.dumps(wrong_version))


if __name__ == "__main__":
    unittest.main()
