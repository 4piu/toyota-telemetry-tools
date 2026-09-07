from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from toyota_telemetry.capture_store import canonical_document_bytes, load_capture_tree
from toyota_telemetry.capture_writer import (
    CaptureLimitReached,
    NativeRunWriter,
    recover_incomplete_segment,
)


def plan_bytes(*, segment_seconds: int = 60, segment_max_mib: int = 16) -> tuple[dict, bytes]:
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
            "segment_max_mib": segment_max_mib,
            "capture_tree_max_mib": 32,
            "disk_reserve_mib": 1,
        },
        "endpoint_sessions": [{"endpoint_id": "700-708-session"}],
        "acquisitions": [],
        "objectives": [],
    }
    body["content_sha256"] = hashlib.sha256(canonical_document_bytes(body)).hexdigest()
    return body, canonical_document_bytes(body)


def exchange(*, payload_bytes: int = 2) -> dict:
    raw = bytes([0x12]) * payload_bytes
    return {
        "acquisition_id": "700-708-session/0x1001",
        "endpoint_id": "700-708-session",
        "data_id": "0x1001",
        "response_utc": "2026-01-01T00:00:00Z",
        "request_elapsed_ms": 0,
        "response_elapsed_ms": 10,
        "request_hex": "22 10 01",
        "response_hex": "62 10 01 " + raw.hex(" ").upper(),
        "outcome": "positive",
        "negative_response_code": None,
        "pending_count": 0,
        "pending_duration_ms": 0,
        "latency_ms": 10,
        "retry_count": 0,
        "scheduler_reason": "target_due",
        "objective_ids": ["372/0x1001/0..7#observe"],
        "observations": [{
            "signal_ref": "372/0x1001/0..7",
            "raw_hex": "12",
            "decoded_value": 18.0,
            "applicability": "true",
        }],
    }


class NativeRunWriterTests(unittest.TestCase):
    def make_writer(self, root: Path, **changes: object) -> NativeRunWriter:
        plan, encoded = plan_bytes(
            segment_seconds=int(changes.pop("segment_seconds", 60)),
            segment_max_mib=int(changes.pop("segment_max_mib", 16)),
        )
        return NativeRunWriter(
            root / "captures",
            plan,
            encoded,
            run_id=str(uuid.uuid4()),
            created_utc="2026-01-01T00:00:00Z",
            backend_kind="synthetic",
            disk_free_bytes=changes.pop("disk_free_bytes", lambda _: 10_000_000),
            storage_unit_bytes=int(changes.pop("storage_unit_bytes", 1024)),
            **changes,
        )

    def test_rotation_preserves_run_sequence_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = self.make_writer(root, segment_seconds=1)
            writer.append_exchange(exchange())
            writer.rotate(1000)
            second = exchange()
            second["request_elapsed_ms"] = 1000
            second["response_elapsed_ms"] = 1010
            writer.append_exchange(second)
            writer.finish("operator_stop")

            runs = load_capture_tree(root / "captures", root)
            self.assertEqual(len(runs), 1)
            self.assertEqual([segment.end["reason"] for segment in runs[0].segments], ["rotation", "operator_stop"])
            self.assertEqual(
                [item["run_sequence"] for segment in runs[0].segments for item in segment.exchanges],
                [1, 2],
            )
            self.assertEqual(runs[0].start["backend_kind"], "synthetic")

    def test_context_manager_crash_keeps_incomplete_file_uncredited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = self.make_writer(root)
            run_directory = writer.run_directory
            with writer:
                writer.append_exchange(exchange())
            self.assertTrue(next(run_directory.glob("*.incomplete.jsonl")).is_file())
            self.assertFalse((run_directory / "run-end.json").exists())
            self.assertEqual(load_capture_tree(root / "captures", root), ())

    def test_crash_after_rotation_keeps_prior_complete_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = self.make_writer(root, segment_seconds=1)
            writer.append_exchange(exchange())
            writer.rotate(1000)
            writer.append_exchange({
                **exchange(),
                "request_elapsed_ms": 1000,
                "response_elapsed_ms": 1010,
            })
            writer.crash()
            run = load_capture_tree(root / "captures", root)[0]
            self.assertEqual(len(run.segments), 1)
            self.assertEqual(len(run.segments[0].exchanges), 1)

    def test_size_limit_finalizes_valid_prefix_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = self.make_writer(root, segment_max_mib=1, storage_unit_bytes=1024)
            with self.assertRaisesRegex(CaptureLimitReached, "size limit"):
                writer.append_exchange(exchange(payload_bytes=2000))
            runs = load_capture_tree(root / "captures", root)
            self.assertEqual(runs[0].segments[0].end["reason"], "size_limit")
            self.assertEqual(runs[0].segments[0].exchanges, ())

    def test_disk_reserve_blocks_before_run_directory_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(CaptureLimitReached, "disk reserve"):
                self.make_writer(root, disk_free_bytes=lambda _: 0)
            captures = root / "captures"
            self.assertEqual(list(captures.iterdir()), [])

    def test_transport_loss_keeps_completed_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = self.make_writer(root)
            writer.append_exchange(exchange())
            writer.finish("transport_lost")
            run = load_capture_tree(root / "captures", root)[0]
            self.assertEqual(run.segments[0].end["reason"], "transport_lost")
            self.assertEqual(len(run.segments[0].exchanges), 1)

    def test_recovery_copies_valid_prefix_and_leaves_original_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = self.make_writer(root)
            writer.append_exchange(exchange())
            incomplete = writer.crash()
            source = incomplete.read_bytes() + b'{"record":"exchange"'
            incomplete.write_bytes(source)

            recovered = recover_incomplete_segment(incomplete)
            self.assertEqual(incomplete.read_bytes(), source)
            self.assertTrue(recovered.is_file())
            run = load_capture_tree(root / "captures", root)[0]
            self.assertEqual(run.segments[0].end["reason"], "recovered_prefix")
            self.assertEqual(len(run.segments[0].exchanges), 1)
            self.assertEqual(
                run.segments[0].start["recovered_from_sha256"],
                hashlib.sha256(source).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
