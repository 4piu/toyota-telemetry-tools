"""State and services shared by the shell and its pages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from toyota_telemetry.operator import OPERATIONS, OperatorService


OPERATIONS_BY_ID = {operation.id: operation for operation in OPERATIONS}


class PageContext(QObject):
    """Owns the open project and the registry proving GUI operation parity."""

    project_changed = Signal()
    data_changed = Signal()
    busy_changed = Signal(bool)

    def __init__(self, parent: QWidget, navigate: Callable[[str], None]) -> None:
        super().__init__(parent)
        self._parent = parent
        self._navigate = navigate
        self.service: OperatorService | None = None
        self.actions: dict[str, QWidget] = {}
        self.live_job_running = False
        self.message: Callable[[str], None] = lambda text: None

    def open(self, root: Path) -> None:
        self.service = OperatorService(root)
        self.project_changed.emit()

    def close(self) -> None:
        self.service = None
        self.project_changed.emit()

    def register(self, operation_id: str, widget: QWidget) -> QWidget:
        """Bind a widget to an operation and show its CLI equivalent."""

        operation = OPERATIONS_BY_ID[operation_id]
        self.actions[operation_id] = widget
        hint = f"{operation.title}. Command line: {operation.cli}"
        existing = widget.toolTip()
        widget.setToolTip(f"{existing}\n{hint}" if existing else hint)
        if not widget.accessibleDescription():
            widget.setAccessibleDescription(hint)
        return widget

    def navigate(self, page: str) -> None:
        self._navigate(page)

    def changed(self) -> None:
        self.data_changed.emit()

    def error(self, title: str, exc: BaseException | str) -> None:
        QMessageBox.warning(self._parent, title, str(exc))

    def set_live(self, running: bool) -> None:
        self.live_job_running = running
        self.busy_changed.emit(running)
