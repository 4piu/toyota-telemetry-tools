"""Console entry point for the optional desktop operator application."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from toyota_telemetry.ui.shell import OperatorWindow
from toyota_telemetry.ui.widgets.icons import app_icon


def _use_own_taskbar_icon() -> None:
    """Group the window under its own taskbar entry and icon, not python.exe's."""

    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("toyota-telemetry-tools.telemetry-ui")
        except (AttributeError, OSError):
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open the telemetry operator application")
    parser.add_argument("project", nargs="?", type=Path, help="existing telemetry project")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication(["telemetry-ui"])
    QCoreApplication.setOrganizationName("toyota-telemetry-tools")
    QCoreApplication.setApplicationName("Telemetry Operator")
    _use_own_taskbar_icon()
    app.setWindowIcon(app_icon())
    window = OperatorWindow(args.project, restore_settings=not args.smoke_test)
    window.show()
    if args.smoke_test:
        app.processEvents()
        window.close()
        app.processEvents()
        return 0
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
