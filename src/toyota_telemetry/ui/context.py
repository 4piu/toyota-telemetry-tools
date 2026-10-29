"""State and services shared by the shell and its pages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from toyota_telemetry.operator import OPERATIONS, OperatorService

from .worker import ServiceWorker


OPERATIONS_BY_ID = {operation.id: operation for operation in OPERATIONS}


class PageContext(QObject):
    """Owns the open project, background work, and the operation registry."""

    project_changed = Signal()
    data_changed = Signal()
    busy_changed = Signal(bool)

    def __init__(self, parent: QWidget, navigate: Callable[[str], None]) -> None:
        super().__init__(parent)
        self._parent = parent
        self._navigate = navigate
        self.service: OperatorService | None = None
        self.worker = ServiceWorker(self)
        self.actions: dict[str, QWidget] = {}
        self.live_job_running = False
        self.message: Callable[[str], None] = lambda text: None
        self._fingerprint: tuple | None = None

    def open(self, root: Path) -> None:
        self.service = OperatorService(root)
        self._fingerprint = self.service.fingerprint()
        self.project_changed.emit()

    def close(self) -> None:
        self.service = None
        self._fingerprint = None
        self.project_changed.emit()

    def run(
        self,
        key: str,
        work: Callable[[OperatorService], Any],
        done: Callable[[Any], None],
        failed: Callable[[str], None] | None = None,
        *,
        title: str = "Operation failed",
    ) -> None:
        """Run ``work(service)`` off the UI thread; ``done`` gets the result."""

        service = self.service
        if service is None:
            return

        def report(detail: str) -> None:
            if failed is not None:
                failed(detail)
            else:
                self.error(title, detail.split("\n\n", 1)[0])

        self.worker.submit(key, lambda: work(service), done, report)

    def register(self, operation_id: str, widget: QWidget, tip: str | None = None) -> QWidget:
        """Bind a widget to an operation (for the parity test) and describe it."""

        operation = OPERATIONS_BY_ID[operation_id]
        self.actions[operation_id] = widget
        description = tip or operation.title
        widget.setToolTip(description)
        widget.setProperty("baseToolTip", description)
        widget.setAccessibleDescription(description)
        return widget

    def navigate(self, page: str) -> None:
        self._navigate(page)

    def changed(self) -> None:
        if self.service is not None:
            self._fingerprint = self.service.fingerprint()
        self.data_changed.emit()

    def check_outside_changes(self) -> None:
        """Refresh only if project files changed since the last refresh."""

        if self.service is None or self.live_job_running:
            return
        fingerprint = self.service.fingerprint()
        if fingerprint != self._fingerprint:
            self.changed()

    def error(self, title: str, exc: BaseException | str) -> None:
        QMessageBox.warning(self._parent, title, str(exc))

    def set_live(self, running: bool) -> None:
        self.live_job_running = running
        self.busy_changed.emit(running)
