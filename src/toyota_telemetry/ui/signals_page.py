"""Signals: the one endpoint -> DID -> signal tree for verify, review, and targets."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.editing import REQUIREMENT_LABELS, format_condition, format_duration
from toyota_telemetry.operator.inventory import EVIDENCE_STATES, FAILED, VERIFIED, InventoryTree
from toyota_telemetry.operator.selection import Selection
from toyota_telemetry.operator.verification import RecipeRequired

from .context import PageContext
from .dialogs import EndpointAddDialog, EndpointConfirmDialog, LiveConfirmDialog, TargetEditDialog
from .jobs import LiveJob
from .widgets import LIVE_TIP, button_row, mark_live, notice, page_title, set_enabled, show_notice


ROLE = Qt.ItemDataRole.UserRole
STATE_FILTERS = {
    "All": None,
    "Verified": "verified",
    "Not verified": "not-verified",
    "Failed": "failed",
    "Has a target": "targeted",
    "No target": "untargeted",
}


class SignalsPage(QWidget):
    dirty_changed = Signal(bool)

    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self.tree_data: InventoryTree | None = None
        self.draft_review: dict[str, Any] | None = None
        self.draft_policy: dict[str, Any] | None = None
        self._base_bytes: dict[str, bytes] | None = None
        self._checked: set[tuple[str, str]] = set()
        self._building = False
        self.job: LiveJob | None = None
        self.confirm_live = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted
        self.run_dialog = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted

        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            "Signals",
            "Endpoints are diagnostic routes to an ECU; a DID is one request on an endpoint and can "
            "carry several signals. Check rows, then verify them with the vehicle or set what "
            "evidence each signal still needs.",
        ))
        layout.addLayout(self._filter_bar())
        self.external_notice = notice()
        layout.addWidget(self.external_notice)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._tree_panel())
        splitter.addWidget(self._detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        layout.addLayout(self._action_bar())
        self.job_log = QPlainTextEdit()
        self.job_log.setReadOnly(True)
        self.job_log.setMaximumBlockCount(500)
        self.job_log.setMaximumHeight(120)
        self.job_log.setVisible(False)
        self.job_log.setAccessibleName("Verification progress")
        layout.addWidget(self.job_log)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.apply_filters)
        context.project_changed.connect(self._project_changed)
        context.data_changed.connect(self.refresh_from_disk)
        self.reload()

    # Layout -----------------------------------------------------------------

    def _filter_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setToolTip("Filter by signal name or reference")
        self.search.setAccessibleName("Search signals")
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        self.ecu_filter = QLineEdit()
        self.ecu_filter.setPlaceholderText("ECU key")
        self.ecu_filter.setMaximumWidth(100)
        self.ecu_filter.textChanged.connect(lambda _text: self._search_timer.start())
        self.state_filter = QComboBox()
        self.state_filter.addItems(list(STATE_FILTERS))
        self.state_filter.setAccessibleName("Filter by state")
        self.state_filter.currentIndexChanged.connect(self.apply_filters)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Reread project files and verification results")
        self.refresh_button.clicked.connect(self.refresh_from_disk)
        row.addWidget(self.search, 1)
        row.addWidget(QLabel("ECU"))
        row.addWidget(self.ecu_filter)
        row.addWidget(self.state_filter)
        row.addWidget(self.refresh_button)
        return row

    def _tree_panel(self) -> QWidget:
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Endpoint / DID / Signal", "Status", "Interval", "Target", "Progress"])
        header_item = self.tree.headerItem()
        header_item.setToolTip(1, (
            "Endpoint: Confirmed, Responded, Silent, Not verified, or Address unknown.\n"
            "DID: Verified, Not verified, Failed, or Endpoint not verified.\n"
            "Signal: Not checked \u2192 Responds (its DID answered) \u2192 Recorded (samples saved) "
            "\u2192 Reviewed (meaning confirmed by review); Refused if its DID was refused."
        ))
        header_item.setToolTip(2, "How often each DID is read during collection; shared by its signals")
        header_item.setToolTip(3, "What each signal still needs to be complete")
        header_item.setToolTip(4, "How much of its target each signal has reached")
        self.tree.setAccessibleName("Endpoint, DID, and signal tree")
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.currentItemChanged.connect(lambda *_: self.show_details())
        self.context.register(
            "signals.list", self.tree,
            "Endpoints are diagnostic routes to an ECU. A DID is one request on an endpoint and can "
            "carry several signals. Check rows to verify them or edit their targets.",
        )
        self.empty_label = QLabel("No project open.")
        self.empty_label.setWordWrap(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.tree, 1)
        return container

    def _detail_panel(self) -> QWidget:
        self.details = QTabWidget()
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setAccessibleName("Selected row details")
        self.context.register("signals.show", self.detail_text, "Details of the selected row")
        self.details.addTab(self.detail_text, "Details")

        definition = QWidget()
        form = QFormLayout(definition)
        self.def_name = QLineEdit()
        self.def_formula = QLineEdit()
        self.def_formula.setPlaceholderText("e.g. raw / 10 - 40")
        self.def_unit = QLineEdit()
        self.def_evidence = QComboBox()
        self.def_evidence.addItems(EVIDENCE_STATES)
        self.def_evidence.setToolTip(
            "How well this signal's meaning is established. Candidate: from the database only. "
            "Observed: seen in a capture. Correlated: matches a reference. Validated: checked across its range. "
            "\"and replayed\": read again independently with this toolkit."
        )
        self.def_notes = QLineEdit()
        form.addRow("Display name", self.def_name)
        form.addRow("Formula", self.def_formula)
        form.addRow("Unit", self.def_unit)
        form.addRow("Evidence state", self.def_evidence)
        form.addRow("Notes", self.def_notes)
        self.def_formula.setToolTip("Decode formula in terms of raw; empty uses the catalog formula")
        self.def_name.setToolTip("Display name; empty uses the catalog name")
        self.def_unit.setToolTip("Unit; empty uses the catalog unit")
        self.def_apply = QPushButton("Apply")
        self.def_apply.clicked.connect(self.apply_definition)
        self.context.register("signal.set", self.def_apply, "Apply to the unsaved draft; Save writes it.")
        form.addRow(self.def_apply)
        self.details.addTab(definition, "Definition")

        settings = QWidget()
        settings_form = QFormLayout(settings)
        settings.setToolTip(
            "Shared by all signals read from this endpoint and DID; changing them affects every "
            "sibling signal."
        )
        self.did_interval = QSpinBox()
        self.did_interval.setRange(1, 3_600_000)
        self.did_interval.setSuffix(" ms")
        self.did_maximum = QSpinBox()
        self.did_maximum.setRange(1, 3_600_000)
        self.did_maximum.setSuffix(" ms")
        self.did_priority = QSpinBox()
        self.did_priority.setRange(0, 100)
        self.did_included = QCheckBox("Included")
        self.did_included.setToolTip("Include this DID in collection")
        self.did_interval.setToolTip("Aim to read this DID this often")
        self.did_maximum.setToolTip("Reading is overdue after this long")
        self.did_priority.setToolTip("0-100; higher wins when reads compete")
        settings_form.addRow("Target interval", self.did_interval)
        settings_form.addRow("Maximum interval", self.did_maximum)
        settings_form.addRow("Priority", self.did_priority)
        settings_form.addRow(self.did_included)
        self.did_apply = QPushButton("Apply")
        self.did_apply.clicked.connect(self.apply_did_settings)
        self.context.register(
            "did.set", self.did_apply, "Apply to the unsaved draft for every signal on this DID; Save writes it."
        )
        settings_form.addRow(self.did_apply)
        self.details.addTab(settings, "DID settings")
        return self.details

    def _action_bar(self) -> QHBoxLayout:
        self.add_endpoint_button = QPushButton("Add endpoint")
        self.confirm_button = QPushButton("Confirm endpoint")
        self.verify_button = mark_live(QPushButton("Verify"))
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Stop after the current probe")
        self.stop_button.setVisible(False)
        self.target_button = QPushButton("Edit targets")
        self.clear_button = QPushButton("Remove targets")
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip("Write the draft to the review and policy files")
        self.discard_button = QPushButton("Discard")
        self.discard_button.setToolTip("Drop the draft and reload the project files")
        for operation, button, tip in (
            ("endpoint.add", self.add_endpoint_button,
             "Add an endpoint candidate by its tester and response CAN IDs"),
            ("endpoint.confirm", self.confirm_button,
             "Record a responding endpoint in config/transport.json"),
            ("verify", self.verify_button,
             f"Probe the checked endpoints, then read their DIDs. {LIVE_TIP}"),
            ("target.set", self.target_button, "Set completion requirements for the checked signals"),
            ("target.clear", self.clear_button, "Remove targets from the checked signals; recordings are kept"),
        ):
            self.context.register(operation, button, tip)
        self.add_endpoint_button.clicked.connect(self.add_endpoint)
        self.confirm_button.clicked.connect(self.confirm_endpoint)
        self.verify_button.clicked.connect(self.verify_checked)
        self.stop_button.clicked.connect(self.stop_job)
        self.target_button.clicked.connect(self.edit_targets)
        self.clear_button.clicked.connect(self.clear_targets)
        self.save_button.clicked.connect(self.save)
        self.discard_button.clicked.connect(self.discard)
        row = button_row(
            self.add_endpoint_button, self.confirm_button, self.verify_button, self.stop_button,
            self.target_button, self.clear_button,
        )
        row.addWidget(self.save_button)
        row.addWidget(self.discard_button)
        return row

    # Tree -------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self.draft_review is not None or self.draft_policy is not None

    def _project_changed(self) -> None:
        self._restore = None
        self._show_tree(None)
        self.reload()

    def reload(self) -> None:
        self.draft_review = None
        self.draft_policy = None
        self._base_bytes = None
        self._checked.clear()
        show_notice(self.external_notice, "")
        self.dirty_changed.emit(False)
        self.rebuild()

    def refresh_from_disk(self) -> None:
        if self.context.service is None:
            return
        if self.dirty and self._base_bytes is not None:
            try:
                changed = self.context.service.authority_bytes() != self._base_bytes
            except (OSError, ValueError):
                changed = True
            if changed:
                show_notice(self.external_notice, "Project files changed outside this window.")
                self.external_notice.setToolTip(
                    "Your unsaved edits are based on older files. Discard reloads them; Save is refused until you do."
                )
        self.rebuild()

    def rebuild(self, restore: tuple[str, str] | None = None) -> None:
        """Reload the tree in the background; the current tree stays until then."""

        service = self.context.service
        self._restore = restore or self._current() or getattr(self, "_restore", None)
        if service is None:
            self._show_tree(None)
            return
        if self.tree.topLevelItemCount() == 0:
            self.empty_label.setText("Loading…")
            self.empty_label.setVisible(True)
        review, policy = self.draft_review, self.draft_policy

        def failed(detail: str) -> None:
            self.empty_label.setText(f"Cannot read the project: {detail.split(chr(10) * 2, 1)[0]}")
            self.empty_label.setVisible(True)

        self.context.run(
            "signals.tree", lambda service: service.tree(review=review, policy=policy),
            self._show_tree, failed,
        )

    def _show_tree(self, data: InventoryTree | None) -> None:
        expanded = {item.data(0, ROLE) for item in self._items() if item.isExpanded()}
        restore = getattr(self, "_restore", None)
        self._building = True
        self.tree.clear()
        self._building = False
        self.tree_data = data
        if data is not None:
            self._populate(data, expanded if expanded else None)
        has_rows = bool(data and data.endpoints)
        self.empty_label.setVisible(not has_rows)
        if self.context.service is None:
            self.empty_label.setText("No project open.")
        elif not has_rows:
            self.empty_label.setText("No candidates.")
            self.empty_label.setToolTip("Import a candidate list in Setup.")
        self.apply_filters()
        if restore is not None:
            try:
                self.select_row(*restore)
            except KeyError:
                pass
        self._restore = None
        self.update_actions()
        self.show_details()

    def _populate(self, data: InventoryTree, expanded: set[tuple[str, str]] | None = None) -> None:
        self._building = True
        try:
            for endpoint in data.endpoints:
                reachable = endpoint.tester_can_id is not None
                address = f"{endpoint.tester_can_id}/{endpoint.response_can_id}" if reachable else "address unknown"
                names = ", ".join(endpoint.ecu_names)
                label = f"{endpoint.key}  {address}  ECU {', '.join(map(str, endpoint.ecu_keys))}"
                parent = QTreeWidgetItem([label + (f"  {names}" if names else ""), endpoint.state.capitalize(), "", "", ""])
                parent.setData(0, ROLE, ("endpoint", endpoint.key))
                unreachable_tip = "No CAN address is known for this ECU. Add an endpoint for it to verify it."
                if not reachable:
                    parent.setToolTip(0, unreachable_tip)
                self._checkable(parent, tristate=True, enabled=reachable)
                self.tree.addTopLevelItem(parent)
                for did in endpoint.dids:
                    cadence = ""
                    if did.settings:
                        cadence = f"every {did.settings['target_interval_ms']} ms"
                        if not did.settings["included"]:
                            cadence += " (excluded)"
                    did_item = QTreeWidgetItem([f"DID {did.data_id}", did.state, cadence, "", ""])
                    did_item.setData(0, ROLE, ("did", did.key))
                    did_item.setToolTip(1, did.detail or did.state)
                    if did.settings:
                        did_item.setToolTip(2, "Shared by all signals read from this endpoint and DID")
                    if not reachable:
                        did_item.setToolTip(0, unreachable_tip)
                    self._checkable(did_item, tristate=bool(did.signals), enabled=reachable)
                    parent.addChild(did_item)
                    for signal in did.signals:
                        unit = f" [{signal.unit}]" if signal.unit else ""
                        conflict = "  (definitions disagree)" if signal.conflict else ""
                        item = QTreeWidgetItem([
                            f"{signal.name}{unit}{conflict}", signal.status, "",
                            _target_summary(signal.target), _progress_summary(signal.progress),
                        ])
                        item.setData(0, ROLE, ("signal", signal.signal_ref))
                        item.setToolTip(0, signal.signal_ref)
                        item.setToolTip(1, _status_tip(signal, did))
                        self._checkable(item, tristate=False, enabled=reachable)
                        did_item.addChild(item)
                    if not did.signals and reachable:
                        did_item.setCheckState(
                            0, Qt.CheckState.Checked if ("did", did.key) in self._checked else Qt.CheckState.Unchecked
                        )
                    if expanded is not None and ("did", did.key) in expanded:
                        did_item.setExpanded(True)
                parent.setExpanded(
                    ("endpoint", endpoint.key) in expanded if expanded is not None else len(data.endpoints) <= 3
                )
        finally:
            self._building = False

    def _checkable(self, item: QTreeWidgetItem, *, tristate: bool, enabled: bool = True) -> None:
        if not enabled:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            return
        flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
        if tristate:
            flags |= Qt.ItemFlag.ItemIsAutoTristate
        item.setFlags(flags)
        kind, key = item.data(0, ROLE)
        if not tristate:
            state = Qt.CheckState.Checked if (kind, key) in self._checked else Qt.CheckState.Unchecked
            item.setCheckState(0, state)

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._building or column != 0:
            return
        kind, key = item.data(0, ROLE)
        if kind == "signal" or (kind == "did" and item.childCount() == 0):
            if item.checkState(0) == Qt.CheckState.Checked:
                self._checked.add((kind, key))
            else:
                self._checked.discard((kind, key))
        self.update_actions()

    def apply_filters(self) -> None:
        if self.tree_data is None or self.context.service is None:
            return
        state = STATE_FILTERS[self.state_filter.currentText()]
        try:
            ecus = [int(self.ecu_filter.text())] if self.ecu_filter.text().strip() else []
        except ValueError:
            ecus = []
        search = self.search.text().strip() or None
        filtered = bool(state or ecus or search)
        selection = (
            self.context.service.select(
                self.tree_data, ecus=ecus, states=[state] if state else [], search=search, require=False
            )
            if filtered else None
        )
        dids = set(selection.dids) if selection else set()
        signals = set(selection.signals) if selection else set()
        self._building = True
        try:
            for index in range(self.tree.topLevelItemCount()):
                endpoint_item = self.tree.topLevelItem(index)
                visible_dids = 0
                for did_index in range(endpoint_item.childCount()):
                    did_item = endpoint_item.child(did_index)
                    did_visible = not filtered or did_item.data(0, ROLE)[1] in dids
                    visible_signals = 0
                    for signal_index in range(did_item.childCount()):
                        signal_item = did_item.child(signal_index)
                        shown = did_visible and (not filtered or signal_item.data(0, ROLE)[1] in signals)
                        signal_item.setHidden(not shown)
                        if not shown:
                            self._untick(signal_item)
                        visible_signals += shown
                    did_item.setHidden(not did_visible)
                    if not did_visible and did_item.childCount() == 0:
                        self._untick(did_item)
                    visible_dids += did_visible
                    if filtered and did_visible:
                        endpoint_item.setExpanded(True)
                        if search and visible_signals:
                            did_item.setExpanded(True)
                endpoint_item.setHidden(filtered and not visible_dids)
        finally:
            self._building = False
        self.update_actions()

    def _untick(self, item: QTreeWidgetItem) -> None:
        """Rows hidden by a filter never stay ticked, so actions match what is shown."""

        if item.flags() & Qt.ItemFlag.ItemIsUserCheckable and item.checkState(0) != Qt.CheckState.Unchecked:
            item.setCheckState(0, Qt.CheckState.Unchecked)
            self._checked.discard(item.data(0, ROLE))

    # Selection --------------------------------------------------------------

    def checked_selection(self) -> Selection:
        """Checked rows, or the current row when nothing is checked."""

        selection = Selection()
        if self.tree_data is None:
            return selection
        for index in range(self.tree.topLevelItemCount()):
            endpoint_item = self.tree.topLevelItem(index)
            for did_index in range(endpoint_item.childCount()):
                did_item = endpoint_item.child(did_index)
                if did_item.isHidden() or did_item.checkState(0) == Qt.CheckState.Unchecked:
                    continue
                selection.dids.append(did_item.data(0, ROLE)[1])
                for signal_index in range(did_item.childCount()):
                    signal_item = did_item.child(signal_index)
                    if not signal_item.isHidden() and signal_item.checkState(0) == Qt.CheckState.Checked:
                        selection.signals.append(signal_item.data(0, ROLE)[1])
        if selection.empty:
            current = self.tree.currentItem()
            if current is not None:
                kind, key = current.data(0, ROLE)
                if kind == "signal":
                    selection.signals.append(key)
                    selection.dids.append(current.parent().data(0, ROLE)[1])
                elif kind == "did":
                    selection.dids.append(key)
                    selection.signals.extend(
                        current.child(i).data(0, ROLE)[1] for i in range(current.childCount())
                    )
                else:
                    for did_index in range(current.childCount()):
                        child = current.child(did_index)
                        selection.dids.append(child.data(0, ROLE)[1])
                        selection.signals.extend(
                            child.child(i).data(0, ROLE)[1] for i in range(child.childCount())
                        )
        return selection

    def focus(self, kind: str, key: str) -> None:
        """Select and reveal a row, now or as soon as the tree has loaded."""

        try:
            self.select_row(kind, key)
        except KeyError:
            self._restore = (kind, key)

    def check(self, kind: str, key: str, checked: bool = True) -> None:
        """Check a row by stable identity (keyboard-free automation and tests)."""

        for item in self._items():
            if item.data(0, ROLE) == (kind, key):
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                return
        raise KeyError(key)

    def select_row(self, kind: str, key: str) -> None:
        for item in self._items():
            if item.data(0, ROLE) == (kind, key):
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return
        raise KeyError(key)

    def _items(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _current(self) -> tuple[str, str] | None:
        item = self.tree.currentItem()
        return item.data(0, ROLE) if item is not None else None

    # Details ----------------------------------------------------------------

    def show_details(self) -> None:
        current = self._current()
        data = self.tree_data
        set_enabled(self.def_apply, False, "Select a signal on a confirmed endpoint.")
        set_enabled(self.did_apply, False, "Select a DID on a confirmed endpoint.")
        if current is None or data is None:
            self.detail_text.setPlainText("")
            self.update_actions()
            return
        kind, key = current
        lines: list[str] = []
        if kind == "endpoint":
            endpoint = data.endpoint(key)
            lines = [
                f"Endpoint {endpoint.key}: {endpoint.state}",
                f"Tester/response CAN IDs: {endpoint.tester_can_id or '?'} / {endpoint.response_can_id or '?'}",
                f"ECU keys: {', '.join(map(str, endpoint.ecu_keys))}  {', '.join(endpoint.ecu_names)}",
                f"Session recipe: {endpoint.session_recipe_id or 'chosen when verified'}",
                f"DIDs: {len(endpoint.dids)}",
            ]
        elif kind == "did":
            endpoint, did = data.find_did(key)
            lines = [
                f"DID {did.key}: {did.state}" + (f" ({did.detail})" if did.detail else ""),
                f"Baseline response: {did.baseline_hex or '-'}",
                f"Latency: {did.latency_ms if did.latency_ms is not None else '-'} ms at {did.observed_utc or '-'}",
                f"Signals: {len(did.signals)}",
            ]
            self._load_did_settings(endpoint, did)
        else:
            endpoint, did, signal = data.find_signal(key)
            lines = [
                f"{signal.name} ({signal.signal_ref})",
                f"Unit: {signal.unit or '-'}   Formula: {signal.formula}",
                f"DID {did.key}: {did.state}",
            ]
            if signal.conflict:
                lines.append(f"{signal.variants} catalog variants disagree; review the definition.")
            if signal.review:
                lines.append(f"Review: {signal.review['evidence_state']}. {signal.review['notes']}".rstrip())
            lines.append(f"Target: {_target_summary(signal.target) or 'none'}")
            if signal.progress:
                counters = signal.progress["counters"]
                lines.append(
                    f"Recorded: {counters['samples']} samples, {counters['runs']} runs, "
                    f"{counters['changes']} changes, {counters['transitions']} transitions, "
                    f"states {counters['states'][:12]}"
                )
                missing = [
                    REQUIREMENT_LABELS[name] for name, met in signal.progress["requirements_met"].items() if not met
                ]
                lines.append("Remaining: " + (", ".join(missing) if missing else "complete"))
            self._load_definition(endpoint, signal)
            self._load_did_settings(endpoint, did)
        self.detail_text.setPlainText("\n".join(lines))
        self.update_actions()

    def _load_definition(self, endpoint, signal) -> None:
        review = signal.review or {}
        self.def_name.setText(review.get("display_name_override") or "")
        self.def_name.setPlaceholderText(signal.name)
        self.def_formula.setText(review.get("formula_override") or "")
        self.def_formula.setPlaceholderText(signal.formula)
        self.def_unit.setText(review.get("unit_override") or "")
        self.def_unit.setPlaceholderText(signal.unit)
        state = review.get("evidence_state") or "Candidate"
        if self.def_evidence.findText(state) < 0:
            self.def_evidence.addItem(state)
        self.def_evidence.setCurrentText(state)
        self.def_notes.setText(review.get("notes") or "")
        set_enabled(self.def_apply, endpoint.confirmed, "Confirm the endpoint first.")

    def _load_did_settings(self, endpoint, did) -> None:
        settings = did.settings or {"target_interval_ms": 1000, "maximum_interval_ms": 5000, "priority": 50, "included": True}
        self.did_interval.setValue(settings["target_interval_ms"])
        self.did_maximum.setValue(settings["maximum_interval_ms"])
        self.did_priority.setValue(settings["priority"])
        self.did_included.setChecked(settings["included"])
        set_enabled(self.did_apply, endpoint.confirmed, "Confirm the endpoint first.")

    # Actions ----------------------------------------------------------------

    def update_actions(self) -> None:
        has_project = self.context.service is not None
        busy = self.job is not None and self.job.running
        selection = self.checked_selection() if has_project else Selection()
        current = self._current()
        responded = False
        if current and current[0] == "endpoint" and self.tree_data is not None:
            responded = self.tree_data.endpoint(current[1]).state == "responded"
        blocked = "Open a project first." if not has_project else "Wait for verification to finish." if busy else ""
        ready = has_project and not busy
        set_enabled(self.add_endpoint_button, ready, blocked)
        set_enabled(self.confirm_button, ready and responded,
                    blocked or "Select an endpoint that responded during verification.")
        set_enabled(self.verify_button, ready and bool(selection.dids), blocked or "Check DIDs or signals first.")
        set_enabled(self.target_button, ready and bool(selection.signals), blocked or "Check signals first.")
        set_enabled(self.clear_button, ready and bool(selection.signals), blocked or "Check signals first.")
        set_enabled(self.save_button, self.dirty and not busy, blocked or "No unsaved changes.")
        set_enabled(self.discard_button, self.dirty and not busy, blocked or "No unsaved changes.")

    def add_endpoint(self) -> None:
        service = self.context.service
        if service is None:
            return
        recipes = [row["id"] for row in service.repository().transport["session_recipes"]]
        dialog = EndpointAddDialog(recipes, self)
        if not self.run_dialog(dialog):
            return
        try:
            service.add_endpoint(*dialog.values())
        except (OSError, ValueError) as exc:
            self.context.error("Endpoint not added", exc)
            return
        self.rebuild()

    def confirm_endpoint(self) -> None:
        current = self._current()
        if self.context.service is None or current is None or current[0] != "endpoint":
            return
        endpoint = self.tree_data.endpoint(current[1])
        dialog = EndpointConfirmDialog(endpoint.endpoint_id, endpoint.ecu_keys, self)
        if not self.run_dialog(dialog):
            return
        try:
            ecu_keys, sources = dialog.values()
            self.context.service.confirm_endpoint(endpoint.endpoint_id, ecu_keys, sources)
        except (OSError, ValueError) as exc:
            self.context.error("Endpoint not confirmed", exc)
            return
        self.context.message(f"Confirmed {endpoint.endpoint_id} in config/transport.json.")
        self.context.changed()

    def verify_checked(self) -> None:
        service = self.context.service
        if service is None or self.tree_data is None:
            return
        selection = self.checked_selection()
        try:
            scope = service.verification_scope(self.tree_data, selection)
        except RecipeRequired as exc:
            recipe, ok = QInputDialog.getItem(
                self, "Diagnostic session",
                f"{exc.endpoint_key} has not been verified yet. Choose which reviewed diagnostic "
                "session to open when probing it (from the project's transport settings):",
                exc.recipes, 0, False,
            )
            if not ok:
                return
            try:
                scope = service.verification_scope(self.tree_data, selection, session_recipe_id=recipe)
            except ValueError as inner:
                self.context.error("Cannot verify", inner)
                return
        except ValueError as exc:
            self.context.error("Cannot verify", exc)
            return
        details = []
        for row in scope.endpoints:
            action = "probe endpoint first, then read" if row.probe_endpoint else "read"
            details.append(f"{row.key} ({row.tester_can_id}/{row.response_can_id}, session {row.session_recipe_id}): {action}")
            details.append("    " + ", ".join(row.dids))
        details.append(f"{scope.probe_count} probe(s), at most {scope.upper_bound_seconds:.0f} s. Scope {scope.digest[:12]}.")
        dialog = LiveConfirmDialog("Verify", scope.summary() + "?", details, self)
        if not self.confirm_live(dialog):
            return
        self.job_log.clear()
        self.job_log.setVisible(True)
        self.job_log.appendPlainText(scope.summary())

        def work(emit, stop):
            return service.verify(scope, on_event=emit, stop_event=stop)

        self._start_job(work)

    def _start_job(self, work) -> None:
        self.job = LiveJob(work, self)
        self.job.event.connect(self._job_event)
        self.job.finished.connect(self._job_finished)
        self.job.failed.connect(self._job_failed)
        self.context.set_live(True)
        self.stop_button.setVisible(True)
        self.job.start()
        self.update_actions()

    def _job_event(self, event: dict) -> None:
        if event["event"] == "endpoint":
            self.job_log.appendPlainText(
                f"Endpoint {event['key']}: {'responded' if event['responded'] else 'no response'}"
            )
        else:
            detail = f" {event['detail']}" if event.get("detail") else ""
            self.job_log.appendPlainText(f"{event['key']}: {event['outcome']}{detail}")

    def _job_finished(self, result) -> None:
        summary = result.as_dict()
        outcomes = ", ".join(f"{count} {name}" for name, count in sorted(summary["outcomes"].items()))
        self.job_log.appendPlainText(("Stopped early: " if result.stopped else "Finished: ") + (outcomes or "no DIDs read"))
        self._job_done()

    def _job_failed(self, detail: str) -> None:
        self.job_log.appendPlainText(f"Failed: {detail.splitlines()[0]}")
        self._job_done()
        self.context.error("Verification failed", detail.splitlines()[0])

    def _job_done(self) -> None:
        self.stop_button.setVisible(False)
        self.context.set_live(False)
        self.job = None
        self.context.changed()

    def stop_job(self) -> None:
        if self.job is not None:
            self.job.request_stop()
            self.job_log.appendPlainText("Stopping after the current probe…")

    def edit_targets(self) -> None:
        service = self.context.service
        selection = self.checked_selection()
        if service is None or self.tree_data is None or not selection.signals:
            return
        current = None
        if len(selection.signals) == 1:
            found = self.tree_data.find_signal(selection.signals[0])
            current = found[2].target if found else None

        def preview(fields: dict[str, Any]) -> str:
            result = service.edit_targets(self.tree_data, selection.signals, policy=self.draft_policy, **fields)
            return _impact_text(result.impact(), len(selection.signals))

        dialog = TargetEditDialog(len(selection.signals), current, preview, self)
        if not self.run_dialog(dialog):
            return
        try:
            result = service.edit_targets(
                self.tree_data, selection.signals, policy=self.draft_policy, **dialog.fields()
            )
        except ValueError as exc:
            self.context.error("Targets not changed", exc)
            return
        self._set_draft(policy=result.policy)

    def clear_targets(self) -> None:
        service = self.context.service
        selection = self.checked_selection()
        if service is None or not selection.signals:
            return
        result = service.clear_targets(selection.signals, policy=self.draft_policy)
        self._set_draft(policy=result.policy)

    def apply_definition(self) -> None:
        current = self._current()
        if self.context.service is None or current is None or current[0] != "signal":
            return
        try:
            result = self.context.service.edit_signals(
                self.tree_data, [current[1]], review=self.draft_review,
                name=self.def_name.text(), formula=self.def_formula.text(), unit=self.def_unit.text(),
                evidence_state=self.def_evidence.currentText(), notes=self.def_notes.text(),
            )
        except ValueError as exc:
            self.context.error("Definition not applied", exc)
            return
        self._set_draft(review=result.review)

    def apply_did_settings(self) -> None:
        current = self._current()
        if self.context.service is None or current is None:
            return
        kind, key = current
        if kind == "signal":
            did_key = self.tree.currentItem().parent().data(0, ROLE)[1]
            selected = [key]
        elif kind == "did":
            did_key, selected = key, []
        else:
            return
        try:
            result = self.context.service.edit_dids(
                self.tree_data, [did_key], selected_signals=selected, policy=self.draft_policy,
                target_interval_ms=self.did_interval.value(),
                maximum_interval_ms=self.did_maximum.value(),
                priority=self.did_priority.value(),
                included=self.did_included.isChecked(),
            )
        except ValueError as exc:
            self.context.error("DID settings not applied", exc)
            return
        siblings = result.impact()["unselected_siblings"]
        if siblings:
            self.context.message(f"DID settings also apply to {len(siblings)} other signal(s) on this DID.")
        self._set_draft(policy=result.policy)

    def _set_draft(self, *, review=None, policy=None) -> None:
        if self._base_bytes is None and self.context.service is not None:
            self._base_bytes = self.context.service.authority_bytes()
        if review is not None:
            self.draft_review = review
        if policy is not None:
            self.draft_policy = policy
        self.dirty_changed.emit(True)
        self.rebuild(self._current())

    def save(self) -> bool:
        service = self.context.service
        if service is None or not self.dirty:
            return True
        if self._base_bytes is not None and service.authority_bytes() != self._base_bytes:
            show_notice(self.external_notice, "Not saved: project files changed outside this window.")
            self.external_notice.setToolTip("Discard to reload the files, then make your edits again.")
            return False
        try:
            service.save(review=self.draft_review, policy=self.draft_policy)
        except (OSError, ValueError) as exc:
            self.context.error("Not saved", exc)
            return False
        self.reload()
        self.context.message("Saved review and collection targets.")
        self.context.changed()
        return True

    def discard(self) -> None:
        self.reload()

    def prompt_unsaved(self) -> bool:
        """Return True when it is fine to continue (saved or discarded)."""

        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save changes to signal review and collection targets?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        if answer == QMessageBox.StandardButton.Discard:
            self.discard()
            return True
        return False


def _target_summary(target: dict[str, Any] | None) -> str:
    if target is None:
        return ""
    parts = []
    for name, value in target["requirements"].items():
        label = REQUIREMENT_LABELS[name].lower().replace("minimum ", "")
        if name.endswith("_seconds"):
            value = format_duration(value)
        elif isinstance(value, list):
            value = ",".join(map(str, value))
        parts.append(f"{label} {value}")
    if target.get("condition"):
        parts.append(f"when {format_condition(target['condition'])}")
    text = "; ".join(parts)
    return text if target["included"] else f"{text} (excluded)"


def _progress_summary(progress: dict[str, Any] | None) -> str:
    if progress is None:
        return ""
    if progress["complete"]:
        return "Complete"
    met = progress["requirements_met"]
    return f"{sum(met.values())}/{len(met)} met, {progress['counters']['samples']} samples"


def _impact_text(impact: dict[str, Any], selected: int) -> str:
    text = f"{selected} signal(s) on {len(impact['dids'])} DID(s) will change."
    created = len(impact["created_did_settings"])
    if created:
        text += f" {created} DID(s) will start being read every 1 s (change this under DID settings)."
    return text

def _status_tip(signal, did) -> str:
    if signal.status == "Reviewed":
        return f"Reviewed: {signal.review['evidence_state']}"
    if signal.status == "Recorded":
        return f"Recorded: {signal.observations} samples saved across drives"
    if signal.status == "Responds":
        return "Its DID answered during verification; nothing recorded yet"
    if signal.status == "Refused":
        return f"Its DID was refused: {did.detail}"
    return "Its DID has not been checked against the vehicle yet"
