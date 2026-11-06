"""Render README screenshots of the desktop application from a synthetic project.

    uv run --extra gui python tools/screenshots/take.py docs/images

Builds the invented demo workspace from demo_project.py in a temporary
folder, opens the application offscreen, and saves one PNG per page shown.
Nothing touches a real workspace, vehicle, or adapter.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform has no font database of its own on Windows.
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from demo_project import build  # noqa: E402
from toyota_telemetry.ui.shell import OperatorWindow  # noqa: E402


SIZE = (1360, 820)


def settle(app: QApplication, window: OperatorWindow, seconds: float = 0.6) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        app.processEvents()
        if window.context.worker.idle:
            break
        time.sleep(0.02)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication(["telemetry-ui"])
    QCoreApplication.setOrganizationName("toyota-telemetry-tools-screenshots")
    with tempfile.TemporaryDirectory() as directory:
        root = build(Path(directory) / "example-car")
        window = OperatorWindow(root, restore_settings=False)
        window.resize(*SIZE)
        window.show()
        settle(app, window)

        window.navigate("signals")
        settle(app, window)
        window.signals_page.select("signal", "1/0x0110/0..15")
        settle(app, window)
        window.grab().save(str(output / "signals.png"))

        window.navigate("collect")
        settle(app, window)
        window.collect_page.check_readiness()
        settle(app, window)
        window.grab().save(str(output / "collect.png"))

        window.navigate("recordings")
        settle(app, window)
        window.recordings_page.select_signal("3/0x0130/0..15")
        settle(app, window, 1.5)
        window.grab().save(str(output / "recordings.png"))

        window.close()
        settle(app, window, 0.2)
    print(output)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
