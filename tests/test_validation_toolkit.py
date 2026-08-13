from __future__ import annotations

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from toyota_telemetry.capture import (
    evaluate_formula,
    read_jsonl,
    summarize_candidate_capture,
    validate_capture_context,
    validate_candidate_capture,
)
from toyota_telemetry.execution import capture_arguments
from toyota_telemetry.planning import build_plan, text_has
from toyota_telemetry.progress import discover_progress, road_queue, summarize_progress


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY / "tests" / "fixtures"


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
            config_path = root / "validation-profile.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            plan = build_plan(root, config_path, False)
            self.assertEqual(plan["endpoint"]["ecu_key"], 812)
            self.assertEqual(plan["endpoint"]["bitrate"], 250000)
            self.assertEqual(plan["candidates"][0]["tier"], "fast")


class ProgressAndBackendTests(unittest.TestCase):
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
                root, backend, "qualification", ["0x1234"], "batch", root / "out.jsonl",
                100, None, 0, 16,
            )
            self.assertIn("0x1234", arguments)
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
p.add_argument('--output'); p.add_argument('--did',action='append'); a=p.parse_args()
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
            config_path.write_text(json.dumps(config), encoding="utf-8")
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
