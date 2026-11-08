"""Older ("phase 4") ECUs: one-byte local identifiers read with 21 LL -> 61 LL ..."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from tests.operator_fixture import ScriptedProbes, make_operator_project
from toyota_telemetry.identity import (
    is_local_identifier, read_request_hex, response_header_bytes, response_prefix_hex, within_read_range,
)
from toyota_telemetry.operator import sources
from toyota_telemetry.operator.service import OperatorService
from toyota_telemetry.operator.sources import NormalizedFileSource, TisDatabaseSource
from toyota_telemetry.repository import ProjectRepository
from toyota_telemetry.worker_protocol import build_worker_config

RECIPE = "session-a1769d23a560"
TPMS = f"750-758-2a-{RECIPE}"
ROWS = [{
    "ecu_key": 203, "ecu_name": "Tire Pressure Monitor", "tester_can_id": "0x750", "response_can_id": "0x758",
    "target_address": "0x2A", "data_id": "0x0030", "first_bit": 0, "last_bit": 7,
    "name": "ID 1 Tire Inflation Pressure", "formula": "((raw * 172 / 1) + -4988) / (10 ** 2)", "unit": "kPa",
}]


class RequestShapeTests(unittest.TestCase):
    def test_local_identifiers_use_service_21(self) -> None:
        self.assertTrue(is_local_identifier(0x30))
        self.assertFalse(is_local_identifier(0x0100))
        self.assertEqual((read_request_hex(0x30), response_prefix_hex(0x30), response_header_bytes(0x30)),
                         ("21 30", "61 30", 2))
        self.assertEqual((read_request_hex(0x1075), response_prefix_hex(0x1075), response_header_bytes(0x1075)),
                         ("22 10 75", "62 10 75", 3))
        self.assertTrue(within_read_range(0x30, 0x0100, 0xEFFF))
        self.assertFalse(within_read_range(0xF000, 0x0100, 0xEFFF))


class Phase4SourceTests(unittest.TestCase):
    def test_phase4_items_become_local_identifier_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "NA"
            (database / "DB").mkdir(parents=True)
            (database / "DB" / "Tire_P4.ddb").write_bytes(b"")
            ok = "0x00000000"
            answers = {
                "--catalog": [{"Index": 0, "Ecu": "Tire Pressure Monitor", "File": "Tire_P4.ddb", "Key": 203}],
                # No phase-5 data list: the older (21 LL) one is read instead.
                "--monitors": [{"Key": 203, "Status": "0x00000005", "Count": 0, "Index": None, "Name": None}],
                "--p4-monitors": [
                    {"Key": 203, "Status": ok, "Count": 2, "Index": 0, "Name": "ID 1 Tire Inflation Pressure",
                     "Layout": "local identifier", "LocalId": "0x30", "LocalIdLength": 5,
                     "ReadRequest": "21 30", "FirstBit": 0, "LastBit": 7, "PhysicalGroup": "0x000C"},
                    {"Key": 203, "Status": ok, "Count": 2, "Index": 1, "Name": "Key Cycle",
                     "Layout": "unknown", "LocalId": "0xE1", "LocalIdLength": 1,
                     "ReadRequest": None, "FirstBit": 16, "LastBit": 31, "PhysicalGroup": "0x26AC"},
                ],
                "--protocol": [{"Key": 203, "Status": ok, "Count": 1, "Index": 0, "PhysicalRequestCanId": "0x750",
                                "ResponseCanIdCandidate": "0x758", "Addressing": "extended",
                                "TargetAddress": "0x2A"}],
                "--physical": [{"Key": 203, "PhysicalGroup": "0x000C", "Status": ok, "Count": 1, "Index": 0,
                                "CoefficientA": 172, "CoefficientB": 1, "CoefficientC": -4988,
                                "UnitId": "0x003A", "ValueType": "0x00", "SignalLsb": 2}],
                "--units": [{"Key": 203, "UnitId": "0x003A", "Status": ok, "Count": 1, "Unit": "kPa"}],
                "--states": [],
            }

            def helper(command, **_kwargs):
                mode = next(argument for argument in command if argument in answers)
                stdout = "".join(json.dumps(row) + "\n" for row in answers[mode])
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

            source = TisDatabaseSource(database)
            with mock.patch.object(TisDatabaseSource, "availability", return_value=None), \
                    mock.patch.object(sources.subprocess, "run", side_effect=helper):
                result = source.read()
        self.assertEqual([row["signal_ref"] for row in result.rows], ["203/0x0030/0..7"])
        row = result.rows[0]
        self.assertEqual((row["unit"], row["target_address"]), ("kPa", "0x2A"))
        self.assertEqual(row["formula"], "((raw * 172 / 1) + -4988) / (10 ** 2)")
        self.assertIn("phase-4 item layout not understood", [reason for _, reason in result.rejections])


class LocalIdentifierWorkflowTests(unittest.TestCase):
    def test_verify_plan_worker_and_decode_use_21_and_61(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            fixture_rows = make_operator_project(root)
            rows = root / "state" / "p4.json"
            rows.write_text(json.dumps(ROWS))
            service = OperatorService(root)
            for path in (fixture_rows, rows):
                service.apply_source(service.preview_source(NormalizedFileSource(path).read()))
            probes = ScriptedProbes()
            tree = service.tree()
            scope = service.verification_scope(tree, service.select(tree, ["750-758-2a"]))
            service.verify(scope, probe_worker_factory=probes)
            read = probes.configs[-1]["preflight_read"]
            self.assertEqual((read["request_hex"], read["expected_prefix_hex"]), ("21 30", "61 30"))
            service.confirm_endpoint(TPMS, [203], [])

            tree = service.tree()
            result = service.edit_targets(tree, ["203/0x0030/0..7"], requirements={"minimum_samples": 3})
            service.save(policy=result.policy)
            readiness = service.readiness(TPMS)
            self.assertTrue(readiness.ready, readiness.problems)
            repository = ProjectRepository(root)
            plan = json.loads((repository.paths.plans / "admitted-plan.json").read_text())
            self.assertEqual([row["request_hex"] for row in plan["acquisitions"]], ["21 30"])
            worker = build_worker_config(plan, TPMS, repository.local_settings["backend"])
            self.assertEqual(worker["allowed_transactions"][0]["expected_prefix_hex"], "61 30")

            class Worker:
                def __init__(self, _executable, config_path):
                    self.config = json.loads(Path(config_path).read_text())

                def start(self):
                    return {"event": "ready", "schema_version": 1, "endpoint_id": self.config["endpoint_id"],
                            "backend_kind": "synthetic"}

                def exchange(self, sequence, transaction_id, event_sink):
                    # 61 30, then five tire bytes: tire 1 reads 0xA8 = 168.
                    return {"event": "result", "schema_version": 1, "sequence": sequence, "outcome": "positive",
                            "response_hex": "61 30 A8 A9 AA AB AC", "nrc": None, "pending_count": 0,
                            "pending_duration_ms": 0, "latency_ms": 5, "detail": None}

                def request_stop(self):
                    pass

                def close(self):
                    pass

                def terminate(self):
                    pass

            with mock.patch("toyota_telemetry.collection_service.JsonlWorkerClient", Worker):
                reason = service.collect(readiness, note=None, event_sink=lambda _e: None,
                                         stop_event=threading.Event())
            self.assertEqual(reason, "plan_complete")
            series = service.series("203/0x0030/0..7")
            self.assertTrue(series)
            self.assertEqual({row["raw_hex"] for row in series}, {"A8"})
            self.assertAlmostEqual(series[0]["decoded_value"], 168 * 1.72 - 49.88, places=6)
            bits = ProjectRepository(root).response_data_bits()
            self.assertEqual(bits[(TPMS, 0x30)], 5 * 8)


if __name__ == "__main__":
    unittest.main()
