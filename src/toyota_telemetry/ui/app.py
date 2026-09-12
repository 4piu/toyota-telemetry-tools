"""Console entry point for the optional desktop operator application."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from toyota_telemetry.ui.shell import OperatorWindow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open the telemetry operator application")
    parser.add_argument("project", nargs="?", type=Path, help="existing telemetry project")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication(["telemetry-ui"])
    QCoreApplication.setOrganizationName("70Y074")
    QCoreApplication.setApplicationName("Telemetry Operator")
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
