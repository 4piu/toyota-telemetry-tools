from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_legacy_migration import make_legacy_project
from toyota_telemetry.admission import compile_plan
from toyota_telemetry.capture_store import inspect_capture_tree
from toyota_telemetry.exporting import export_project
from toyota_telemetry.legacy_migration import convert_legacy_project
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


def migrated_repository(root: Path) -> tuple[ProjectRepository, list[dict]]:
    make_legacy_project(root)
    destination = root / "scratch/migration-v1"
    convert_legacy_project(root, destination, "deadbeef")
    rows = json.loads((destination / "result/catalog-rows.json").read_text())
    return ProjectRepository(destination), rows


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
            repository, rows = migrated_repository(root)
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
            inspection = inspect_capture_tree(repository.paths.captures, repository.root)
            self.assertEqual(inspection["runs"], 1)
            self.assertEqual(inspection["exchanges"], 1)
            self.assertNotIn("response_hex", json.dumps(inspection))

    def test_plan_rejects_missing_deadband_disk_and_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = migrated_repository(root)
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
            capture = make_legacy_project(root)
            capture.unlink()
            destination = root / "scratch/migration-v1"
            convert_legacy_project(root, destination, "deadbeef")
            repository = ProjectRepository(destination)
            rows = json.loads((destination / "result/catalog-rows.json").read_text())
            repository = author_collection_policy(repository, rows, minimum_observations=2)
            with self.assertRaisesRegex(ValueError, "missing latency/response-size"):
                compile_plan(repository, available_disk_mib=1024, created_utc="x")

    def test_empty_policy_cannot_authorize_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, _ = migrated_repository(root)
            with self.assertRaisesRegex(ValueError, "no included objectives"):
                compile_plan(repository, available_disk_mib=1024, created_utc="x")

    def test_policy_change_reuses_all_recorded_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, rows = migrated_repository(root)
            signal_ref = "372/0x1002/0..15"
            before = repository.signal_observations(signal_ref)
            self.assertEqual(len(before), 1)

            repository = author_collection_policy(repository, rows, minimum_observations=1)
            self.assertTrue(repository.replay_progress()[0]["complete"])
            self.assertEqual(repository.signal_observations(signal_ref), before)

            repository = author_collection_policy(repository, rows, minimum_observations=2)
            self.assertFalse(repository.replay_progress()[0]["complete"])
            self.assertEqual(repository.signal_observations(signal_ref), before)

    def test_export_is_reproducible_and_privacy_minimized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            repository, _ = migrated_repository(root)
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
