from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from tests.project_fixture import make_current_project
from toyota_telemetry.admission import compile_plan, validate_objective_shape
from toyota_telemetry.capture_store import inspect_capture_tree
from toyota_telemetry.capture_writer import NativeRunWriter, recover_incomplete_segment
from toyota_telemetry.collector import load_verified_plan
from toyota_telemetry.exporting import export_project
from toyota_telemetry.objectives import ReplayObservation, ReplayRun, replay_objectives
from toyota_telemetry.repository import ProjectRepository


def objective(**changes: object) -> dict:
    row = {
        "signal_ref": "372/0x1002/0..15",
        "objective_id": "all-counters",
        "class": "fast_dynamic",
        "included": True,
        "priority": 50,
        "target_interval_ms": 1000,
        "maximum_interval_ms": 1000,
        "deadband": 1.0,
        "targets": {
            "minimum_total_observations": 4,
            "minimum_changed_observations": 1,
            "minimum_distinct_raw_values": 3,
            "maximum_distinct_raw_values": 3,
            "minimum_transitions": 1,
            "required_raw_states": [1, 3],
            "minimum_applicable_seconds": 2,
            "minimum_time_span_seconds": 10,
            "minimum_distinct_runs": 1,
        },
        "applicability": None,
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
        "class_defaults": {},
        "acquisitions": [{
            "acquisition_id": f"{endpoint_id}/0x1002",
            "included": True,
            "priority": 50,
            "target_interval_ms": 1000,
            "maximum_interval_ms": 2000,
        }],
        "objectives": [objective(
            objective_id="reviewed-target",
            **{
                "class": "exploration",
                "target_interval_ms": 1000,
                "maximum_interval_ms": 2000,
                "targets": {"minimum_total_observations": minimum_observations},
            },
        )],
    }
    repository.write_authored_document("collection_policy", policy)
    repository.rebuild_cache(catalog_rows, repository.paths.captures)
    return ProjectRepository(repository.root)


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


class ObjectiveReplayTests(unittest.TestCase):
    def test_every_objective_class_accepts_and_credits_its_required_shape(self) -> None:
        cases = {
            "descriptive": {
                "minimum_total_observations": 2,
                "minimum_distinct_runs": 1,
                "maximum_distinct_raw_values": 1,
            },
            "snapshot": {
                "minimum_total_observations": 2,
                "minimum_distinct_runs": 1,
            },
            "switch_or_enum": {"required_raw_states": [7]},
            "fast_dynamic": {
                "minimum_changed_observations": 1,
                "minimum_applicable_seconds": 1,
            },
            "slow_trend": {
                "minimum_total_observations": 2,
                "minimum_time_span_seconds": 1,
            },
            "counter_or_history": {
                "minimum_total_observations": 2,
                "minimum_distinct_runs": 1,
            },
            "exploration": {"minimum_total_observations": 2},
        }
        for class_name, targets in cases.items():
            with self.subTest(class_name=class_name):
                row = objective(
                    **{
                        "class": class_name,
                        "targets": targets,
                        "deadband": 1.0,
                    }
                )
                validate_objective_shape(row)
                changed = class_name == "fast_dynamic"
                observations = [
                    ReplayObservation(
                        "run-a", 1, 0, None, "372/0x1002/0..15", 7, 7.0
                    ),
                    ReplayObservation(
                        "run-a", 2, 1000, None, "372/0x1002/0..15",
                        9 if changed else 7,
                        9.0 if changed else 7.0,
                    ),
                ]
                result = replay_objectives(
                    [row], [ReplayRun("run-a", "reviewed_independent")], observations
                )[0]
                self.assertTrue(result["complete"])

    def test_every_counter_and_segment_run_boundaries(self) -> None:
        runs = [ReplayRun("run-a", "reviewed_independent"), ReplayRun("run-b", "unknown")]
        observations = [
            ReplayObservation("run-a", 1, 0, "2026-01-01T00:00:00+00:00", "372/0x1002/0..15", 1, 1.0),
            ReplayObservation("run-a", 2, 1000, "2026-01-01T00:00:01+00:00", "372/0x1002/0..15", 1, 1.0),
            ReplayObservation("run-a", 3, 2000, "2026-01-01T00:00:02+00:00", "372/0x1002/0..15", 3, 3.0, 2),
            ReplayObservation("run-b", 1, 0, "2026-01-01T00:00:10+00:00", "372/0x1002/0..15", 2, 2.0),
        ]
        result = replay_objectives([objective()], runs, observations)[0]
        counters = result["counters"]
        self.assertEqual(counters["total_observations"], 4)
        self.assertEqual(counters["changed_observations"], 1)
        self.assertEqual(counters["distinct_raw_values"], 3)
        self.assertEqual(counters["transitions"], 1)
        self.assertEqual(counters["raw_states"], [1, 2, 3])
        self.assertEqual(counters["applicable_seconds"], 2)
        self.assertEqual(counters["time_span_seconds"], 10)
        self.assertEqual(counters["distinct_reviewed_runs"], 1)
        self.assertEqual(counters["intervals"]["within_target"], 2)
        self.assertTrue(result["complete"])

    def test_noise_inside_deadband_does_not_earn_change_credit(self) -> None:
        observations = [
            ReplayObservation("run-a", 1, 0, None, "372/0x1002/0..15", 10, 10.0),
            ReplayObservation("run-a", 2, 1000, None, "372/0x1002/0..15", 11, 10.5),
        ]
        result = replay_objectives(
            [objective(targets={"minimum_changed_observations": 1, "minimum_applicable_seconds": 1})],
            [ReplayRun("run-a", "unknown")],
            observations,
        )[0]
        self.assertEqual(result["counters"]["changed_observations"], 0)
        self.assertFalse(result["complete"])

    def test_applicability_true_false_and_unknown(self) -> None:
        gated = objective(
            objective_id="gated",
            **{
                "class": "exploration",
                "targets": {"minimum_total_observations": 1},
                "applicability": {
                    "all": [{
                        "signal_ref": "372/0x1003/0..7",
                        "operator": "eq",
                        "value": 1,
                        "maximum_age_ms": 100,
                    }]
                },
            },
        )
        observations = [
            ReplayObservation("run-a", 1, 0, None, "372/0x1003/0..7", 1, 1.0),
            ReplayObservation("run-a", 1, 0, None, "372/0x1002/0..15", 10, 10.0),
            ReplayObservation("run-a", 2, 200, None, "372/0x1002/0..15", 11, 11.0),
            ReplayObservation("run-a", 3, 300, None, "372/0x1003/0..7", 0, 0.0),
            ReplayObservation("run-a", 3, 300, None, "372/0x1002/0..15", 12, 12.0),
        ]
        result = replay_objectives([gated], [ReplayRun("run-a", "unknown")], observations)[0]
        self.assertEqual(result["applicability_counts"], {"true": 1, "false": 1, "unknown": 1})
        self.assertEqual(result["counters"]["total_observations"], 1)
        self.assertTrue(result["complete"])

    def test_flat_slow_signal_earns_count_and_span(self) -> None:
        slow = objective(
            objective_id="slow",
            **{
                "class": "slow_trend",
                "targets": {"minimum_total_observations": 2, "minimum_time_span_seconds": 5},
            },
        )
        observations = [
            ReplayObservation("run-a", 1, 0, "2026-01-01T00:00:00+00:00", "372/0x1002/0..15", 7, 7.0),
            ReplayObservation("run-a", 2, 5000, "2026-01-01T00:00:05+00:00", "372/0x1002/0..15", 7, 7.0),
        ]
        result = replay_objectives([slow], [ReplayRun("run-a", "unknown")], observations)[0]
        self.assertEqual(result["counters"]["changed_observations"], 0)
        self.assertTrue(result["complete"])


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

    def test_plan_rejects_missing_deadband_disk_and_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = current_repository(root)
            repository = author_collection_policy(repository, rows, minimum_observations=2)
            with self.assertRaisesRegex(ValueError, "insufficient disk"):
                compile_plan(repository, available_disk_mib=1, created_utc="x")
            row = repository.policy["objectives"][0]
            row["class"] = "fast_dynamic"
            row["targets"] = {
                "minimum_changed_observations": 1,
                "minimum_applicable_seconds": 1,
            }
            row.pop("deadband", None)
            with self.assertRaisesRegex(ValueError, "reviewed deadband"):
                compile_plan(repository, available_disk_mib=1024, created_utc="x")
            row["class"] = "exploration"
            row["targets"] = {"minimum_total_observations": 2}
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
            with self.assertRaisesRegex(ValueError, "no included objectives"):
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
            first = export_project(
                repository,
                toolkit_version="0.4.0-dev",
                toolkit_commit="deadbeef",
                generated_at_utc="2026-01-01T00:00:00+00:00",
            )
            before = tree_hashes(repository.paths.result)
            second = export_project(
                repository,
                toolkit_version="0.4.0-dev",
                toolkit_commit="deadbeef",
                generated_at_utc="2026-01-01T00:00:00+00:00",
            )
            self.assertEqual(first, second)
            self.assertEqual(before, tree_hashes(repository.paths.result))
            self.assertEqual(first["row_counts"]["signals"], 1)
            self.assertIn("No raw response payloads", first["privacy"])
            generation = repository.paths.result / "generations" / first["generation_id"]
            self.assertTrue((generation / "signals.csv").is_file())
            self.assertTrue((generation / "signals.json").is_file())


if __name__ == "__main__":
    unittest.main()
