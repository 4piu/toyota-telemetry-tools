"""Collect: automatic readiness, exact-scope confirmation, target progress, safe stop."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.collection import SLICES, STRATEGIES, CollectionReadiness, Rotation
from toyota_telemetry.operator.inventory import InventoryTree

from .context import PageContext
from .dialogs import LiveConfirmDialog
from .jobs import LiveJob
from .names import count, endpoint_label, still_needed, target_summary
from .widgets import LIVE_TIP, Disclosure, button_row, mark_live, notice, page_title, set_enabled

ENDPOINT = Qt.ItemDataRole.UserRole
SLICE_MINUTES = (2, 5, 10, 15)
DEFAULT_SLICE_MINUTES = 5


class CollectPage(QWidget):
    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self.readiness: CollectionReadiness | None = None
        self.labels: dict[str, str] = {}
        self.job: LiveJob | None = None
        self.confirm_live = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            "Collect",
            "Run collection; target progress",
        ))

        readiness_group = QGroupBox("Readiness")
        readiness_layout = QVBoxLayout(readiness_group)
        controls = QHBoxLayout()
        # ECUs to collect: checked ones are collected, in the list's order.
        self.ecus = QListWidget()
        self.ecus.setAccessibleName("ECUs to collect")
        self.ecus.setToolTip("Check ECUs to collect; drag to order")
        self.ecus.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.ecus.setMaximumHeight(140)
        self.ecus.itemChanged.connect(self._ecus_changed)
        self.ecus.model().rowsMoved.connect(lambda *_: self._ecus_changed())
        self._order: list[str] = []
        self._checked: set[str] = set()
        self._known: set[str] = set()
        self.strategy = QComboBox()
        self.strategy.setAccessibleName("How to move between ECUs")
        self.strategy.setToolTip("How to move between checked ECUs")
        for key, label in STRATEGIES.items():
            self.strategy.addItem(label, key)
        self.strategy.activated.connect(lambda _index: self.check_readiness())
        self.slice = QComboBox()
        self.slice.setAccessibleName("Time on each ECU")
        self.slice.setToolTip("Time on each ECU before the next")
        for minutes in SLICE_MINUTES:
            self.slice.addItem(f"{minutes} min", minutes * 60)
        self.slice.setCurrentIndex(SLICE_MINUTES.index(DEFAULT_SLICE_MINUTES))
        self.slice.activated.connect(lambda _index: self.check_readiness())
        self.check_button = QPushButton("Check")
        self.context.register("collect.readiness", self.check_button, "Recheck; nothing is transmitted")
        self.check_button.clicked.connect(self.check_readiness)
        controls.addWidget(self.ecus, 1)
        self.turns = QFormLayout()
        self.turns.addRow("Several ECUs", self.strategy)
        self.turns.addRow("Switch every", self.slice)
        self.turns.addRow(self.check_button)
        controls.addLayout(self.turns)
        readiness_layout.addLayout(controls)
        self._show_turn_options(0)
        self.readiness_text = notice("No project open.")
        readiness_layout.addWidget(self.readiness_text)
        self.problem_area = QVBoxLayout()
        readiness_layout.addLayout(self.problem_area)
        self.scope_text = QPlainTextEdit()
        self.scope_text.setReadOnly(True)
        self.scope_text.setAccessibleName("Technical collection scope")
        self.scope_toggle = Disclosure("Scope", self.scope_text)
        self.scope_toggle.setToolTip("The exact compiled scope")
        readiness_layout.addWidget(self.scope_toggle)
        readiness_layout.addWidget(self.scope_text)
        layout.addWidget(readiness_group)

        run_group = QGroupBox("Run")
        run_layout = QVBoxLayout(run_group)
        form = QFormLayout()
        self.note = QLineEdit()
        self.note.setPlaceholderText("optional")
        self.note.setToolTip("Saved with the run, e.g. highway")
        form.addRow("Note", self.note)
        run_layout.addLayout(form)
        self.start_button = mark_live(QPushButton("Start"))
        self.context.register("collect", self.start_button, f"Stops when targets are met\n{LIVE_TIP}")
        self.start_button.clicked.connect(self.start_or_stop)
        run_layout.addLayout(button_row(self.start_button))
        self.run_state = QLabel("Idle. Adapter closed.")
        self.progress = QProgressBar()
        self.progress.setFormat("%v of %m targets complete")
        self.progress.setVisible(False)
        self.run_details = QLabel()
        run_layout.addWidget(self.run_state)
        run_layout.addWidget(self.progress)
        run_layout.addWidget(self.run_details)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(300)
        self.events_toggle = Disclosure("Read log", self.events)
        self.events_toggle.setToolTip("Every read as it happens")
        run_layout.addWidget(self.events_toggle)
        run_layout.addWidget(self.events)
        layout.addWidget(run_group)

        self.targets_group = QGroupBox("Targets")
        self.targets_group.setToolTip("Collected so far, across all runs")
        targets_layout = QVBoxLayout(self.targets_group)
        self.targets = QTreeWidget()
        self.targets.setRootIsDecorated(False)
        self.targets.setAlternatingRowColors(True)
        self.targets.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.targets.setAccessibleName("Target progress")
        self.targets.setStyleSheet("QTreeView::item { padding: 3px 6px; }")
        self.targets.setHeaderLabels(["Signal", "ECU", "Target", "Recorded", "Still needs"])
        for column, tip in enumerate((
            "Signal with a target", "Where it is read", "What it must collect",
            "Collected so far across valid runs", "Requirements not met yet",
        )):
            self.targets.headerItem().setToolTip(column, tip)
        header = self.targets.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((220, 180, 220, 170)):
            header.resizeSection(column, width)
        header.setStretchLastSection(True)
        targets_layout.addWidget(self.targets)
        layout.addWidget(self.targets_group, 1)
        self._stale = True
        context.project_changed.connect(self._mark_stale)
        context.data_changed.connect(self._mark_stale)
        self._show_readiness(None)

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.running

    def _mark_stale(self) -> None:
        self._stale = True
        if self.isVisible() and not self.running:
            self.check_readiness()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._stale and not self.running:
            self.check_readiness()

    def check_readiness(self) -> None:
        """Recompute readiness in the background; nothing is transmitted."""

        if self.running:
            return
        self._stale = False
        if self.context.service is None:
            self._show_readiness(None)
            return
        chosen = [endpoint_id for endpoint_id in self._order if endpoint_id in self._checked]
        first = not self._known
        self.check_button.setEnabled(False)
        self.readiness_text.setText("Checking…")

        def failed(detail: str) -> None:
            self.check_button.setEnabled(True)
            self.readiness = None
            self._clear_problems()
            self.readiness_text.setText(f"Cannot check readiness: {detail.split(chr(10) * 2, 1)[0]}")
            self._update_start()

        strategy, slice_seconds = self.strategy.currentData(), self.slice.currentData()

        def work(service):
            if len(chosen) == 1:
                readiness = service.readiness(chosen[0])
            elif chosen:
                readiness = service.readiness(rotation=Rotation(strategy, slice_seconds, tuple(chosen)))
            else:
                # First look: every ready ECU is checked, in turn when several.
                readiness = service.readiness()
                if first and len(readiness.endpoints) > 1:
                    readiness = service.readiness(rotation=Rotation(strategy, slice_seconds))
            self._names = service.signal_names(readiness.scope.get("targets", []))
            self._tree = service.tree()
            return readiness

        self.context.run("collect.readiness", work, self._show_readiness, failed)

    def _show_readiness(self, readiness: CollectionReadiness | None) -> None:
        self.check_button.setEnabled(self.context.service is not None)
        self._clear_problems()
        self.readiness = readiness
        if readiness is None:
            self.readiness_text.setText("No project open.")
            self._show_targets(None)
            self._update_start()
            return
        self._show_targets(getattr(self, "_tree", None))
        if readiness.rotation is not None:
            # The boxes show what would be confirmed.
            self.strategy.setCurrentIndex(self.strategy.findData(readiness.rotation["strategy"]))
            if readiness.rotation["slice_seconds"]:
                self.slice.setCurrentIndex(max(0, self.slice.findData(readiness.rotation["slice_seconds"])))
        self._fill_ecus(readiness)
        rotating = readiness.rotation is not None
        if self._none_checked:
            self.readiness_text.setText("Check at least one ECU to collect.")
            self.scope_text.setPlainText("")
            self._update_start()
            return
        if readiness.ready:
            summary = readiness.summary
            load = summary["estimated_bus_utilization"]
            where = (
                f"{summary['endpoint_count']} ECUs {self._turns_text(readiness.rotation)}"
                if rotating else self._label(readiness.endpoint_id)
            )
            self.readiness_text.setText(
                f"Ready: {where}, {summary['unfinished_targets']} target(s), "
                f"{summary['dids']} DID(s)"
                + (f", bus load {load * 100:.2f}%, {'up to ' if rotating else ''}"
                   f"{summary['request_rate_hz']:.1f} reads/s" if load is not None else "")
                + f", storage limit {summary['capture_tree_max_mib']} MiB."
            )
            self.scope_text.setPlainText(json.dumps(readiness.scope, indent=2, sort_keys=True))
        elif readiness.complete and not readiness.problems:
            self.readiness_text.setText("✓ All targets met. Nothing left to collect.")
            self.scope_text.setPlainText("")
        else:
            self.readiness_text.setText("Not ready:")
            self.scope_text.setPlainText("")
            for problem in readiness.problems:
                self._add_problem(problem)
        self._update_start()

    def _fill_ecus(self, readiness: CollectionReadiness) -> None:
        """List the ready ECUs; keep the user's order and checks, check new ones."""

        ready = list(readiness.endpoints)
        self._order = [e for e in self._order if e in ready] + [e for e in ready if e not in self._order]
        for endpoint_id in ready:
            if endpoint_id not in self._known:
                self._known.add(endpoint_id)
                self._checked.add(endpoint_id)
        self._checked &= set(ready)
        counts = readiness.summary.get("endpoint_targets", {})
        self.ecus.blockSignals(True)
        self.ecus.clear()
        for endpoint_id in self._order:
            targets = counts.get(endpoint_id)
            item = QListWidgetItem(self._label(endpoint_id) + (f"  ({count(targets, 'target')})" if targets else ""))
            item.setData(ENDPOINT, endpoint_id)
            item.setToolTip(endpoint_id)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled
            )
            item.setCheckState(Qt.CheckState.Checked if endpoint_id in self._checked else Qt.CheckState.Unchecked)
            self.ecus.addItem(item)
        self.ecus.blockSignals(False)
        self._none_checked = bool(ready) and not self._checked
        self._show_turn_options(len(self._checked))

    def _ecus_changed(self, *_args) -> None:
        self._order = [self.ecus.item(row).data(ENDPOINT) for row in range(self.ecus.count())]
        self._checked = {
            self.ecus.item(row).data(ENDPOINT) for row in range(self.ecus.count())
            if self.ecus.item(row).checkState() == Qt.CheckState.Checked
        }
        self.check_readiness()

    def _show_turn_options(self, checked: int) -> None:
        several = checked > 1
        self.turns.setRowVisible(self.strategy, several)
        self.turns.setRowVisible(self.slice, several and self.strategy.currentData() == SLICES)

    def _turns_text(self, rotation: dict) -> str:
        if rotation["strategy"] == SLICES:
            return f"in turn, {rotation['slice_seconds'] // 60} min each"
        return "one after another"

    def _label(self, endpoint_id: str | None) -> str:
        return self.labels.get(endpoint_id or "", endpoint_id or "")

    def _show_targets(self, tree: InventoryTree | None) -> None:
        """Fill the target table: unmet targets first, then met ones."""

        self.labels = {
            endpoint.endpoint_id: endpoint_label(endpoint)
            for endpoint in (tree.endpoints if tree else []) if endpoint.endpoint_id
        }
        self.targets.clear()
        rows = [(endpoint, signal) for endpoint, _did, signal in (tree.signals() if tree else []) if signal.target]
        rows.sort(key=lambda row: (bool((row[1].progress or {}).get("complete")), row[1].name.lower()))
        met = 0
        for endpoint, signal in rows:
            progress = signal.progress or {}
            counters = progress.get("counters") or {}
            complete = bool(progress.get("complete"))
            met += complete
            missing = [name for name, ok in (progress.get("requirements_met") or {}).items() if not ok]
            recorded = (
                f"{count(counters['samples'], 'sample')}, {count(counters['runs'], 'run')}"
                if counters.get("samples") else "Nothing yet"
            )
            item = QTreeWidgetItem([
                signal.name, endpoint_label(endpoint), target_summary(signal.target, signal.states), recorded,
                "✓ Met" if complete else still_needed(missing, signal.target, counters, signal.states),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, signal.signal_ref)
            self.targets.addTopLevelItem(item)
        self.targets_group.setTitle(f"Targets: {met} of {len(rows)} met" if rows else "Targets")
        if not rows:
            empty = QTreeWidgetItem(["No targets yet. Set them on Signals."])
            self.targets.addTopLevelItem(empty)
            empty.setFirstColumnSpanned(True)

    def _add_problem(self, problem) -> None:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(problem.hint)
        label.setWordWrap(True)
        row_layout.addWidget(label, 1)
        self.problem_area.addWidget(row)

    def _clear_problems(self) -> None:
        while self.problem_area.count():
            item = self.problem_area.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _update_start(self) -> None:
        if self.running:
            self.start_button.setText("Stop")
            self.start_button.setIcon(QIcon())
            self.start_button.setToolTip("Stops safely and keeps the run")
            self.start_button.setEnabled(True)
            return
        mark_live(self.start_button)
        self.start_button.setText("Start")
        self.start_button.setToolTip(self.start_button.property("baseToolTip") or "")
        ready = self.readiness is not None and self.readiness.ready and not getattr(self, "_none_checked", False)
        busy = self.context.live_job_running
        complete = self.readiness is not None and self.readiness.complete
        set_enabled(
            self.start_button, ready and not busy,
            "Another live job is running." if busy
            else "All targets met." if complete else "Readiness has problems.",
        )

    def start_or_stop(self) -> None:
        if self.running:
            self.stop()
            return
        readiness = self.readiness
        service = self.context.service
        if readiness is None or not readiness.ready or service is None:
            return
        if readiness.rotation is not None:
            dialog = self._rotation_dialog(readiness)
        else:
            dialog = self._endpoint_dialog(readiness)
        if not self.confirm_live(dialog):
            return
        note = self.note.text().strip() or None
        self.events.clear()
        self.progress.setVisible(True)
        self.progress.setFormat(
            "%v of %m targets on this ECU" if readiness.rotation else "%v of %m targets complete"
        )
        self.progress.setRange(0, max(1, readiness.summary["unfinished_targets"]))
        self.progress.setValue(0)
        self.run_state.setText("LIVE - starting…")
        self._rotating = readiness.rotation is not None
        self._current = ""

        def work(emit, stop):
            return service.collect(readiness, note=note, event_sink=emit, stop_event=stop)

        self.job = LiveJob(work, self)
        self.job.event.connect(self._event)
        self.job.finished.connect(self._finished)
        self.job.failed.connect(self._failed)
        self.context.set_live(True)
        self.job.start()
        self._update_start()

    def _rotation_dialog(self, readiness: CollectionReadiness) -> LiveConfirmDialog:
        scope = readiness.scope
        connector = scope["connector"]
        device = ", ".join(f"{value}" for value in connector["settings"].values())
        details = [
            f"Adapter: {connector['kind']} {device}".rstrip(),
            f"ECUs {self._turns_text(readiness.rotation)}, in this order:",
            *[f"    {self._label(row['endpoint']['endpoint_id'])}: {count(len(row['targets']), 'signal')}, "
              f"{len(row['acquisitions'])} DID(s)" for row in scope["endpoints"]],
            "One run per ECU and turn; ECUs whose targets are met are skipped.",
            *(["Each ECU is read until its targets are met: one that cannot be met",
               "now keeps the next ECUs waiting until you stop."]
              if readiness.rotation["strategy"] != SLICES else []),
            "Stops when every target is met or you stop it.",
            f"Storage limit: {scope['storage']['capture_tree_max_mib']} MiB",
            f"Note: {self.note.text() or '-'}",
        ]
        technical = [f"Scope fingerprint: {readiness.digest}"]
        for row in scope["endpoints"]:
            session = row["endpoint"]
            technical += [
                f"Endpoint ID: {session['endpoint_id']} ({session['tester_can_id']}/{session['response_can_id']})",
                f"  Session {session['session_recipe_id']}: "
                + "; ".join(step["request_hex"] for step in session["session_setup"]),
                *[f"  {item['acquisition_id']}: request {item['request_hex']}, every {item['target_interval_ms']} ms"
                  for item in row["acquisitions"]],
            ]
        return LiveConfirmDialog(
            "Collect",
            f"LIVE collection of {len(scope['targets'])} signal(s) on {len(scope['endpoints'])} ECUs "
            f"{self._turns_text(readiness.rotation)}?",
            details, self, technical=technical,
        )

    def _endpoint_dialog(self, readiness: CollectionReadiness) -> LiveConfirmDialog:
        scope = readiness.scope
        session = scope["endpoint"]
        connector = scope["connector"]
        device = ", ".join(f"{value}" for value in connector["settings"].values())
        names = getattr(self, "_names", {})
        details = [
            f"Endpoint: {self._label(readiness.endpoint_id)}",
            f"Adapter: {connector['kind']} {device}".rstrip(),
            f"Signals ({len(scope['targets'])}):",
            *[f"    {names.get(ref, ref)}" for ref in scope["targets"]],
            f"Reads {len(scope['acquisitions'])} DID(s); stops when these targets are complete or you stop it.",
            f"Storage limit: {scope['storage']['capture_tree_max_mib']} MiB",
            f"Note: {self.note.text() or '-'}",
        ]
        technical = [
            f"Endpoint ID: {readiness.endpoint_id} ({session['tester_can_id']}/{session['response_can_id']})",
            f"Scope fingerprint: {readiness.digest}",
            f"Session {session['session_recipe_id']}: "
            + "; ".join(step["request_hex"] for step in session["session_setup"]),
            *[f"{row['acquisition_id']}: request {row['request_hex']}, every {row['target_interval_ms']} ms"
              for row in scope["acquisitions"]],
        ]
        return LiveConfirmDialog(
            "Collect",
            f"LIVE collection of {len(scope['targets'])} signal(s) on {self._label(readiness.endpoint_id)}?",
            details, self, technical=technical,
        )

    def stop(self) -> None:
        if self.job is not None:
            self.job.request_stop()
            self.run_state.setText("LIVE - stopping safely…")

    def _event(self, event: dict) -> None:
        kind = event["event"]
        if kind == "scope" and getattr(self, "_rotating", False):
            self._current = self._label(event["endpoint_id"])
            self.run_state.setText(f"LIVE - switching to {self._current}…")
        elif kind == "started":
            current = getattr(self, "_current", "")
            self.run_state.setText(f"LIVE - collecting from {current}." if current else "LIVE - collecting.")
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
            "slice_end": "Turn ended.",
            "operator_stop": "Stopped safely.",
            "size_limit": "Stopped at the storage limit; the run was finalized.",
            "transport_lost": "Transport lost; the valid prefix was finalized.",
        }
        self.run_state.setText(labels.get(reason, f"Finished: {reason}") + " Adapter closed.")
        self.note.clear()
        self._done()

    def _failed(self, detail: str) -> None:
        self.run_state.setText(f"Failed: {detail.splitlines()[0]} Adapter closed.")
        self._done()

    def _done(self) -> None:
        self.job = None
        self.context.set_live(False)
        self.context.changed()

