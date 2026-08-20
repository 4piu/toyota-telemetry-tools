from __future__ import annotations

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from toyota_telemetry.capture import (
    evaluate_formula,
    read_jsonl,
    summarize_candidate_capture,
    validate_capture_context,
    validate_candidate_capture,
)
from toyota_telemetry.execution import (
    CaptureTail,
    capture_arguments,
    capture_end_reason,
    recover_manual_experiment_captures,
    run_and_validate,
    run_backend,
)
from toyota_telemetry.experiments import (
    discover_completed_experiment_runs,
    experiment_snapshot,
    load_experiment,
)
from toyota_telemetry.planning import build_plan, ecu_views, text_has
from toyota_telemetry.progress import discover_progress, road_queue, summarize_progress


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY / "tests" / "fixtures"


def schema2_profile(config):
    """Convert a single-ECU profile literal to the multi-ECU schema.

    The suite was written when a profile described exactly one ECU. Keeping the
    fixtures in that shape and converting here keeps each test about the thing
    it is testing rather than about configuration plumbing.
    """
    endpoint = config.pop("endpoint")
    paths = config.pop(
        "paths", {"plan": "scratch/plan.json", "capture_root": "scratch/captures"}
    )
    plan_id = config.pop("plan_id", "test-plan")
    config["schema_version"] = 2
    config["transport"] = {
        "bitrate": endpoint.get("bitrate", 500000),
        "addressing": endpoint.get("addressing", "normal"),
    }
    config["ecus"] = [
        {
            "key": endpoint["ecu_key"],
            "name": endpoint.get("name", "Test ECU"),
            "plan_id": plan_id,
            "tester_can_id": endpoint["tester_can_id"],
            "response_can_id": endpoint["response_can_id"],
            "paths": paths,
        }
    ]
    return config


class CaptureTests(unittest.TestCase):
    def test_qualification_and_batch_contract(self):
        qualification = read_jsonl(FIXTURES / "candidate-qualification.complete.jsonl")
        start, samples = validate_candidate_capture(qualification, 1)
        self.assertEqual(start["profile_id"], "example-platform")
        self.assertEqual(len(samples), 2)

        batch = read_jsonl(FIXTURES / "candidate-batch.complete.jsonl")
        start, samples = validate_candidate_capture(batch, 2)
        report = summarize_candidate_capture(start, samples, None, 1)
        self.assertEqual(report["transport_retries"]["total"], 1)

        validate_capture_context(
            start,
            "example-platform",
            {
                "bitrate": 500000,
                "tester_can_id": "0x700",
                "response_can_id": "0x708",
            },
        )
        with self.assertRaises(ValueError):
            validate_capture_context(
                start,
                "wrong-platform",
                {
                    "bitrate": 500000,
                    "tester_can_id": "0x700",
                    "response_can_id": "0x708",
                },
            )

    def test_user_stop_requires_explicit_capture_policy(self):
        records = read_jsonl(FIXTURES / "candidate-batch.complete.jsonl")
        records[-1]["reason"] = "user_stop"
        with self.assertRaisesRegex(ValueError, "user_stop"):
            validate_candidate_capture(records, 2)
        start, samples = validate_candidate_capture(
            records, 2, accepted_end_reasons=frozenset({"user_stop"})
        )
        self.assertEqual(start["mode"], "candidate_batch")
        self.assertEqual(len(samples), 2)

    def test_formula_evaluator_is_bounded(self):
        self.assertEqual(evaluate_formula("signed(raw, 16) / 10", 0xFF9B), -10.1)
        with self.assertRaises(ValueError):
            evaluate_formula("__import__('os').getcwd()", 0)


class PlanningTests(unittest.TestCase):
    def test_word_matching_uses_boundaries(self):
        self.assertFalse(text_has("Temperature", ["rate"]))
        self.assertTrue(text_has("Vehicle Speed", ["speed"]))

    def test_plan_is_driven_by_profile_not_hardcoded_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "telemetry-project.toml").write_text(
                'schema_version = 1\nproject_kind = "vehicle"\nproject_id = "test"\n',
                encoding="utf-8",
            )
            catalog = root / "catalog.csv"
            with catalog.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "profile_id", "ecu_key", "acquisition", "data_id", "name",
                        "last_bit", "live_state",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "profile_id": "other-platform",
                        "ecu_key": "812",
                        "acquisition": "direct DID",
                        "data_id": "0x1234",
                        "name": "Motor Speed",
                        "last_bit": "15",
                        "live_state": "Not tested",
                    }
                )
            config = {
                "schema_version": 1,
                "plan_id": "other-plan",
                "profile_id": "other-platform",
                "endpoint": {
                    "ecu_key": 812,
                    "tester_can_id": "0x701",
                    "response_can_id": "0x709",
                    "bitrate": 250000,
                },
                "catalog": {"path": "catalog.csv", "acquisition": "direct DID"},
                "paths": {"plan": "scratch/plan.json", "capture_root": "scratch/captures"},
                "selection": {
                    "did_min": "0x1000",
                    "did_max": "0x1FFF",
                    "max_response_bytes": 32,
                    "maximum_dids_per_road_batch": 4,
                    "default_tier": "normal",
                    "tier_words": {"fast": ["speed"]},
                },
                "policies": {
                    "fast": {"period_ms": 100, "duration_seconds": 10, "minimum_samples_per_did": 5},
                    "normal": {"period_ms": 250, "duration_seconds": 10, "minimum_samples_per_did": 5},
                },
            }
            config = schema2_profile(config)
            config_path = root / "validation-profile.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            plan = build_plan(root, ecu_views(config)[0], False)
            self.assertEqual(plan["endpoint"]["ecu_key"], 812)
            self.assertEqual(plan["endpoint"]["bitrate"], 250000)
            self.assertEqual(plan["candidates"][0]["tier"], "fast")


class ExperimentTests(unittest.TestCase):
    def test_toml_experiment_is_qualified_bounded_and_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.toml"
            path.write_text(
                """schema_version = 1
experiment_id = "crosscheck-v1"
profile_id = "example-platform"
[defaults]
period_ms = 100
duration_seconds = 30
minimum_samples_per_did = 100
transport_retries = 2
[[batches]]
batch_id = "related-values"
dids = ["0x1000", "0x1001"]
repetitions = 2
vehicle_state = "ordinary driving"
""",
                encoding="utf-8",
            )
            plan = {
                "limits": {"maximum_dids_per_road_batch": 2},
                "candidates": [{"data_id": "0x1000"}, {"data_id": "0x1001"}],
            }
            document, runs = load_experiment(
                path, "example-platform", plan, {"0x1000", "0x1001"}
            )
            self.assertEqual(document["experiment_id"], "crosscheck-v1")
            self.assertEqual(len(runs), 2)
            self.assertNotEqual(runs[0].backend_batch_id, runs[1].backend_batch_id)
            self.assertTrue(runs[0].backend_batch_id.startswith("experiment-"))
            snapshot = experiment_snapshot(document, runs, {runs[0].backend_batch_id}, [])
            self.assertEqual(snapshot["status"]["complete"], 1)
            self.assertEqual(snapshot["status"]["pending"], 1)

    def test_experiment_rejects_unqualified_did(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.toml"
            path.write_text(
                """schema_version = 1
experiment_id = "test-v1"
profile_id = "example-platform"
[defaults]
period_ms = 100
duration_seconds = 10
minimum_samples_per_did = 1
transport_retries = 0
[[batches]]
batch_id = "one"
dids = ["0x1000"]
""",
                encoding="utf-8",
            )
            plan = {
                "limits": {"maximum_dids_per_road_batch": 2},
                "candidates": [{"data_id": "0x1000"}],
            }
            with self.assertRaisesRegex(ValueError, "positive qualification"):
                load_experiment(path, "example-platform", plan, set())

    def test_experiment_allows_unbounded_manual_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.toml"
            path.write_text(
                """schema_version = 1
experiment_id = "manual-v1"
profile_id = "example-platform"
[defaults]
period_ms = 100
duration_seconds = 0
minimum_samples_per_did = 100
transport_retries = 1
[[batches]]
batch_id = "operator-sequence"
dids = ["0x1000"]
""",
                encoding="utf-8",
            )
            plan = {
                "limits": {"maximum_dids_per_road_batch": 2},
                "candidates": [{"data_id": "0x1000"}],
            }
            _, runs = load_experiment(path, "example-platform", plan, {"0x1000"})
            self.assertEqual(runs[0].duration_seconds, 0)

    def test_custom_capture_is_separate_from_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "experiment-test-v1-one-r01-0123456789"
            capture = root / "one.complete.jsonl"
            records = [
                {
                    "record": "session_start",
                    "profile_id": "example-platform",
                    "mode": "candidate_batch",
                    "batch_id": batch_id,
                },
                {"record": "candidate_sample", "did": "0x1000", "supported": True},
                {"record": "session_end", "reason": "completed"},
            ]
            capture.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            progress = discover_progress(root, "example-platform")
            self.assertNotIn("0x1000", progress.road_complete)
            complete, corrupt = discover_completed_experiment_runs(
                root, "example-platform", {batch_id}
            )
            self.assertEqual(complete, {batch_id})
            self.assertEqual(corrupt, [])

            records[-1]["reason"] = "user_stop"
            capture.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            complete, corrupt = discover_completed_experiment_runs(
                root, "example-platform", {batch_id}
            )
            self.assertEqual(complete, {batch_id})
            self.assertEqual(corrupt, [])

    def test_valid_quarantined_manual_experiment_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            experiment = root / "experiment.toml"
            experiment.write_text(
                """schema_version = 1
experiment_id = "manual-v1"
profile_id = "example-platform"
[defaults]
period_ms = 100
duration_seconds = 30
minimum_samples_per_did = 1
transport_retries = 1
[[batches]]
batch_id = "sequence"
dids = ["0x1000"]
""",
                encoding="utf-8",
            )
            plan = {
                "limits": {"maximum_dids_per_road_batch": 2},
                "candidates": [{"data_id": "0x1000"}],
            }
            _, runs = load_experiment(experiment, "example-platform", plan, {"0x1000"})
            records = [
                {
                    "record": "session_start",
                    "schema_version": 1,
                    "profile_id": "example-platform",
                    "mode": "candidate_batch",
                    "batch_id": runs[0].backend_batch_id,
                    "dids": ["0x1000"],
                    "transport": {
                        "bitrate": 500000,
                        "tester_id": "0x700",
                        "response_id": "0x708",
                    },
                },
                {
                    "record": "candidate_sample",
                    "sequence": 1,
                    "did": "0x1000",
                    "request_elapsed_ms": 0,
                    "response_elapsed_ms": 1,
                    "latency_ms": 1,
                    "response_hex": "62 10 00 2A",
                    "supported": True,
                    "qualification_status": "supported",
                    "negative_response_code": None,
                },
                {
                    "record": "session_end",
                    "reason": "user_stop",
                    "sample_count": 1,
                    "did_counts": {"0x1000": 1},
                },
            ]
            source = root / "sequence.incomplete.jsonl"
            source.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            recovered = recover_manual_experiment_captures(
                root,
                runs,
                2,
                None,
                1,
                None,
                "example-platform",
                {
                    "bitrate": 500000,
                    "tester_can_id": "0x700",
                    "response_can_id": "0x708",
                },
            )
            self.assertEqual(len(recovered), 1)
            self.assertTrue((root / "sequence.complete.jsonl").is_file())
            self.assertTrue((root / "sequence.complete.summary.json").is_file())


class ProgressAndBackendTests(unittest.TestCase):
    def test_user_stop_reason_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.jsonl"
            capture.write_text(
                '{"record":"session_start"}\n'
                '{"record":"session_end","reason":"user_stop"}\n',
                encoding="utf-8",
            )
            self.assertEqual(capture_end_reason(capture), "user_stop")

    def test_custom_run_promotes_valid_manual_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "manual.partial.jsonl"
            complete = root / "manual.complete.jsonl"
            records = [
                {
                    "record": "session_start",
                    "schema_version": 1,
                    "profile_id": "example-platform",
                    "mode": "candidate_batch",
                    "batch_id": "experiment-manual-v1-sequence-r01-0123456789",
                    "dids": ["0x1000"],
                    "transport": {
                        "bitrate": 500000,
                        "tester_id": "0x700",
                        "response_id": "0x708",
                    },
                },
                {
                    "record": "candidate_sample",
                    "sequence": 1,
                    "did": "0x1000",
                    "request_elapsed_ms": 0,
                    "response_elapsed_ms": 1,
                    "latency_ms": 1,
                    "response_hex": "62 10 00 2A",
                    "supported": True,
                    "qualification_status": "supported",
                    "negative_response_code": None,
                },
                {
                    "record": "session_end",
                    "reason": "user_stop",
                    "sample_count": 1,
                    "did_counts": {"0x1000": 1},
                },
            ]
            partial.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with mock.patch(
                "toyota_telemetry.execution.run_backend", side_effect=KeyboardInterrupt()
            ):
                reason = run_and_validate(
                    ["backend"],
                    root,
                    partial,
                    complete,
                    1,
                    2,
                    None,
                    1,
                    None,
                    "example-platform",
                    {
                        "bitrate": 500000,
                        "tester_can_id": "0x700",
                        "response_can_id": "0x708",
                    },
                    True,
                )
            self.assertEqual(reason, "user_stop")
            self.assertTrue(complete.is_file())
            self.assertEqual(
                json.loads(complete.with_name("manual.complete.summary.json").read_text())[
                    "completion_reason"
                ],
                "user_stop",
            )

    def test_backend_ticks_while_the_capture_runs(self):
        """A road batch blocks for minutes. Without this the console shows
        nothing at all for the whole of it, which is what the operator reads
        as a hang."""
        process = mock.Mock()
        process.wait.side_effect = [
            subprocess.TimeoutExpired("backend", 1),
            subprocess.TimeoutExpired("backend", 1),
            0,
        ]
        ticks = []
        with mock.patch("toyota_telemetry.execution.subprocess.Popen", return_value=process):
            returncode = run_backend(["backend"], Path("."), tick=lambda: ticks.append(1))
        self.assertEqual(returncode, 0)
        self.assertEqual(len(ticks), 2)
        self.assertEqual(process.wait.call_args_list[0], mock.call(timeout=1.0))

    def test_capture_tail_counts_only_what_was_appended(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.partial.jsonl"
            tail = CaptureTail(path)
            self.assertEqual(tail.note(), "")
            path.write_text("a\nb\n", encoding="utf-8")
            self.assertEqual(tail.note(), "2 record(s)")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("c\n")
            self.assertEqual(tail.note(), "3 record(s)")
            self.assertEqual(tail.offset, path.stat().st_size)

    def test_backend_gets_grace_period_after_interrupt(self):
        process = mock.Mock()
        process.wait.side_effect = [KeyboardInterrupt(), 2]
        with mock.patch("toyota_telemetry.execution.subprocess.Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                run_backend(["backend"], Path("."))
        self.assertEqual(process.wait.call_args_list[1], mock.call(timeout=5))
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_backend_is_terminated_if_interrupt_cleanup_times_out(self):
        process = mock.Mock()
        process.wait.side_effect = [
            KeyboardInterrupt(),
            subprocess.TimeoutExpired("backend", 5),
            2,
        ]
        process.poll.return_value = None
        with mock.patch("toyota_telemetry.execution.subprocess.Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                run_backend(["backend"], Path("."))
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()

    def test_console_entry_treats_backend_user_stop_as_clean_interrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "telemetry-project.toml").write_text(
                'schema_version = 1\nproject_kind = "vehicle"\nproject_id = "test"\n',
                encoding="utf-8",
            )
            (root / "catalog.csv").write_text(
                "profile_id,ecu_key,acquisition,data_id,name,last_bit,live_state\n"
                "example-platform,1,direct DID,0x1000,Example,7,Not tested\n",
                encoding="utf-8",
            )
            endpoint = {
                "ecu_key": 1,
                "tester_can_id": "0x700",
                "response_can_id": "0x708",
                "bitrate": 500000,
            }
            plan = {
                "schema_version": 1,
                "plan_id": "test-plan",
                "profile_id": "example-platform",
                "endpoint": endpoint,
                "limits": {"maximum_dids_per_road_batch": 2},
                "policies": {
                    "fast": {
                        "period_ms": 100,
                        "duration_seconds": 10,
                        "minimum_samples_per_did": 1,
                    }
                },
                "candidates": [{"data_id": "0x1000", "tier": "fast", "signal_count": 1}],
            }
            plan_path = root / "scratch" / "plan.json"
            plan_path.parent.mkdir()
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            backend_script = root / "backend.py"
            backend_script.write_text(
                """import argparse, json, sys
p=argparse.ArgumentParser(); p.add_argument('--qualify',action='store_true')
p.add_argument('--live',action='store_true'); p.add_argument('--batch-id')
p.add_argument('--period-ms'); p.add_argument('--max-log-mib')
p.add_argument('--output'); p.add_argument('--ecu')
p.add_argument('--did',action='append'); a=p.parse_args()
records=[
 {'record':'session_start','schema_version':1,'profile_id':'example-platform'},
 {'record':'session_end','reason':'user_stop','sample_count':0,'did_counts':{}}]
with open(a.output,'x',encoding='utf-8') as h:
  for record in records: h.write(json.dumps(record)+'\\n'); h.flush()
sys.exit(2)
""",
                encoding="utf-8",
            )
            config = {
                "schema_version": 1,
                "plan_id": "test-plan",
                "profile_id": "example-platform",
                "endpoint": endpoint,
                "catalog": {"path": "catalog.csv", "acquisition": "direct DID"},
                "paths": {
                    "plan": "scratch/plan.json",
                    "capture_root": "scratch/captures",
                    "output_root": "scratch/captures/progressive",
                },
                "selection": {},
                "policies": plan["policies"],
                "backend": {
                    "schema_version": 1,
                    "executable": sys.executable,
                    "search_path": True,
                    "arguments": [str(backend_script)],
                    "mode_arguments": {"qualification": ["--qualify", "--live"]},
                },
            }
            config_path = root / "validation-profile.json"
            config_path.write_text(json.dumps(schema2_profile(config)), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "toyota_telemetry.execution",
                    "qualify",
                    "--project-root",
                    str(root),
                    "--config",
                    str(config_path),
                    "--live",
                    "--startup-delay-seconds",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 130, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("preserved as incomplete", result.stderr)
            self.assertEqual(len(list(root.rglob("*.incomplete.jsonl"))), 1)
            self.assertFalse(list(root.rglob("*.partial.jsonl")))

    def test_progress_is_reconstructed_per_did(self):
        progress = discover_progress(FIXTURES, "example-platform")
        self.assertIn("0x1000", progress.qualified)
        self.assertIn("0x1000", progress.road_complete)
        self.assertEqual(progress.negative_codes["0x1001"], ["0x31"])
        plan = {
            "plan_id": "test",
            "profile_id": "example-platform",
            "limits": {"maximum_dids_per_road_batch": 2},
            "policies": {"fast": {}},
            "candidates": [
                {"data_id": "0x1000", "tier": "fast"},
                {"data_id": "0x1001", "tier": "fast"},
            ],
        }
        self.assertEqual(road_queue(plan, progress, {"fast"}), [])
        summary = summarize_progress(plan, progress, {"fast"}, {"0x31"})
        self.assertEqual(summary["qualification"]["terminal_negative"], 1)

    def test_backend_contract_appends_only_structured_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend_file = root / "backend.exe"
            backend_file.touch()
            backend = {
                "schema_version": 1,
                "executable": "backend.exe",
                "arguments": ["--profile", "profile.json"],
                "mode_arguments": {"qualification": ["--qualify", "--live"]},
            }
            arguments = capture_arguments(
                root, backend, "qualification", 1, ["0x1234"], "batch",
                root / "out.jsonl", 100, None, 0, 16,
            )
            self.assertIn("0x1234", arguments)
            self.assertEqual(
                arguments[arguments.index("--ecu") + 1],
                "1",
                "the backend must be told which ECU it is talking to",
            )
            self.assertNotIn("--quiet", arguments)

    def test_live_runner_promotes_a_valid_backend_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "telemetry-project.toml").write_text(
                'schema_version = 1\nproject_kind = "vehicle"\nproject_id = "test"\n',
                encoding="utf-8",
            )
            catalog = root / "catalog.csv"
            catalog.write_text(
                "profile_id,ecu_key,acquisition,data_id,name,first_bit,last_bit,formula,unit,live_state\n"
                "example-platform,1,direct DID,0x1000,Example,0,7,raw,raw,Not tested\n",
                encoding="utf-8",
            )
            plan = {
                "schema_version": 1,
                "plan_id": "test-plan",
                "profile_id": "example-platform",
                "endpoint": {
                    "ecu_key": 1,
                    "tester_can_id": "0x700",
                    "response_can_id": "0x708",
                    "bitrate": 500000,
                },
                "limits": {"maximum_dids_per_road_batch": 2, "max_response_bytes": 32},
                "policies": {
                    "fast": {"period_ms": 100, "duration_seconds": 10, "minimum_samples_per_did": 1}
                },
                "candidates": [
                    {"data_id": "0x1000", "tier": "fast", "signal_count": 1}
                ],
                "excluded": [],
            }
            plan_path = root / "scratch" / "plan.json"
            plan_path.parent.mkdir()
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            backend_script = root / "backend.py"
            backend_script.write_text(
                """import argparse, json
p=argparse.ArgumentParser(); p.add_argument('--qualify',action='store_true')
p.add_argument('--live',action='store_true'); p.add_argument('--batch-id')
p.add_argument('--period-ms'); p.add_argument('--max-log-mib')
p.add_argument('--output'); p.add_argument('--ecu')
p.add_argument('--did',action='append'); a=p.parse_args()
did=a.did[0]
records=[
 {'record':'session_start','schema_version':1,'profile_id':'example-platform',
  'mode':'candidate_qualification','batch_id':a.batch_id,'dids':[did],
  'transport':{'backend':'synthetic','bitrate':500000,'tester_id':'0x700','response_id':'0x708'}},
 {'record':'candidate_sample','sequence':1,'did':did,'request_elapsed_ms':1,
  'response_elapsed_ms':2,'latency_ms':1,'request_hex':'22 10 00',
  'response_hex':'62 10 00 2A','supported':True,'qualification_status':'supported',
  'negative_response_code':None},
 {'record':'session_end','reason':'completed','sample_count':1,'did_counts':{did:1}}]
with open(a.output,'x',encoding='utf-8') as h:
  for record in records: h.write(json.dumps(record)+'\\n'); h.flush()
""",
                encoding="utf-8",
            )
            config = {
                "schema_version": 1,
                "plan_id": "test-plan",
                "profile_id": "example-platform",
                "endpoint": plan["endpoint"],
                "catalog": {"path": "catalog.csv", "acquisition": "direct DID"},
                "paths": {
                    "plan": "scratch/plan.json",
                    "capture_root": "scratch/captures",
                    "output_root": "scratch/captures/progressive",
                },
                "selection": {},
                "policies": plan["policies"],
                "backend": {
                    "schema_version": 1,
                    "executable": sys.executable,
                    "search_path": True,
                    "arguments": [str(backend_script)],
                    "mode_arguments": {"qualification": ["--qualify", "--live"]},
                },
            }
            config_path = root / "validation-profile.json"
            config_path.write_text(json.dumps(schema2_profile(config)), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "toyota_telemetry.execution",
                    "qualify",
                    "--project-root",
                    str(root),
                    "--config",
                    str(config_path),
                    "--live",
                    "--startup-delay-seconds",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            complete = list(root.glob("scratch/captures/progressive/qualification/*.complete.jsonl"))
            self.assertEqual(len(complete), 1)
            self.assertFalse(list(root.rglob("*.partial.jsonl")))


if __name__ == "__main__":
    unittest.main()
