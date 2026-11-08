from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from toyota_telemetry.operator import sources
from toyota_telemetry.operator.sources import TisDatabaseSource


OK = "0x00000000"
ANSWERS = {
    "--catalog": [
        {"Index": 0, "Ecu": "Hybrid Control", "File": "HV_P5.ddb", "Key": 397},
        {"Index": 1, "Ecu": "Not installed", "File": "XX_P5.ddb", "Key": 1},
    ],
    "--monitors": [
        {"Key": 397, "Status": OK, "Count": 2, "Index": 0, "Name": "Vehicle Speed", "Flags": "0x00000000",
         "InternalId": "0x0001", "Acquisition": "direct DID", "ReadRequest": "22 10 75", "FirstBit": 0,
         "LastBit": 15, "PhysicalGroup": "0x0020", "DataId": "0x1075", "DisplayKind": 2, "StateSet": 0},
        {"Key": 397, "Status": OK, "Count": 2, "Index": 2, "Name": "Shift Position", "Flags": "0x00000000",
         "InternalId": "0x0003", "Acquisition": "direct DID", "ReadRequest": "22 10 61", "FirstBit": 0,
         "LastBit": 7, "PhysicalGroup": "0x0021", "DataId": "0x1061", "DisplayKind": 1, "StateSet": 8},
        {"Key": 397, "Status": OK, "Count": 2, "Index": 1, "Name": "Mapped", "Flags": "0x00000010",
         "InternalId": "0x0002", "Acquisition": "mapped/dynamic", "ReadRequest": None, "FirstBit": 0,
         "LastBit": 7, "PhysicalGroup": "0x0021", "DataId": "0x1076"},
    ],
    "--protocol": [
        {"Key": 397, "Status": OK, "Count": 1, "Index": 0,
         "PhysicalRequestCanId": "0x7D2", "ResponseCanIdCandidate": "0x7DA"},
    ],
    "--physical": [
        {"Key": 397, "PhysicalGroup": "0x0020", "Status": OK, "Count": 1, "Index": 0, "CoefficientA": 1,
         "CoefficientB": 100, "CoefficientC": 0, "UnitId": "0x0005", "ValueType": "0x00", "SignalLsb": 0},
        {"Key": 397, "PhysicalGroup": "0x0021", "Status": "0x00000005", "Count": 0},
    ],
    "--units": [{"Key": 397, "UnitId": "0x0005", "Status": OK, "Count": 1, "Unit": "km/h"}],
    "--states": [
        {"Key": 397, "StateSet": 8, "Status": OK, "Count": 2, "Index": 0, "Value": 0, "Text": "P"},
        {"Key": 397, "StateSet": 8, "Status": OK, "Count": 2, "Index": 1, "Value": 2, "Text": "R"},
    ],
}


class TisSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "NA"
        (self.database / "DB").mkdir(parents=True)
        (self.database / "DB" / "HV_P5.ddb").write_bytes(b"")
        self.commands: list[list[str]] = []

    def helper(self, command, **_kwargs):
        self.commands.append(command)
        mode = next(argument for argument in command if argument in ANSWERS)
        stdout = "".join(json.dumps(row) + "\n" for row in ANSWERS[mode])
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def test_rows_come_from_the_bundled_helper(self) -> None:
        source = TisDatabaseSource(self.database)
        with mock.patch.object(TisDatabaseSource, "availability", return_value=None), \
                mock.patch.object(sources.subprocess, "run", side_effect=self.helper):
            result = source.read()
        self.assertEqual(self.commands[0][0], str(sources.WORKERS / "telemetry-tisdb.exe"))
        self.assertEqual(self.commands[0][1:5], ["--database", str(self.database), "--code-page", "932"])
        self.assertEqual([row["signal_ref"] for row in result.rows], ["397/0x1075/0..15", "397/0x1061/0..7"])
        # A state signal carries the text Techstream shows for each listed raw value.
        self.assertEqual(result.rows[1]["states"], {"0": "P", "2": "R"})
        self.assertNotIn("states", result.rows[0])
        self.assertIn(["--keys", "397", "--states", "8"], [command[-4:] for command in self.commands])
        row = result.rows[0]
        self.assertEqual((row["name"], row["unit"], row["ecu_name"]), ("Vehicle Speed", "km/h", "Hybrid Control"))
        self.assertEqual(row["formula"], "((raw * 1 / 100) + 0) / (10 ** 0)")
        self.assertEqual((row["tester_can_id"], row["response_can_id"]), ("0x7D2", "0x7DA"))
        self.assertEqual(len(result.rejections), 1)
        self.assertIn("not a direct DID", result.rejections[0][1])

    def test_extended_addressing_carries_the_target_address(self) -> None:
        answers = dict(ANSWERS)

        def helper(command, **_kwargs):
            mode = next(argument for argument in command if argument in answers)
            stdout = "".join(json.dumps(row) + "\n" for row in answers[mode])
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        source = TisDatabaseSource(self.database)
        for addressing, address, expected in (
            ("extended", "0x2A", "0x2A"), ("normal", None, None), ("other 0x14", None, "rejected"),
        ):
            answers["--protocol"] = [{
                "Key": 397, "Status": OK, "Count": 1, "Index": 0, "PhysicalRequestCanId": "0x750",
                "ResponseCanIdCandidate": "0x758", "Addressing": addressing, "TargetAddress": address,
            }]
            with mock.patch.object(TisDatabaseSource, "availability", return_value=None), \
                    mock.patch.object(sources.subprocess, "run", side_effect=helper):
                result = source.read()
            if expected == "rejected":
                # Ethernet diagnostics have no CAN address: nothing is imported for them.
                self.assertEqual(result.rows, ())
                self.assertTrue(any("not a CAN diagnostic address" in reason for _, reason in result.rejections))
            else:
                self.assertEqual([row.get("target_address") for row in result.rows], [expected, expected])
                self.assertEqual(result.rows[0]["tester_can_id"], "0x750")

    def test_the_installed_database_is_found_in_the_standard_location(self) -> None:
        program_files = Path(self.database).parent / "Program Files (x86)"
        techstream = program_files / "Toyota Diagnostics" / "Techstream"
        environment = {"ProgramFiles(x86)": str(program_files), "ProgramFiles": str(program_files)}
        with mock.patch.dict(os.environ, environment), mock.patch.object(sources, "FALLBACK_PROGRAM_FILES", ()):
            self.assertIsNone(sources.default_tis_database())
            (techstream / "EU" / "DB").mkdir(parents=True)
            self.assertEqual(sources.default_tis_database(), techstream / "EU")
            (techstream / "NA" / "DB").mkdir(parents=True)
            self.assertEqual(sources.default_tis_database(), techstream / "NA")

    def test_a_helper_failure_is_reported(self) -> None:
        source = TisDatabaseSource(self.database)
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="ERROR: Could not load KgpDataCtrl.dll")
        with mock.patch.object(TisDatabaseSource, "availability", return_value=None), \
                mock.patch.object(sources.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "Could not load KgpDataCtrl.dll"):
                source.read()

    @unittest.skipUnless(os.name == "nt", "the reader is Windows-only")
    def test_a_missing_helper_is_named(self) -> None:
        with mock.patch.object(sources, "WORKERS", self.database / "no-workers"):
            self.assertIn("telemetry-tisdb.exe is missing", TisDatabaseSource(self.database).availability())


if __name__ == "__main__":
    unittest.main()
