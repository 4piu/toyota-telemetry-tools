from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from tests.project_fixture import make_current_project
from toyota_telemetry import cli
from toyota_telemetry.exporting import check_destination
from toyota_telemetry.operator import OperatorService
from toyota_telemetry.repository import ProjectRepository


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.base = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_init_creates_an_empty_valid_workspace(self) -> None:
        root = self.base / "My Car"
        code, out, err = run_cli("init", str(root), "--profile-id", "my-profile")
        self.assertEqual(code, 0, err)
        self.assertIn("Created workspace my-car", out)
        repository = ProjectRepository(root)
        self.assertEqual((repository.project_id, repository.profile_id), ("my-car", "my-profile"))
        self.assertEqual(repository.transport["endpoints"], [])
        self.assertTrue(repository.paths.captures.is_dir())
        self.assertTrue(OperatorService(root).check().ok)
        status = OperatorService(root).status()
        self.assertTrue(status.valid, status.error)
        self.assertEqual(status.next_action.text, "Configure the connector")

    def test_init_refuses_a_folder_with_files(self) -> None:
        root = self.base / "used"
        root.mkdir()
        (root / "notes.txt").write_text("keep\n")
        code, _, err = run_cli("init", str(root))
        self.assertEqual(code, 1)
        self.assertIn("not an empty folder", err)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["notes.txt"])

    def test_export_writes_the_result_only(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        make_current_project(root)
        folder = self.base / "exported"
        code, out, err = run_cli("export", str(folder), "--project-root", str(root))
        self.assertEqual(code, 0, err)
        self.assertIn("Exported result", out)
        pointer = json.loads((folder / "result/current.json").read_text())
        generation = folder / "result/generations" / pointer["generation_id"]
        manifest = json.loads((generation / "manifest.json").read_text())
        self.assertEqual(manifest["project_id"], "test-project")
        self.assertIn("toolkit_commit", manifest)
        self.assertEqual(
            json.loads((generation / "transport.json").read_text()),
            json.loads((root / "config/transport.json").read_text()),
        )
        exported = {path.relative_to(folder).parts[0] for path in folder.rglob("*") if path.is_file()}
        self.assertEqual(exported, {"result"})

    def test_export_refuses_the_workspace_and_another_project(self) -> None:
        root = self.base / "workspace"
        root.mkdir()
        make_current_project(root)
        repository = ProjectRepository(root)
        for inside in (root, root / "out", self.base):
            with self.assertRaisesRegex(ValueError, "workspace"):
                check_destination(repository, inside)
        other = self.base / "other"
        OperatorService(root).export_result(other)
        manifest_dir = next((other / "result/generations").iterdir())
        manifest = json.loads((manifest_dir / "manifest.json").read_text())
        manifest["project_id"] = "someone-else"
        (manifest_dir / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "holds results of project someone-else"):
            check_destination(repository, other)


if __name__ == "__main__":
    unittest.main()
