"""Live view of one DID: values as they are read, nothing recorded."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.i18n import _
from toyota_telemetry.operator.watch import WatchScope

from .context import PageContext
from .jobs import LiveJob
from .widgets import button_row, mark_live


class WatchWindow(QDialog):
    """Pause stops the worker (nothing is sent); Resume opens the session again."""

    def __init__(self, context: PageContext, scope: WatchScope, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.scope = scope
        self.job: LiveJob | None = None
        self._closing = False
        self._reads = 0
        self.setWindowTitle(_("Live view: {title}").format(title=title))
        self.setMinimumSize(560, 320)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        layout = QVBoxLayout(self)
        heading = QLabel(
            _("{title}, read every {interval} ms. Nothing is recorded.").format(title=title, interval=scope.interval_ms)
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.table = QTableWidget(len(scope.signals), 3)
        self.table.setHorizontalHeaderLabels([_("Signal"), _("Value"), _("Raw")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._rows = {}
        for row, signal in enumerate(scope.signals):
            self._rows[signal["signal_ref"]] = row
            name = QTableWidgetItem(signal["name"])
            name.setToolTip(signal["signal_ref"])
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(""))
            self.table.setItem(row, 2, QTableWidgetItem(""))
        layout.addWidget(self.table, 1)
        self.status = QLabel(_("Starting…"))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.pause_button = mark_live(QPushButton(_("Pause")))
        self.pause_button.setToolTip(_("Pause stops sending; Resume starts again"))
        self.stop_button = QPushButton(_("Stop"))
        self.stop_button.setToolTip(_("Stop reading and close"))
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button.clicked.connect(self.stop)
        layout.addLayout(button_row(self.pause_button, self.stop_button, stretch_first=True))

    @property
    def running(self) -> bool:
        return self.job is not None

    def start(self) -> None:
        service = self.context.service
        if service is None or self.job is not None:
            return
        scope = self.scope
        self.job = LiveJob(
            lambda emit, stop: service.watch(scope, on_event=emit, stop_event=stop), self,
        )
        self.job.event.connect(self._event)
        self.job.finished.connect(self._finished)
        self.job.failed.connect(self._failed)
        self.context.set_live(True)
        self.pause_button.setText(_("Pause"))
        self.status.setText(_("Opening the session…"))
        self.job.start()

    def toggle_pause(self) -> None:
        if self.job is not None:
            self.pause_button.setEnabled(False)
            self.status.setText(_("Pausing after the current read…"))
            self.job.request_stop()
        else:
            self.start()

    def stop(self) -> None:
        self._closing = True
        if self.job is not None:
            self.status.setText(_("Stopping after the current read…"))
            self.job.request_stop()
        else:
            self.close()

    def _event(self, event: dict) -> None:
        if event["event"] == "started":
            self.status.setText(_("Reading every {interval} ms.").format(interval=event["interval_ms"]))
            return
        if event["event"] != "read":
            return
        self._reads += 1
        if event["outcome"] != "positive":
            if event.get("nrc"):
                text = _("Read {number}: {outcome} (code {code}).").format(
                    number=self._reads, outcome=event["outcome"], code=event["nrc"],
                )
            else:
                text = _("Read {number}: {outcome}.").format(number=self._reads, outcome=event["outcome"])
            self.status.setText(text)
            return
        if event.get("latency_ms") is not None:
            text = _("Read {number}, {latency} ms.").format(number=self._reads, latency=event["latency_ms"])
        else:
            text = _("Read {number}.").format(number=self._reads)
        self.status.setText(text)
        units = {signal["signal_ref"]: signal["unit"] for signal in self.scope.signals}
        for value in event["values"]:
            row = self._rows[value["signal_ref"]]
            self.table.item(row, 1).setText(value_text(value, units[value["signal_ref"]]))
            self.table.item(row, 2).setText("" if value["raw"] is None else str(value["raw"]))

    def _finished(self, reason: str) -> None:
        self._ended(_("Paused; nothing is being sent.") if reason == "stopped" else f"{reason.capitalize()}.")

    def _failed(self, detail: str) -> None:
        self._ended(_("Stopped: {error}").format(error=detail.splitlines()[0]))

    def _ended(self, text: str) -> None:
        self.job = None
        self.context.set_live(False)
        self.status.setText(text)
        self.pause_button.setText(_("Resume"))
        self.pause_button.setEnabled(True)
        if self._closing:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.job is not None:
            # Close once the worker has stopped and released the adapter.
            self._closing = True
            self.job.request_stop()
            event.ignore()
            return
        super().closeEvent(event)


def value_text(value: dict, unit: str) -> str:
    """State text when the raw value has one, else the decoded number and unit."""

    if value["text"]:
        return value["text"]
    if value["value"] is None:
        return "?"
    return f"{value['value']:g}" + (f" {unit}" if unit else "")
