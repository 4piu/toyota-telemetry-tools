"""Collect: automatic readiness, exact-scope confirmation, live progress, safe stop."""

from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.collection import CollectionReadiness

from .context import PageContext
from .dialogs import LiveConfirmDialog
from .jobs import LiveJob
from .widgets import button_row, mark_live, notice, page_title


class CollectPage(QWidget):
    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self.readiness: CollectionReadiness | None = None
        self.job: LiveJob | None = None
        self.confirm_live = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            "Collect",
            "Collection readiness is checked automatically from your targets, DID settings, and "
            "verification results. One run collects one endpoint and stops itself when its "
            "targets are complete.",
        ))

        readiness_group = QGroupBox("Readiness")
        readiness_layout = QVBoxLayout(readiness_group)
        controls = QHBoxLayout()
        self.endpoint = QComboBox()
        self.endpoint.setAccessibleName("Endpoint to collect")
        self.endpoint.setMinimumContentsLength(28)
        self.endpoint.activated.connect(lambda _index: self.check_readiness())
        self.check_button = QPushButton("Check readiness")
        self.context.register("collect.readiness", self.check_button)
        self.check_button.clicked.connect(self.check_readiness)
        controls.addWidget(QLabel("Endpoint"))
        controls.addWidget(self.endpoint)
        controls.addWidget(self.check_button)
        controls.addStretch(1)
        readiness_layout.addLayout(controls)
        self.readiness_text = notice("Open a project to check collection readiness.")
        readiness_layout.addWidget(self.readiness_text)
        self.problem_area = QVBoxLayout()
        readiness_layout.addLayout(self.problem_area)
        self.scope_toggle = QToolButton()
        self.scope_toggle.setText("Technical scope")
        self.scope_toggle.setCheckable(True)
        self.scope_text = QPlainTextEdit()
        self.scope_text.setReadOnly(True)
        self.scope_text.setVisible(False)
        self.scope_text.setAccessibleName("Technical collection scope")
        self.scope_toggle.toggled.connect(self.scope_text.setVisible)
        readiness_layout.addWidget(self.scope_toggle)
        readiness_layout.addWidget(self.scope_text)
        layout.addWidget(readiness_group)

        run_group = QGroupBox("Run")
        run_layout = QVBoxLayout(run_group)
        form = QFormLayout()
        self.note = QLineEdit()
        self.note.setPlaceholderText("optional, e.g. highway drive, passenger operating")
        form.addRow("Run note", self.note)
        run_layout.addLayout(form)
        self.start_button = mark_live(QPushButton("Review and start collection…"))
        self.context.register("collect", self.start_button)
        self.start_button.clicked.connect(self.start_or_stop)
        run_layout.addLayout(button_row(self.start_button))
        self.start_reason = QLabel()
        self.start_reason.setWordWrap(True)
        run_layout.addWidget(self.start_reason)
        self.run_state = QLabel("Idle. Adapter closed.")
        self.progress = QProgressBar()
        self.progress.setFormat("%v of %m targets complete")
        self.progress.setVisible(False)
        self.run_details = QLabel()
        run_layout.addWidget(self.run_state)
        run_layout.addWidget(self.progress)
        run_layout.addWidget(self.run_details)
        self.events_toggle = QToolButton()
        self.events_toggle.setText("Technical details")
        self.events_toggle.setCheckable(True)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(300)
        self.events.setVisible(False)
        self.events_toggle.toggled.connect(self.events.setVisible)
        run_layout.addWidget(self.events_toggle)
        run_layout.addWidget(self.events)
        layout.addWidget(run_group)
        layout.addStretch(1)
        context.project_changed.connect(self.check_readiness)
        context.data_changed.connect(self._refresh_if_idle)
        self.check_readiness()

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.running

    def _refresh_if_idle(self) -> None:
        if not self.running and self.isVisible():
            self.check_readiness()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self.running:
            self.check_readiness()

    def check_readiness(self) -> None:
        if self.running:
            return
        self._clear_problems()
        service = self.context.service
        if service is None:
            self.readiness = None
            self.readiness_text.setText("Open a project to check collection readiness.")
            self._update_start()
            return
        chosen = self.endpoint.currentData()
        try:
            readiness = service.readiness(chosen)
        except (OSError, ValueError) as exc:
            self.readiness = None
            self.readiness_text.setText(f"Cannot check readiness: {exc}")
            self._update_start()
            return
        self.readiness = readiness
        self.endpoint.blockSignals(True)
        self.endpoint.clear()
        for endpoint_id in readiness.endpoints:
            self.endpoint.addItem(endpoint_id, endpoint_id)
        if readiness.endpoint_id:
            self.endpoint.setCurrentIndex(self.endpoint.findData(readiness.endpoint_id))
        elif readiness.endpoints:
            self.endpoint.setCurrentIndex(-1)
        self.endpoint.blockSignals(False)
        if readiness.ready:
            summary = readiness.summary
            load = summary["estimated_bus_utilization"]
            self.readiness_text.setText(
                f"Ready to collect on {readiness.endpoint_id}: {summary['unfinished_targets']} unfinished "
                f"target(s) over {summary['dids']} DID(s)."
                + (f" Estimated bus load {load * 100:.2f}%, {summary['request_rate_hz']:.1f} requests/s." if load is not None else "")
                + f" Storage limit {summary['capture_tree_max_mib']} MiB."
            )
            self.scope_text.setPlainText(json.dumps(readiness.scope, indent=2, sort_keys=True))
        else:
            self.readiness_text.setText("Not ready to collect. Resolve these first:")
            self.scope_text.setPlainText("")
            for problem in readiness.problems:
                self._add_problem(problem)
        self._update_start()

    def _add_problem(self, problem) -> None:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(f"{problem.message}\nFix: {problem.fix}")
        label.setWordWrap(True)
        row_layout.addWidget(label, 1)
        if problem.page != "collect":
            go = QPushButton(f"Open {problem.page.title()}")
            go.clicked.connect(lambda _checked=False, page=problem.page: self.context.navigate(page))
            row_layout.addWidget(go)
        self.problem_area.addWidget(row)

    def _clear_problems(self) -> None:
        while self.problem_area.count():
            item = self.problem_area.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _update_start(self) -> None:
        if self.running:
            self.start_button.setText("Stop safely")
            self.start_button.setEnabled(True)
            self.start_reason.setText("")
            return
        mark_live(self.start_button)
        self.start_button.setText("LIVE - Review and start collection…")
        ready = self.readiness is not None and self.readiness.ready
        busy = self.context.live_job_running
        self.start_button.setEnabled(ready and not busy)
        if busy:
            self.start_reason.setText("Another live job is running.")
        elif not ready:
            self.start_reason.setText("Start is available once readiness has no problems.")
        else:
            self.start_reason.setText("")

    def start_or_stop(self) -> None:
        if self.running:
            self.stop()
            return
        readiness = self.readiness
        service = self.context.service
        if readiness is None or not readiness.ready or service is None:
            return
        scope = readiness.scope
        session = scope["endpoint"]
        details = [
            f"Scope hash: {readiness.digest}",
            f"Connector: {scope['connector']['kind']} {scope['connector']['settings']}",
            f"Endpoint: {readiness.endpoint_id} ({session['tester_can_id']}/{session['response_can_id']}, "
            f"session {session['session_recipe_id']})",
            "Session setup: " + "; ".join(step["request_hex"] for step in session["session_setup"]),
            f"DIDs ({len(scope['acquisitions'])}): " + ", ".join(
                row["acquisition_id"].rsplit("/", 1)[1] for row in scope["acquisitions"]
            ),
            f"Storage: segments of {scope['storage']['segment_max_mib']} MiB, tree limit "
            f"{scope['storage']['capture_tree_max_mib']} MiB",
            f"Run note: {self.note.text() or '-'}",
        ]
        dialog = LiveConfirmDialog(
            "Start collection",
            f"Start LIVE collection of {len(scope['acquisitions'])} DID(s) on {readiness.endpoint_id}?",
            details, self,
        )
        if not self.confirm_live(dialog):
            return
        note = self.note.text().strip() or None
        self.events.clear()
        self.progress.setVisible(True)
        self.progress.setRange(0, max(1, readiness.summary["unfinished_targets"]))
        self.progress.setValue(0)
        self.run_state.setText("LIVE - starting…")

        def work(emit, stop):
            return service.collect(readiness, note=note, event_sink=emit, stop_event=stop)

        self.job = LiveJob(work, self)
        self.job.event.connect(self._event)
        self.job.finished.connect(self._finished)
        self.job.failed.connect(self._failed)
        self.context.set_live(True)
        self.job.start()
        self._update_start()

    def stop(self) -> None:
        if self.job is not None:
            self.job.request_stop()
            self.run_state.setText("LIVE - stopping safely…")

    def _event(self, event: dict) -> None:
        kind = event["event"]
        if kind == "started":
            self.run_state.setText(f"LIVE - collecting. Run {event['run_id']}.")
        elif kind == "progress":
            self.progress.setRange(0, max(1, event["targets"]))
            self.progress.setValue(event["targets_complete"])
            self.run_details.setText(
                f"Elapsed {event['elapsed_ms'] // 1000} s, {event['exchanges']} reads, "
                f"{event['failed_exchanges']} failed."
            )
        elif kind == "heartbeat":
            self.events.appendPlainText(f"heartbeat {event['state']} {event['current_acquisition_id'] or ''}")
        elif kind == "exchange":
            self.events.appendPlainText(f"{event['acquisition_id']}: {event['outcome']}")
        elif kind == "error":
            self.events.appendPlainText(f"error: {event['detail']}")
        elif kind == "finished":
            self.events.appendPlainText(f"finished: {event['reason']}")

    def _finished(self, reason: str) -> None:
        labels = {
            "plan_complete": "Completed: every target in this run is complete.",
            "operator_stop": "Stopped safely.",
            "size_limit": "Stopped at the storage limit; the run was finalized.",
            "transport_lost": "Transport lost; the valid prefix was finalized.",
        }
        self.run_state.setText(labels.get(reason, f"Finished: {reason}") + " Adapter closed. See Recordings.")
        self._done()

    def _failed(self, detail: str) -> None:
        self.run_state.setText(
            f"Failed: {detail.splitlines()[0]} Adapter closed. Check Recordings for a run that needs recovery."
        )
        self._done()

    def _done(self) -> None:
        self.job = None
        self.context.set_live(False)
        self.context.changed()
        self.check_readiness()
