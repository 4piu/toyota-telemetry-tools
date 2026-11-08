"""ECUs behind a gateway: one shared request/response ID pair, told apart by an address byte."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.operator_fixture import ScriptedProbes, make_operator_project
from toyota_telemetry import cli
from toyota_telemetry.identity import address_suffix, canonical_target_address
from toyota_telemetry.operator.service import OperatorService
from toyota_telemetry.operator.sources import NormalizedFileSource
from toyota_telemetry.repository import ProjectRepository
from toyota_telemetry.schemas import validate_transport
from toyota_telemetry.worker_protocol import build_probe_config, build_worker_config

RECIPE = "session-a1769d23a560"
GATEWAY_ROWS = [
    {"ecu_key": 452, "ecu_name": "Tire Pressure Monitor", "tester_can_id": "0x750",
     "response_can_id": "0x758", "target_address": "2a", "data_id": "0x0140", "first_bit": 0,
     "last_bit": 7, "name": "Tire pressure FL", "formula": "raw", "unit": "kPa"},
    {"ecu_key": 449, "ecu_name": "Main Body", "tester_can_id": "0x750",
     "response_can_id": "0x758", "target_address": "0x40", "data_id": "0x0141", "first_bit": 0,
     "last_bit": 7, "name": "Door", "formula": "raw", "unit": ""},
]
TPMS = f"750-758-2a-{RECIPE}"


class AddressTests(unittest.TestCase):
    def test_canonical_target_address(self) -> None:
        self.assertEqual(canonical_target_address("2a"), "0x2A")
        self.assertEqual(canonical_target_address("0X2a"), "0x2A")
        self.assertEqual(canonical_target_address(0x40), "0x40")
        self.assertIsNone(canonical_target_address(None))
        for bad in ("0x12A", "zz", -1, 256, True, ""):
            with self.assertRaises(ValueError):
                canonical_target_address(bad)
        self.assertEqual(address_suffix("0x2A"), "-2a")
        self.assertEqual(address_suffix(None), "")

    def test_transport_schema_accepts_only_a_canonical_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_operator_project(root)
            transport = json.loads((root / "config/transport.json").read_text())
        endpoint = dict(transport["endpoints"][0], id=TPMS, tester_can_id="0x750",
                        response_can_id="0x758", ecu_keys=[452], target_address="0x2A")
        validate_transport({**transport, "endpoints": [endpoint]})
        for bad in ("2A", "0x2a", "0x12A", None):
            with self.assertRaisesRegex(ValueError, "target_address"):
                validate_transport({**transport, "endpoints": [dict(endpoint, target_address=bad)]})

    def test_probe_config_switches_to_extended_addressing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_operator_project(root)
            repository = ProjectRepository(root)
            backend = repository.local_settings["backend"]
            normal = build_probe_config(repository.transport, "x", "0x700", "0x708", RECIPE, backend)
            addressed = build_probe_config(
                repository.transport, "y", "0x750", "0x758", RECIPE, backend, target_address="0x2A"
            )
        self.assertEqual(normal["transport"]["addressing"], "normal")
        self.assertNotIn("target_address", normal["transport"])
        self.assertEqual(
            (addressed["transport"]["addressing"], addressed["transport"]["target_address"]),
            ("extended", "0x2A"),
        )


class GatewayWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name) / "vehicle"
        self.root.mkdir()
        fixture_rows = make_operator_project(self.root)
        rows = self.root / "state" / "gateway.json"
        rows.write_text(json.dumps(GATEWAY_ROWS))
        self.service = OperatorService(self.root)
        # Two sources side by side: the fixture's normal ECUs and the gateway modules.
        for path in (fixture_rows, rows):
            self.service.apply_source(self.service.preview_source(NormalizedFileSource(path).read()))
        self.probes = ScriptedProbes()

    def test_each_address_is_its_own_endpoint_through_verify_confirm_and_plan(self) -> None:
        tree = self.service.tree()
        keys = {row.key: row for row in tree.endpoints}
        self.assertIn("750-758-2a", keys)
        self.assertIn("750-758-40", keys)
        self.assertEqual(keys["750-758-2a"].target_address, "0x2A")
        self.assertEqual(keys["750-758-2a"].as_dict()["target_address"], "0x2A")

        scope = self.service.verification_scope(tree, self.service.select(tree, ["750-758-2a"]))
        self.assertEqual(scope.endpoints[0].as_dict()["target_address"], "0x2A")
        self.service.verify(scope, probe_worker_factory=self.probes)
        # The endpoint probe and the DID read both go out extended-addressed to 0x2A.
        self.assertEqual(len(self.probes.configs), 2)
        for config in self.probes.configs:
            self.assertEqual(config["transport"]["addressing"], "extended")
            self.assertEqual(config["transport"]["target_address"], "0x2A")
            self.assertEqual(config["transport"]["tester_can_id"], "0x750")
        self.assertEqual(self.probes.configs[0]["candidate_id"], TPMS)
        # The other module on the same IDs was not probed and is still unverified.
        self.assertEqual(
            {row.key: row.state for row in self.service.tree().endpoints}["750-758-40"], "not verified"
        )

        endpoint = self.service.confirm_endpoint(TPMS, [452], [])
        self.assertEqual(endpoint["target_address"], "0x2A")
        transport = ProjectRepository(self.root).transport
        validate_transport(transport)
        self.assertEqual(
            next(row for row in transport["endpoints"] if row["id"] == TPMS)["target_address"], "0x2A"
        )

        tree = self.service.tree()
        result = self.service.edit_targets(tree, ["452/0x0140/0..7"], requirements={"minimum_samples": 3})
        self.service.save(policy=result.policy)
        readiness = self.service.readiness(TPMS)
        self.assertTrue(readiness.ready, readiness.problems)
        repository = ProjectRepository(self.root)
        plan = json.loads((repository.paths.plans / "admitted-plan.json").read_text())
        session = next(row for row in plan["endpoint_sessions"] if row["endpoint_id"] == TPMS)
        self.assertEqual(session["target_address"], "0x2A")
        worker = build_worker_config(plan, TPMS, repository.local_settings["backend"])
        self.assertEqual(
            (worker["transport"]["addressing"], worker["transport"]["target_address"]), ("extended", "0x2A")
        )

    def test_cli_adds_and_lists_an_addressed_endpoint(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO("")):
            code = cli.main([
                "endpoint", "add", "0x750:0x758:" + RECIPE, "--ecu", "5000", "--address", "0x5F",
                "--project-root", str(self.root),
            ])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("address 0x5F", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli.main(["signals", "--project-root", str(self.root)])
        self.assertIn("750-758-5f", out.getvalue())
        self.assertIn("0x750/0x758 address 0x5F", out.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(cli.main([
                "endpoint", "add", "0x750:0x758:" + RECIPE, "--ecu", "5001", "--address", "0x1FF",
                "--project-root", str(self.root),
            ]), cli.EXIT_FAILED)


if __name__ == "__main__":
    unittest.main()
