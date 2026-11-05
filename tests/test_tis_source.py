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
