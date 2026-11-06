"""Build the Windows adapter workers into every wheel.

The workers are compiled from `backends/` with the .NET Framework C# compiler
that ships with Windows, so installing from a Git URL needs no clone, SDK, or
manual build step. A regular wheel always compiles them fresh and is tagged
for 64-bit Windows; an editable install compiles them only when missing (run
`backends/build.ps1` after changing `backends/`).
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


WORKERS = ("pcan", "j2534", "gsusb", "slcan", "tisdb")


class WorkerBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        if sys.platform != "win32":
            raise RuntimeError(
                "toyota-telemetry-tools supports Windows only: its adapter workers "
                "are Windows programs built with the C# compiler Windows ships."
            )
        root = Path(self.root)
        output = root / "src" / "toyota_telemetry" / "workers"
        executables = [output / f"telemetry-{name}.exe" for name in WORKERS]
        if version != "editable" or not all(path.is_file() for path in executables):
            _run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(root / "backends" / "build.ps1")],
                "building the adapter workers",
            )
            for path in executables:
                _run([str(path), "--self-test"], f"self-testing {path.name}")
        if version != "editable":
            build_data["pure_python"] = False
            build_data["tag"] = "py3-none-win_amd64"


def _run(command: list[str], what: str) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if completed.returncode != 0:
        raise RuntimeError(
            f"{what} failed (exit code {completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}".rstrip()
        )
