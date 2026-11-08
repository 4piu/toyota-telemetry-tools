from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.operator_fixture import ENDPOINT_ID, UNCONFIRMED_KEY, ScriptedProbes, make_operator_project
from toyota_telemetry import cli
from toyota_telemetry.operator import OPERATION_IDS


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name) / "vehicle"
        self.root.mkdir()
        self.rows = make_operator_project(self.root)
        self.probes = ScriptedProbes()
        patcher = mock.patch(
            "toyota_telemetry.discovery.default_probe_worker_factory", lambda repository, root: self.probes
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def run_cli(self, *argv: str, stdin: str = "", tty: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = io.StringIO(stdin)
        fake_stdin.isatty = lambda: tty  # type: ignore[method-assign]
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", fake_stdin):
            code = cli.main([*argv, "--project-root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def json_cli(self, *argv: str) -> dict:
        code, out, err = self.run_cli(*argv, "--json")
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def test_every_operation_has_a_command(self) -> None:
        self.assertEqual(cli.cli_operations(), set(OPERATION_IDS))

    def test_status_and_signals_report_json(self) -> None:
        status = self.json_cli("status")
        self.assertEqual(status["next_action"]["page"], "setup")
        self.assertEqual(self.run_cli("source", "import", "file", str(self.rows), "--apply")[0], 0)
        listing = self.json_cli("signals", "--ecu", "405")
        self.assertEqual([row["key"] for row in listing["endpoints"]], [UNCONFIRMED_KEY])
        detail = self.json_cli("signals", "show", f"{ENDPOINT_ID}/0x1003")
        self.assertEqual(len(detail["signals"]), 2)

    def test_scripted_verify_needs_matching_preview(self) -> None:
        self.run_cli("source", "import", "file", str(self.rows), "--apply")
        code, _, err = self.run_cli("verify", UNCONFIRMED_KEY)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("transmits to the vehicle", err)
        code, _, err = self.run_cli("verify", UNCONFIRMED_KEY, "--yes")
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("no previewed verify scope", err)
        self.assertEqual(self.probes.transmissions, 0)

        self.assertEqual(self.run_cli("verify", UNCONFIRMED_KEY, "--dry-run")[0], 0)
        self.assertEqual(self.probes.transmissions, 0)
        code, _, err = self.run_cli("verify", f"{ENDPOINT_ID}/0x1003", "--yes")
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("changed since the last --dry-run", err)
        code, out, err = self.run_cli("verify", UNCONFIRMED_KEY, "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("(1/2 50%) endpoint 7a1-7a9: responded", out)
        self.assertEqual(self.probes.transmissions, 2)

    def test_interactive_verify_asks_and_can_be_declined(self) -> None:
        self.run_cli("source", "import", "file", str(self.rows), "--apply")
        code, _, err = self.run_cli("verify", UNCONFIRMED_KEY, stdin="no\n", tty=True)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("nothing was transmitted", err)
        self.assertEqual(self.probes.transmissions, 0)
        code, _, err = self.run_cli("verify", UNCONFIRMED_KEY, stdin="yes\n", tty=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.probes.transmissions, 2)

    def test_edit_commands_round_trip_and_dry_run_writes_nothing(self) -> None:
        self.run_cli("source", "import", "file", str(self.rows), "--apply")
        policy_path = self.root / "config/collection-policy.json"
        before = policy_path.read_bytes()
        code, out, _ = self.run_cli("target", "set", "372/0x1003/8..15", "--min-changes", "3",
                                    "--min-difference", "0.5", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(policy_path.read_bytes(), before)
        result = self.json_cli("target", "set", "372/0x1003/8..15", "--min-changes", "3",
                               "--min-difference", "0.5", "--min-span", "10m",
                               "--when", "372/0x1003/0..7 == 1")
        self.assertEqual(result["created_did_settings"], [f"{ENDPOINT_ID}/0x1003"])
        target = json.loads(policy_path.read_text())["targets"][0]
        self.assertEqual(target["requirements"], {"minimum_changes": 3, "minimum_span_seconds": 600.0})
        self.assertEqual(target["minimum_difference"], 0.5)
        self.assertEqual(target["condition"]["signal_ref"], "372/0x1003/0..7")
        siblings = self.json_cli("did", "set", "372/0x1003/0..7", "--interval", "250", "--max-interval", "500")
        self.assertEqual(siblings["unselected_siblings"], ["372/0x1003/8..15"])
        self.assertEqual(self.json_cli("target", "clear", "372/0x1003/8..15")["signals"], ["372/0x1003/8..15"])

    def test_hand_edited_json_is_checked(self) -> None:
        policy_path = self.root / "config/collection-policy.json"
        document = json.loads(policy_path.read_text())
        document["targets"] = [{"signal_ref": "372/0x1002/0..15", "included": True,
                                "condition": None, "requirements": {"minimum_samples": 0}}]
        policy_path.write_text(json.dumps(document, indent=2) + "\n")
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("minimum_samples must be at least 1", out)

    def test_collect_dry_run_reports_problems_with_fixes(self) -> None:
        code, out, _ = self.run_cli("collect", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("Not ready to collect", out)
        self.assertIn("fix: telemetry target set", out)

    def test_collect_with_every_target_met_has_nothing_to_do(self) -> None:
        self.run_cli("source", "import", "file", str(self.rows), "--apply")
        self.assertEqual(self.run_cli("target", "set", "372/0x1002/0..15", "--min-samples", "1")[0], 0)
        code, out, _ = self.run_cli("collect", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to collect: every included target is met.", out)
        self.assertNotIn("Not ready", out)
        result = self.json_cli("collect", "--dry-run")
        self.assertTrue(result["complete"])
        self.assertEqual(result["problems"], [])
        self.assertIn("Collect   complete, every target met", self.run_cli("status")[1])

    def test_connector_set_and_check_never_open_a_device(self) -> None:
        # The bundled workers may be built in a development checkout; use none here.
        patcher = mock.patch("toyota_telemetry.project.WORKERS", self.root / "no-workers")
        patcher.start()
        self.addCleanup(patcher.stop)
        code, out, _ = self.run_cli("connector", "set", "gsusb", "--vendor-id", "1D50", "--product-id", "606f")
        self.assertEqual(code, 0)
        self.assertIn("warning: worker telemetry-gsusb.exe is missing", out)
        shown = self.json_cli("connector", "show")
        self.assertEqual(shown["connector"]["backend"]["settings"], {"product_id": "606f", "vendor_id": "1d50"})
        code, _, err = self.run_cli("connector", "set", "pcan", "--channel", "USB9")
        self.assertEqual(code, 1)
        self.assertIn("PCAN_USBBUS1 through PCAN_USBBUS8", err)
        check = self.run_cli("connector", "check", "--no-self-test")
        self.assertEqual(check[0], 1)

    def test_recordings_list_and_export(self) -> None:
        runs = self.json_cli("recordings")["runs"]
        self.assertEqual(len(runs), 1)
        code, out, _ = self.run_cli("recordings", "export", "--signal", "372/0x1002/0..15")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)
        code, out, _ = self.run_cli("recordings", "export", "--signal", "372/0x1002/0..15",
                                    "--run", runs[0]["run_id"])
        self.assertEqual(len(out.strip().splitlines()), 2)
        code, _, err = self.run_cli("recordings", "show")
        self.assertEqual(code, 1)
        self.assertIn("usage: telemetry recordings show RUN", err)
        # Deleting moves the run folder to the system trash (a stand-in here).
        trashed = []
        with mock.patch("toyota_telemetry.system_trash.send_to_trash", trashed.extend):
            self.assertEqual(self.json_cli("recordings", "trash", runs[0]["run_id"])["trash"], runs[0]["run_id"])
        self.assertEqual(len(trashed), 1)
        self.assertEqual(trashed[0].parent.name, "captures")


if __name__ == "__main__":
    unittest.main()


class VersionTests(unittest.TestCase):
    def version(self, commit: str | None) -> str:
        out = io.StringIO()
        with mock.patch("toyota_telemetry.cli.toolkit_commit", return_value=commit), redirect_stderr(out), \
                redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--version"])
        return out.getvalue().strip()

    def test_version_names_the_commit_when_known(self) -> None:
        self.assertRegex(self.version("0123456789abcdef0123"), r"^telemetry \S+ \(0123456789ab\)$")
        self.assertRegex(self.version("0123456789abcdef0123-dirty"), r"\(0123456789ab-dirty\)$")
        self.assertNotIn("(", self.version(None))

    def test_installed_commit_reads_the_git_install_record(self) -> None:
        from toyota_telemetry.operator import service

        record = json.dumps({"url": "https://example.invalid/repo", "vcs_info": {"vcs": "git", "commit_id": "abc123"}})
        distribution = mock.Mock(read_text=lambda name: record if name == "direct_url.json" else None)
        with mock.patch.object(service.metadata, "distribution", return_value=distribution):
            self.assertEqual(service.installed_commit(), "abc123")
        local = mock.Mock(read_text=lambda name: json.dumps({"url": "file:///x", "dir_info": {}}))
        with mock.patch.object(service.metadata, "distribution", return_value=local):
            self.assertIsNone(service.installed_commit())
        with mock.patch.object(service.metadata, "distribution", side_effect=service.metadata.PackageNotFoundError):
            self.assertIsNone(service.installed_commit())
