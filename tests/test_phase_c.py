from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from tests.project_fixture import make_current_project
from toyota_telemetry.admission import compile_plan
from toyota_telemetry.capture_store import inspect_capture_tree
from toyota_telemetry.capture_writer import NativeRunWriter, recover_incomplete_segment
from toyota_telemetry.collector import load_verified_plan
from toyota_telemetry.exporting import export_project
from toyota_telemetry.objectives import ReplayObservation, replay_targets
from toyota_telemetry.repository import ProjectRepository
from toyota_telemetry.schemas import validate_target


SIGNAL = "372/0x1002/0..15"


def target(**changes: object) -> dict:
    row = {
        "signal_ref": SIGNAL,
        "included": True,
        "minimum_difference": 1.0,
        "requirements": {
            "minimum_samples": 4,
            "minimum_changes": 1,
            "minimum_transitions": 1,
            "required_states": [1, 3],
            "minimum_applicable_seconds": 2,
            "minimum_span_seconds": 10,
            "minimum_runs": 2,
        },
        "condition": None,
    }
    row.update(changes)
    return row


def current_repository(root: Path) -> tuple[ProjectRepository, list[dict]]:
    rows = make_current_project(root)
    repository = ProjectRepository(root)
    repository.rebuild_cache(rows, repository.paths.captures)
    return repository, rows


def author_collection_policy(
    repository: ProjectRepository,
    catalog_rows: list[dict],
    *,
    minimum_observations: int = 1,
) -> ProjectRepository:
    endpoint_id = repository.transport["endpoints"][0]["id"]
    policy = {
        **repository.policy,
        "acquisitions": [{
            "acquisition_id": f"{endpoint_id}/0x1002",
            "included": True,
            "priority": 50,
            "target_interval_ms": 1000,
            "maximum_interval_ms": 2000,
        }],
        "targets": [{
            "signal_ref": SIGNAL,
            "included": True,
            "requirements": {"minimum_samples": minimum_observations},
            "condition": None,
        }],
    }
    repository.write_authored_document("collection_policy", policy)
    repository.rebuild_cache(catalog_rows, repository.paths.captures)
    return ProjectRepository(repository.root)


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


class TargetReplayTests(unittest.TestCase):
    def test_target_schema_requires_requirements_and_paired_difference(self) -> None:
        validate_target(target(), "target")
        with self.assertRaisesRegex(ValueError, "at least one completion requirement"):
            validate_target(target(requirements={}), "target")
        without = target()
        without.pop("minimum_difference")
        with self.assertRaisesRegex(ValueError, "minimum_difference is required"):
            validate_target(without, "target")
        with self.assertRaisesRegex(ValueError, "applies only to minimum_changes"):
            validate_target(target(requirements={"minimum_samples": 1}), "target")
        with self.assertRaisesRegex(ValueError, "unknown fields: class"):
            validate_target({**target(), "class": "exploration"}, "target")

    def test_every_counter_and_run_boundaries(self) -> None:
        observations = [
            ReplayObservation("run-a", 1, 0, "2026-01-01T00:00:00+00:00", SIGNAL, 1, 1.0),
            ReplayObservation("run-a", 2, 1000, "2026-01-01T00:00:01+00:00", SIGNAL, 1, 1.0),
            ReplayObservation("run-a", 3, 2000, "2026-01-01T00:00:02+00:00", SIGNAL, 3, 3.0, 2),
            ReplayObservation("run-b", 1, 0, "2026-01-01T00:00:10+00:00", SIGNAL, 2, 2.0),
        ]
        result = replay_targets([target()], observations)[0]
        counters = result["counters"]
        self.assertEqual(counters["samples"], 4)
        self.assertEqual(counters["changes"], 1)
        self.assertEqual(counters["transitions"], 1)
        self.assertEqual(counters["states"], [1, 2, 3])
        self.assertEqual(counters["applicable_seconds"], 2)
        self.assertEqual(counters["span_seconds"], 10)
        self.assertEqual(counters["runs"], 2)
        self.assertTrue(all(result["requirements_met"].values()))
        self.assertTrue(result["complete"])

    def test_noise_inside_minimum_difference_does_not_earn_change_credit(self) -> None:
        observations = [
            ReplayObservation("run-a", 1, 0, None, SIGNAL, 10, 10.0),
            ReplayObservation("run-a", 2, 1000, None, SIGNAL, 11, 10.5),
        ]
        result = replay_targets(
            [target(requirements={"minimum_changes": 1})], observations
        )[0]
        self.assertEqual(result["counters"]["transitions"], 1)
        self.assertEqual(result["counters"]["changes"], 0)
        self.assertFalse(result["complete"])

    def test_condition_true_false_and_unknown(self) -> None:
        gated = target(
            requirements={"minimum_samples": 1},
            condition={
                "all": [{
                    "signal_ref": "372/0x1003/0..7",
                    "operator": "eq",
                    "value": 1,
                    "maximum_age_ms": 100,
                }]
            },
        )
        gated.pop("minimum_difference")
        observations = [
            ReplayObservation("run-a", 1, 0, None, "372/0x1003/0..7", 1, 1.0),
            ReplayObservation("run-a", 1, 0, None, SIGNAL, 10, 10.0),
            ReplayObservation("run-a", 2, 200, None, SIGNAL, 11, 11.0),
            ReplayObservation("run-a", 3, 300, None, "372/0x1003/0..7", 0, 0.0),
            ReplayObservation("run-a", 3, 300, None, SIGNAL, 12, 12.0),
        ]
        result = replay_targets([gated], observations)[0]
        self.assertEqual(result["condition_counts"], {"true": 1, "false": 1, "unknown": 1})
        self.assertEqual(result["counters"]["samples"], 1)
        self.assertTrue(result["complete"])

    def test_flat_slow_signal_earns_samples_and_span(self) -> None:
        slow = {
            "signal_ref": SIGNAL,
            "included": True,
            "requirements": {"minimum_samples": 2, "minimum_span_seconds": 5},
            "condition": None,
        }
        observations = [
            ReplayObservation("run-a", 1, 0, "2026-01-01T00:00:00+00:00", SIGNAL, 7, 7.0),
            ReplayObservation("run-a", 2, 5000, "2026-01-01T00:00:05+00:00", SIGNAL, 7, 7.0),
        ]
        result = replay_targets([slow], observations)[0]
        self.assertEqual(result["counters"]["changes"], 0)
        self.assertTrue(result["complete"])

    def test_excluded_target_is_never_complete(self) -> None:
        excluded = {
            "signal_ref": SIGNAL,
            "included": False,
            "requirements": {"minimum_samples": 1},
            "condition": None,
        }
        observation = ReplayObservation("run-a", 1, 0, None, SIGNAL, 7, 7.0)
        self.assertFalse(replay_targets([excluded], [observation])[0]["complete"])


class PlanningAndExportTests(unittest.TestCase):
    def test_offline_plan_is_admitted_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = current_repository(root)
            repository = author_collection_policy(repository, rows, minimum_observations=2)
            output = repository.paths.plans / "admitted-plan.json"
            plan, digest = compile_plan(
                repository,
                available_disk_mib=1024,
                created_utc="2026-01-01T00:00:00+00:00",
                output=output,
            )
            self.assertTrue(plan["admitted"])
            self.assertTrue(plan["live_authorized"])
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)
            self.assertNotIn("backend", plan)
            self.assertNotIn("--live", output.read_text())
            self.assertEqual(
                plan["decodes"],
                [{
                    "acquisition_id": f"{plan['endpoint_sessions'][0]['endpoint_id']}/0x1002",
                    "signal_ref": "372/0x1002/0..15",
                    "first_bit": 0,
                    "last_bit": 15,
                    "formula": "raw / 1000",
                }],
            )
            self.assertEqual(
                plan["endpoint_sessions"][0]["session_setup"][0]["expected_prefix_hex"],
                "50 03",
            )
            verified, verified_bytes = load_verified_plan(output, repository)
            self.assertEqual(verified, plan)
            self.assertEqual(verified_bytes, output.read_bytes())
            original_policy = repository.paths.collection_policy.read_bytes()
            repository.paths.collection_policy.write_bytes(original_policy + b" ")
            with self.assertRaisesRegex(ValueError, "stale: collection_policy"):
                load_verified_plan(output, repository)
            repository.paths.collection_policy.write_bytes(original_policy)
            inspection = inspect_capture_tree(repository.paths.captures, repository.root)
            self.assertEqual(inspection["runs"], 1)
            self.assertEqual(inspection["exchanges"], 1)
            self.assertNotIn("response_hex", json.dumps(inspection))

    def test_plan_rejects_missing_settings_disk_and_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = current_repository(root)
            repository = author_collection_policy(repository, rows, minimum_observations=2)
            with self.assertRaisesRegex(ValueError, "insufficient disk"):
                compile_plan(repository, available_disk_mib=1, created_utc="x")
            repository.policy["acquisitions"][0]["included"] = False
            with self.assertRaisesRegex(ValueError, "no included DID settings"):
                compile_plan(repository, available_disk_mib=1024, created_utc="x")
            repository.policy["acquisitions"][0]["included"] = True
            repository.transport["safety"]["maximum_request_rate_hz"] = 4
            with self.assertRaisesRegex(ValueError, "request-start floor"):
                compile_plan(repository, available_disk_mib=1024, created_utc="x")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            rows = make_current_project(root, include_capture=False)
            repository = ProjectRepository(root)
            repository.rebuild_cache(rows, repository.paths.captures)
            repository = author_collection_policy(repository, rows, minimum_observations=2)
            with self.assertRaisesRegex(ValueError, "missing latency/response-size"):
                compile_plan(repository, available_disk_mib=1024, created_utc="x")

    def test_empty_policy_cannot_authorize_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, _ = current_repository(root)
            with self.assertRaisesRegex(ValueError, "no included targets"):
                compile_plan(repository, available_disk_mib=1024, created_utc="x")

    def test_completed_policy_cannot_authorize_unnecessary_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = current_repository(root)
            repository = author_collection_policy(repository, rows, minimum_observations=1)
            with self.assertRaisesRegex(ValueError, "already complete"):
                compile_plan(repository, available_disk_mib=1024, created_utc="x")

    def test_policy_change_reuses_all_recorded_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = current_repository(root)
            signal_ref = "372/0x1002/0..15"
            before = repository.signal_observations(signal_ref)
            self.assertEqual(len(before), 1)

            repository = author_collection_policy(repository, rows, minimum_observations=1)
            self.assertTrue(repository.replay_progress()[0]["complete"])
            self.assertEqual(repository.signal_observations(signal_ref), before)

            repository = author_collection_policy(repository, rows, minimum_observations=2)
            self.assertFalse(repository.replay_progress()[0]["complete"])
            self.assertEqual(repository.signal_observations(signal_ref), before)

    def test_native_run_incremental_ingestion_matches_clean_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = current_repository(root)
            repository = author_collection_policy(repository, rows, minimum_observations=10)
            plan_path = repository.paths.plans / "admitted-plan.json"
            plan, _ = compile_plan(
                repository,
                available_disk_mib=4096,
                created_utc="2026-01-01T00:00:00Z",
                output=plan_path,
            )
            acquisition = plan["acquisitions"][0]
            native_exchange = {
                "acquisition_id": acquisition["acquisition_id"],
                "endpoint_id": acquisition["endpoint_id"],
                "data_id": acquisition["data_id"],
                "response_utc": "2026-01-01T00:00:00Z",
                "request_elapsed_ms": 0,
                "response_elapsed_ms": 10,
                "request_hex": acquisition["request_hex"],
                "response_hex": "62 10 02 00 01",
                "outcome": "positive",
                "negative_response_code": None,
                "pending_count": 0,
                "pending_duration_ms": 0,
                "latency_ms": 10,
                "retry_count": 0,
                "scheduler_reason": "target_due",
                "objective_ids": acquisition["objective_ids"],
                "observations": [{
                    "signal_ref": "372/0x1002/0..15",
                    "raw_hex": "00 01",
                    "decoded_value": 0.001,
                    "applicability": "true",
                }],
            }
            writer = NativeRunWriter(
                repository.paths.captures,
                plan,
                plan_path.read_bytes(),
                run_id=str(uuid.uuid4()),
                created_utc="2026-01-01T00:00:00Z",
                backend_kind="synthetic",
                disk_free_bytes=lambda _: 10**12,
            )
            writer.append_exchange(native_exchange)
            writer.segment_duration_ms = 1
            writer.rotate(1)
            later = {
                **native_exchange,
                "request_elapsed_ms": 1,
                "response_elapsed_ms": 11,
            }
            writer.append_exchange(later)
            incomplete = writer.crash()

            repository.ingest_captures(repository.paths.captures)
            recover_incomplete_segment(incomplete)
            repository.ingest_captures(repository.paths.captures)
            incremental = repository.cache_snapshot()
            repository.paths.database.unlink()
            repository.rebuild_cache(rows, repository.paths.captures)
            self.assertEqual(repository.cache_snapshot(), incremental)

    def test_export_is_reproducible_and_privacy_minimized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, _ = current_repository(root)
            deliverable = Path(directory) / "deliverable"
            first = export_project(
                repository,
                deliverable,
                toolkit_version="0.4.0-dev",
                toolkit_commit="deadbeef",
                generated_at_utc="2026-01-01T00:00:00+00:00",
            )
            before = tree_hashes(deliverable)
            second = export_project(
                repository,
                deliverable,
                toolkit_version="0.4.0-dev",
                toolkit_commit="deadbeef",
                generated_at_utc="2026-01-01T00:00:00+00:00",
            )
            self.assertEqual(first, second)
            self.assertEqual(before, tree_hashes(deliverable))
            self.assertEqual(first["row_counts"]["signals"], 1)
            self.assertIn("No raw response payloads", first["privacy"])
            generation = deliverable / "result" / "generations" / first["generation_id"]
            self.assertTrue((generation / "signals.csv").is_file())
            self.assertTrue((generation / "signals.json").is_file())


if __name__ == "__main__":
    unittest.main()
