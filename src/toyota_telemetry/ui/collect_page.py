"""Collect: automatic readiness, exact-scope confirmation, target progress, safe stop."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, QTimer
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

from toyota_telemetry.i18n import _
from toyota_telemetry.operator.collection import SLICES, STRATEGIES, CollectionReadiness, Rotation
from toyota_telemetry.operator.inventory import InventoryTree

from .context import PageContext
from .dialogs import LiveConfirmDialog
from .jobs import LiveJob
from .names import count, endpoint_label, still_needed, target_summary
from .widgets import LIVE_TIP, Disclosure, button_row, mark_live, notice, page_title, set_enabled, show_notice

ENDPOINT = Qt.ItemDataRole.UserRole
SLICE_MINUTES = (2, 5, 10, 15)
# Ticking, dragging, or choosing settles for this long before one check runs.
SETTLE_MS = 700
DEFAULT_SLICE_MINUTES = 5


def _ids(session: dict) -> str:
    """Request/response IDs, and the address byte of an ECU behind a gateway."""

    text = f"{session['tester_can_id']}/{session['response_can_id']}"
    if session.get("target_address"):
        return _("{ids} address {address}").format(ids=text, address=session["target_address"])
    return text


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
            _("Collect"),
            _("Run collection; target progress"),
        ))
        self.unsaved_notice = notice()
        layout.addWidget(self.unsaved_notice)

        readiness_group = QGroupBox(_("Readiness"))
        readiness_layout = QVBoxLayout(readiness_group)
        controls = QHBoxLayout()
        # ECUs to collect: checked ones are collected, in the list's order.
        self.ecus = QListWidget()
        self.ecus.setAccessibleName(_("ECUs to collect"))
        self.ecus.setToolTip(_("Check ECUs to collect; drag to order"))
        self.ecus.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.ecus.setMaximumHeight(140)
        # Changes wait until the user settles, then one check runs; until its
        # result arrives, Start stays off.
        self._pending = False
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self.check_readiness)
        self.ecus.itemChanged.connect(self._ecus_changed)
        self.ecus.model().rowsMoved.connect(lambda *_: self._ecus_changed())
        self._order: list[str] = []
        self._checked: set[str] = set()
        self._known: set[str] = set()
        self.strategy = QComboBox()
        self.strategy.setAccessibleName(_("How to move between ECUs"))
        self.strategy.setToolTip(_("How to move between checked ECUs"))
        for key, label in STRATEGIES.items():
            self.strategy.addItem(_(label), key)
        self.strategy.activated.connect(lambda _index: self._choices_changed())
        self.slice = QComboBox()
        self.slice.setAccessibleName(_("Time on each ECU"))
        self.slice.setToolTip(_("Time on each ECU before the next"))
        for minutes in SLICE_MINUTES:
            self.slice.addItem(_("{minutes} min").format(minutes=minutes), minutes * 60)
        self.slice.setCurrentIndex(SLICE_MINUTES.index(DEFAULT_SLICE_MINUTES))
        self.slice.activated.connect(lambda _index: self._choices_changed())
        self.check_button = QPushButton(_("Check"))
        self.context.register("collect.readiness", self.check_button, _("Recheck; nothing is transmitted"))
        self.check_button.clicked.connect(self.check_readiness)
        controls.addWidget(self.ecus, 1)
        self.turns = QFormLayout()
        self.turns.addRow(_("Several ECUs"), self.strategy)
        self.turns.addRow(_("Switch every"), self.slice)
        self.turns.addRow(self.check_button)
        controls.addLayout(self.turns)
        readiness_layout.addLayout(controls)
        self._show_turn_options(0)
        self.readiness_text = notice(_("No project open."))
        readiness_layout.addWidget(self.readiness_text)
        self.problem_area = QVBoxLayout()
        readiness_layout.addLayout(self.problem_area)
        self.scope_text = QPlainTextEdit()
        self.scope_text.setReadOnly(True)
        self.scope_text.setAccessibleName(_("Technical collection scope"))
        self.scope_toggle = Disclosure(_("Scope"), self.scope_text)
        self.scope_toggle.setToolTip(_("The exact compiled scope"))
        readiness_layout.addWidget(self.scope_toggle)
        readiness_layout.addWidget(self.scope_text)
        layout.addWidget(readiness_group)

        run_group = QGroupBox(_("Run"))
        run_layout = QVBoxLayout(run_group)
        form = QFormLayout()
        self.note = QLineEdit()
        self.note.setPlaceholderText(_("optional"))
        self.note.setToolTip(_("Saved with the run, e.g. highway"))
        form.addRow(_("Note"), self.note)
        run_layout.addLayout(form)
        self.start_button = mark_live(QPushButton(_("Start")))
        self.context.register(
            "collect", self.start_button,
            _("Stops when targets are met\n{live}").format(live=_(LIVE_TIP)),
        )
        self.start_button.clicked.connect(self.start_or_stop)
        run_layout.addLayout(button_row(self.start_button))
        self.run_state = QLabel(_("Idle. Adapter closed."))
        self.progress = QProgressBar()
        # %v and %m are Qt's placeholders for the value and the maximum.
        self.progress.setFormat(_("%v of %m targets complete"))
        self.progress.setVisible(False)
        self.run_details = QLabel()
        run_layout.addWidget(self.run_state)
        run_layout.addWidget(self.progress)
        run_layout.addWidget(self.run_details)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(300)
        self.events_toggle = Disclosure(_("Read log"), self.events)
        self.events_toggle.setToolTip(_("Every read as it happens"))
        run_layout.addWidget(self.events_toggle)
        run_layout.addWidget(self.events)
        layout.addWidget(run_group)

        self.targets_group = QGroupBox(_("Targets"))
        self.targets_group.setToolTip(_("Collected so far, across all runs"))
        targets_layout = QVBoxLayout(self.targets_group)
        self.targets = QTreeWidget()
        self.targets.setRootIsDecorated(False)
        self.targets.setAlternatingRowColors(True)
        self.targets.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.targets.setAccessibleName(_("Target progress"))
        self.targets.setStyleSheet("QTreeView::item { padding: 3px 6px; }")
        self.targets.setHeaderLabels([_("Signal"), _("ECU"), _("Target"), _("Recorded"), _("Still needs")])
        for column, tip in enumerate((
            _("Signal with a target"), _("Where it is read"), _("What it must collect"),
            _("Collected so far across valid runs"), _("Requirements not met yet"),
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

    def set_unsaved(self, dirty: bool) -> None:
        """Say that this page shows saved settings while Signals holds a draft."""

        show_notice(
            self.unsaved_notice,
            _("Signals has unsaved changes. This page uses the saved settings until you save them.")
            if dirty else "",
        )

    def check_readiness(self) -> None:
        """Recompute readiness in the background; nothing is transmitted."""

        if self.running:
            return
        self._settle.stop()
        self._stale = False
        if self.context.service is None:
            self._show_readiness(None)
            return
        chosen = [endpoint_id for endpoint_id in self._order if endpoint_id in self._checked]
        first = not self._known
        self.check_button.setEnabled(False)
        self.readiness_text.setText(_("Checking…"))

        def failed(detail: str) -> None:
            self._pending = False
            self.check_button.setEnabled(True)
            self.readiness = None
            self._clear_problems()
            self.readiness_text.setText(
                _("Cannot check readiness: {detail}").format(detail=detail.split(chr(10) * 2, 1)[0])
            )
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
        self._pending = False
        self.check_button.setEnabled(self.context.service is not None)
        self._clear_problems()
        self.readiness = readiness
        if readiness is None:
            self.readiness_text.setText(_("No project open."))
            self._show_targets(None)
            self._update_start()
            return
        self._show_targets(getattr(self, "_tree", None))
        self._fill_ecus(readiness)
        rotating = readiness.rotation is not None
        if self._none_checked:
            self.readiness_text.setText(_("Check at least one ECU to collect."))
            self.scope_text.setPlainText("")
            self._update_start()
            return
        if readiness.ready:
            summary = readiness.summary
            load = summary["estimated_bus_utilization"]
            where = (
                _("{count} ECUs {turns}").format(
                    count=summary["endpoint_count"], turns=self._turns_text(readiness.rotation),
                )
                if rotating else self._label(readiness.endpoint_id)
            )
            values = {
                "where": where, "targets": summary["unfinished_targets"], "dids": summary["dids"],
                "limit": summary["capture_tree_max_mib"],
            }
            if load is None:
                text = _("Ready: {where}, {targets} target(s), {dids} DID(s), storage limit {limit} MiB.")
            else:
                values.update(load=f"{load * 100:.2f}", rate=f"{summary['request_rate_hz']:.1f}")
                text = (
                    _("Ready: {where}, {targets} target(s), {dids} DID(s), bus load {load}%, "
                      "up to {rate} reads/s, storage limit {limit} MiB.")
                    if rotating else
                    _("Ready: {where}, {targets} target(s), {dids} DID(s), bus load {load}%, "
                      "{rate} reads/s, storage limit {limit} MiB.")
                )
            self.readiness_text.setText(text.format(**values))
            self.scope_text.setPlainText(json.dumps(readiness.scope, indent=2, sort_keys=True))
        elif readiness.complete and not readiness.problems:
            self.readiness_text.setText(_("✓ All targets met. Nothing left to collect."))
            self.scope_text.setPlainText("")
        else:
            self.readiness_text.setText(_("Not ready:"))
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
            item = QListWidgetItem(
                _("{ecu}  ({targets})").format(ecu=self._label(endpoint_id), targets=count(targets, "target"))
                if targets else self._label(endpoint_id)
            )
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
        self._choices_changed()

    def _choices_changed(self) -> None:
        """Show the matching options now; check once the user settles."""

        self._show_turn_options(len(self._checked))
        self._pending = True
        self.readiness_text.setText(_("Checking…"))
        self._clear_problems()
        self._update_start()
        self._settle.start()

    def _show_turn_options(self, checked: int) -> None:
        several = checked > 1
        self.turns.setRowVisible(self.strategy, several)
        self.turns.setRowVisible(self.slice, several and self.strategy.currentData() == SLICES)

    def _turns_text(self, rotation: dict) -> str:
        if rotation["strategy"] == SLICES:
            return _("in turn, {minutes} min each").format(minutes=rotation["slice_seconds"] // 60)
        return _("one after another")

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
                _("{samples}, {runs}").format(
                    samples=count(counters["samples"], "sample"), runs=count(counters["runs"], "run"),
                )
                if counters.get("samples") else _("Nothing yet")
            )
            item = QTreeWidgetItem([
                signal.name, endpoint_label(endpoint), target_summary(signal.target, signal.states), recorded,
                _("✓ Met") if complete else still_needed(missing, signal.target, counters, signal.states),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, signal.signal_ref)
            self.targets.addTopLevelItem(item)
        self.targets_group.setTitle(
            _("Targets: {met} of {total} met").format(met=met, total=len(rows)) if rows else _("Targets")
        )
        if not rows:
            empty = QTreeWidgetItem([_("No targets yet. Set them on Signals.")])
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
            self.start_button.setText(_("Stop"))
            self.start_button.setIcon(QIcon())
            self.start_button.setToolTip(_("Stops safely and keeps the run"))
            self.start_button.setEnabled(True)
            return
        mark_live(self.start_button)
        self.start_button.setText(_("Start"))
        self.start_button.setToolTip(self.start_button.property("baseToolTip") or "")
        ready = (
            self.readiness is not None and self.readiness.ready
            and not getattr(self, "_none_checked", False) and not self._pending
        )
        busy = self.context.live_job_running
        complete = self.readiness is not None and self.readiness.complete
        set_enabled(
            self.start_button, ready and not busy,
            _("Another live job is running.") if busy
            else _("All targets met.") if complete else _("Readiness has problems."),
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
            _("%v of %m targets on this ECU") if readiness.rotation else _("%v of %m targets complete")
        )
        self.progress.setRange(0, max(1, readiness.summary["unfinished_targets"]))
        self.progress.setValue(0)
        self.run_state.setText(_("LIVE - starting…"))
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
            _("Adapter: {kind} {device}").format(kind=connector["kind"], device=device).rstrip(),
            _("ECUs {turns}, in this order:").format(turns=self._turns_text(readiness.rotation)),
            *["    " + _("{ecu}: {signals}, {dids} DID(s)").format(
                ecu=self._label(row["endpoint"]["endpoint_id"]), signals=count(len(row["targets"]), "signal"),
                dids=len(row["acquisitions"]),
            ) for row in scope["endpoints"]],
            _("One run per ECU and turn; ECUs whose targets are met are skipped."),
            *(_("Each ECU is read until its targets are met: one that cannot be met\n"
                "now keeps the next ECUs waiting until you stop.").split("\n")
              if readiness.rotation["strategy"] != SLICES else []),
            _("Stops when every target is met or you stop it."),
            _("Storage limit: {limit} MiB").format(limit=scope["storage"]["capture_tree_max_mib"]),
            _("Note: {note}").format(note=self.note.text() or "-"),
        ]
        technical = [_("Scope fingerprint: {digest}").format(digest=readiness.digest)]
        for row in scope["endpoints"]:
            session = row["endpoint"]
            technical += [
                _("Endpoint ID: {endpoint} ({ids})").format(endpoint=session["endpoint_id"], ids=_ids(session)),
                "  " + _("Session {recipe}: {requests}").format(
                    recipe=session["session_recipe_id"],
                    requests="; ".join(step["request_hex"] for step in session["session_setup"]),
                ),
                *["  " + _("{acquisition}: request {request}, every {interval} ms").format(
                    acquisition=item["acquisition_id"], request=item["request_hex"],
                    interval=item["target_interval_ms"],
                ) for item in row["acquisitions"]],
            ]
        return LiveConfirmDialog(
            _("Collect"),
            _("LIVE collection of {signals} signal(s) on {ecus} ECUs {turns}?").format(
                signals=len(scope["targets"]), ecus=len(scope["endpoints"]),
                turns=self._turns_text(readiness.rotation),
            ),
            details, self, technical=technical,
        )

    def _endpoint_dialog(self, readiness: CollectionReadiness) -> LiveConfirmDialog:
        scope = readiness.scope
        session = scope["endpoint"]
        connector = scope["connector"]
        device = ", ".join(f"{value}" for value in connector["settings"].values())
        names = getattr(self, "_names", {})
        details = [
            _("Endpoint: {ecu}").format(ecu=self._label(readiness.endpoint_id)),
            _("Adapter: {kind} {device}").format(kind=connector["kind"], device=device).rstrip(),
            _("Signals ({count}):").format(count=len(scope["targets"])),
            *[f"    {names.get(ref, ref)}" for ref in scope["targets"]],
            _("Reads {dids} DID(s); stops when these targets are complete or you stop it.").format(
                dids=len(scope["acquisitions"]),
            ),
            _("Storage limit: {limit} MiB").format(limit=scope["storage"]["capture_tree_max_mib"]),
            _("Note: {note}").format(note=self.note.text() or "-"),
        ]
        technical = [
            _("Endpoint ID: {endpoint} ({ids})").format(endpoint=readiness.endpoint_id, ids=_ids(session)),
            _("Scope fingerprint: {digest}").format(digest=readiness.digest),
            _("Session {recipe}: {requests}").format(
                recipe=session["session_recipe_id"],
                requests="; ".join(step["request_hex"] for step in session["session_setup"]),
            ),
            *[_("{acquisition}: request {request}, every {interval} ms").format(
                acquisition=row["acquisition_id"], request=row["request_hex"], interval=row["target_interval_ms"],
            ) for row in scope["acquisitions"]],
        ]
        return LiveConfirmDialog(
            _("Collect"),
            _("LIVE collection of {signals} signal(s) on {ecu}?").format(
                signals=len(scope["targets"]), ecu=self._label(readiness.endpoint_id),
            ),
            details, self, technical=technical,
        )

    def stop(self) -> None:
        if self.job is not None:
            self.job.request_stop()
            self.run_state.setText(_("LIVE - stopping safely…"))

    def _event(self, event: dict) -> None:
        kind = event["event"]
        if kind == "scope" and getattr(self, "_rotating", False):
            self._current = self._label(event["endpoint_id"])
            self.run_state.setText(_("LIVE - switching to {ecu}…").format(ecu=self._current))
        elif kind == "started":
            current = getattr(self, "_current", "")
            self.run_state.setText(
                _("LIVE - collecting from {ecu}.").format(ecu=current) if current else _("LIVE - collecting.")
            )
        elif kind == "progress":
            self.progress.setRange(0, max(1, event["targets"]))
            self.progress.setValue(event["targets_complete"])
            self.run_details.setText(
                _("Elapsed {seconds} s, {reads} reads, {failed} failed.").format(
                    seconds=event["elapsed_ms"] // 1000, reads=event["exchanges"], failed=event["failed_exchanges"],
                )
            )
        elif kind == "heartbeat":
            self.events.appendPlainText(_("heartbeat {state} {acquisition}").format(
                state=event["state"], acquisition=event["current_acquisition_id"] or "",
            ))
        elif kind == "exchange":
            self.events.appendPlainText(f"{event['acquisition_id']}: {event['outcome']}")
        elif kind == "error":
            self.events.appendPlainText(_("error: {detail}").format(detail=event["detail"]))
        elif kind == "finished":
            self.events.appendPlainText(_("finished: {reason}").format(reason=event["reason"]))

    def _finished(self, reason: str) -> None:
        labels = {
            "plan_complete": _("Completed: every target in this run is complete."),
            "slice_end": _("Turn ended."),
            "operator_stop": _("Stopped safely."),
            "size_limit": _("Stopped at the storage limit; the run was finalized."),
            "transport_lost": _("Transport lost; the valid prefix was finalized."),
        }
        self.run_state.setText(_("{outcome} Adapter closed.").format(
            outcome=labels.get(reason) or _("Finished: {reason}").format(reason=reason),
        ))
        self.note.clear()
        self._done()

    def _failed(self, detail: str) -> None:
        self.run_state.setText(_("Failed: {detail} Adapter closed.").format(detail=detail.splitlines()[0]))
        self._done()

    def _done(self) -> None:
        self.job = None
        self.context.set_live(False)
        self.context.changed()

