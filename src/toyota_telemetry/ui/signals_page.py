"""Signals: review and set how each DID is collected, verify, and set signal targets.

One endpoint -> DID -> signal table with standard row selection. The panel on
the right edits everything selected at once and shows "mixed" where the
selected rows disagree.
"""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.editing import UNSET
from toyota_telemetry.operator.inventory import EVIDENCE_STATES, FAILED, VERIFIED, InventoryTree
from toyota_telemetry.operator.selection import Selection
from toyota_telemetry.operator.verification import RecipeRequired

from .context import PageContext
from .dialogs import EndpointAddDialog, EndpointConfirmDialog, LiveConfirmDialog, TargetEditDialog
from .jobs import LiveJob
from .names import (
    ENDPOINT_READABLE,
    count,
    did_readable,
    endpoint_address,
    endpoint_label,
    endpoint_name,
    target_summary,
)
from .widgets import (
    LIVE_TIP,
    FilterButton,
    button_row,
    mark_live,
    notice,
    page_title,
    search_icon,
    set_enabled,
    show_notice,
)


ROLE = Qt.ItemDataRole.UserRole
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
COLUMNS = ("Name", "Readable", "Collect", "Every", "Late after", "Priority", "Target")
COLUMN_TIPS = (
    "ECU (endpoint) → DID (one request) → signal. An endpoint is the diagnostic route to an ECU; "
    "a DID can carry several signals.",
    "Whether the vehicle answers. DID: Readable, Not checked, or Refused; a signal is readable when "
    "its DID is. Endpoint: how many DIDs are readable once confirmed; otherwise Needs confirming, "
    "No answer, Not checked, or No address.",
    "Whether the DID is read during collection",
    "How often the DID is read; shared by all its signals",
    "A read counts as late after this long",
    "0-100; higher wins when reads compete",
    "What each signal must collect; ✓ when already met. A DID shows how many of its signals have one.",
)
NUMERIC = {3, 4, 5}
FILTER_GROUPS = {
    "Readable": [
        ("readable", "Readable", "DIDs that answered when verified"),
        ("unchecked", "Not checked", "DIDs not read from the vehicle yet"),
        ("refused", "Refused", "DIDs the ECU refused"),
    ],
    "Collect": [
        ("collected", "Collected", "DIDs read during collection"),
        ("not-collected", "Not collected", "DIDs not read during collection"),
    ],
    "Target": [
        ("targeted", "Has target", "Signals with a target"),
        ("untargeted", "No target", "Signals without a target"),
        ("unmet", "Not met", "Signals whose target is not met yet"),
    ],
}
DEFAULTS = {"target_interval_ms": 1000, "maximum_interval_ms": 5000, "priority": 50, "included": False}


def duration_text(milliseconds: int | None) -> str:
    if milliseconds is None:
        return ""
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    seconds = milliseconds / 1000
    if seconds < 60:
        return f"{seconds:g} s"
    return f"{seconds / 60:g} min"


class MixedSpinBox(QSpinBox):
    """A spin box that can show "mixed" and reports whether the user changed it."""

    def __init__(self, minimum: int, maximum: int, suffix: str = "") -> None:
        super().__init__()
        self._minimum = minimum
        self.setRange(minimum, maximum)
        self.setSuffix(suffix)
        self.setAccelerated(True)
        self.setSpecialValueText("mixed")
        self.touched = False
        self._loading = False
        self.valueChanged.connect(self._changed)

    def load(self, values: set[int]) -> None:
        self._loading = True
        if len(values) == 1:
            self.setMinimum(self._minimum)
            self.setValue(next(iter(values)))
        else:
            self.setMinimum(self._minimum - 1)
            self.setValue(self._minimum - 1)
        self.touched = False
        self._loading = False

    @property
    def mixed(self) -> bool:
        return self.value() < self._minimum

    def _changed(self, _value: int) -> None:
        if not self._loading:
            self.touched = not self.mixed


class SignalFilter(QSortFilterProxyModel):
    """Shows only rows whose identity is in ``visible`` (None shows everything)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.visible: set[tuple[str, str]] | None = None
        self.setSortRole(SORT_ROLE)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802 - Qt API
        if self.visible is None:
            return True
        index = self.sourceModel().index(row, 0, parent)
        return index.data(ROLE) in self.visible

    def refilter(self, visible: set[tuple[str, str]] | None) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+
            self.beginFilterChange()
            self.visible = visible
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.visible = visible
            self.invalidateFilter()


class SignalsPage(QWidget):
    dirty_changed = Signal(bool)

    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self.tree_data: InventoryTree | None = None
        # Lookups by identity, rebuilt with each load so a large selection stays linear.
        self._dids: dict[str, tuple[Any, Any]] = {}
        self._signals: dict[str, tuple[Any, Any, Any]] = {}
        self.draft_review: dict[str, Any] | None = None
        self.draft_policy: dict[str, Any] | None = None
        self._base_bytes: dict[str, bytes] | None = None
        self._restore: dict[str, Any] | None = None
        self.job: LiveJob | None = None
        self.confirm_live = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted
        self.run_dialog = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted

        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            "Signals",
            "Plan collection: choose which DIDs are read and how often, and what each signal must "
            "collect. Verify first to see what the vehicle answers. Select rows (Shift or Ctrl for "
            "several); the panel on the right edits all of them at once. Progress is on Collect.",
        ))
        layout.addLayout(self._filter_bar())
        self.external_notice = notice()
        layout.addWidget(self.external_notice)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._table_panel())
        splitter.addWidget(self._selection_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([900, 380])
        layout.addWidget(splitter, 1)
        self.job_log = QPlainTextEdit()
        self.job_log.setReadOnly(True)
        self.job_log.setMaximumBlockCount(500)
        self.job_log.setMaximumHeight(120)
        self.job_log.setVisible(False)
        self.job_log.setAccessibleName("Verification progress")
        layout.addWidget(self.job_log)
        layout.addLayout(self._action_bar())
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.apply_filters)
        # Selection changes arrive in bursts (Shift+click, select all); refresh once per burst.
        self._selection_timer = QTimer(self)
        self._selection_timer.setSingleShot(True)
        self._selection_timer.setInterval(0)
        self._selection_timer.timeout.connect(self.show_details)
        context.project_changed.connect(self._project_changed)
        context.data_changed.connect(self.refresh_from_disk)
        self.reload()

    # Layout -----------------------------------------------------------------

    def _filter_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search signal or DID")
        self.search.setToolTip("Show signals whose name or reference contains this text, or DIDs with this ID")
        self.search.setAccessibleName("Search signals")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(search_icon(), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        row.addWidget(self.search, 1)
        self.filter_button = FilterButton()
        for title, options in FILTER_GROUPS.items():
            self.filter_button.set_group(title, options)
        self.filter_button.changed.connect(self._filters_changed)
        row.addWidget(self.filter_button)
        self.clear_filters_button = QPushButton("Clear")
        self.clear_filters_button.setToolTip("Clear the search and every filter")
        self.clear_filters_button.clicked.connect(self.clear_filters)
        row.addWidget(self.clear_filters_button)
        row.addSpacing(12)
        self.expand_button = QPushButton("Expand all")
        self.expand_button.setToolTip("Expand every ECU and DID; Collapse shows ECUs only")
        self.expand_button.clicked.connect(self.toggle_expanded)
        row.addWidget(self.expand_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Reread project files and verification results")
        self.refresh_button.clicked.connect(self.refresh_from_disk)
        row.addWidget(self.refresh_button)
        return row

    def _table_panel(self) -> QWidget:
        self.model = QStandardItemModel(0, len(COLUMNS), self)
        self.model.setHorizontalHeaderLabels(list(COLUMNS))
        for column, tip in enumerate(COLUMN_TIPS):
            self.model.setHeaderData(column, Qt.Orientation.Horizontal, tip, Qt.ItemDataRole.ToolTipRole)
        self.proxy = SignalFilter(self)
        self.proxy.setSourceModel(self.model)
        self.view = QTreeView()
        self.view.setModel(self.proxy)
        self.view.setAccessibleName("Endpoint, DID, and signal table")
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setUniformRowHeights(True)
        self.view.setAlternatingRowColors(True)
        self.view.setStyleSheet("QTreeView::item { padding: 4px 8px; }")
        self.view.header().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.view.setSortingEnabled(True)
        header = self.view.header()
        # Names get the room; the table scrolls sideways rather than squeezing them.
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((340, 130, 70, 75, 80, 65, 200)):
            header.resizeSection(column, width)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._selection_timer.start())
        self.context.register(
            "signals.list", self.view,
            "Each ECU (endpoint) lists its DIDs; a DID is one request and can carry several signals. "
            "Select rows to verify them or change their collection plan.",
        )
        self.empty_label = QLabel("No project open.")
        self.empty_label.setWordWrap(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.view, 1)
        return container

    def _selection_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.selection_title = QLabel("Nothing selected")
        font = self.selection_title.font()
        font.setBold(True)
        self.selection_title.setFont(font)
        layout.addWidget(self.selection_title)
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setAccessibleName("Selected row details")
        self.detail_text.setMaximumHeight(130)
        self.context.register("signals.show", self.detail_text, "Details of the selected rows")
        layout.addWidget(self.detail_text)

        self.collection_group = QGroupBox("Collection")
        self.collection_group.setToolTip(
            "How the selected DIDs are read during collection. Shared by every signal on a DID. "
            "Fields showing \"mixed\" differ between the selected DIDs and are left as they are "
            "unless you change them."
        )
        form = QFormLayout(self.collection_group)
        self.did_included = QCheckBox("Collect")
        self.did_included.setToolTip("Read the selected DIDs during collection")
        self.did_included.clicked.connect(lambda _checked: self._included_clicked())
        self._included_touched = False
        self.did_interval = MixedSpinBox(1, 3_600_000, " ms")
        self.did_interval.setToolTip("Aim to read each DID this often")
        self.did_maximum = MixedSpinBox(1, 3_600_000, " ms")
        self.did_maximum.setToolTip("A read counts as late after this long")
        self.did_priority = MixedSpinBox(0, 100)
        self.did_priority.setToolTip("0-100; higher wins when reads compete")
        for spin in (self.did_interval, self.did_maximum, self.did_priority):
            spin.valueChanged.connect(lambda _value: self._update_apply())
        form.addRow(self.did_included)
        form.addRow("Every", self.did_interval)
        form.addRow("Late after", self.did_maximum)
        form.addRow("Priority", self.did_priority)
        self.did_apply = QPushButton("Apply")
        self.did_apply.clicked.connect(self.apply_did_settings)
        self.context.register(
            "did.set", self.did_apply,
            "Apply the changed fields to every selected DID in the unsaved draft; Save writes it.",
        )
        form.addRow(self.did_apply)
        layout.addWidget(self.collection_group)

        self.targets_group = QGroupBox("Targets")
        self.targets_group.setToolTip("What evidence the selected signals still need; recordings are never removed")
        targets_layout = QVBoxLayout(self.targets_group)
        self.targets_text = QLabel()
        self.targets_text.setWordWrap(True)
        targets_layout.addWidget(self.targets_text)
        self.target_button = QPushButton("Edit targets")
        self.clear_button = QPushButton("Remove targets")
        self.context.register("target.set", self.target_button, "Set completion requirements for the selected signals")
        self.context.register(
            "target.clear", self.clear_button, "Remove targets from the selected signals; recordings are kept"
        )
        self.target_button.clicked.connect(self.edit_targets)
        self.clear_button.clicked.connect(self.clear_targets)
        targets_layout.addLayout(button_row(self.target_button, self.clear_button))
        layout.addWidget(self.targets_group)

        self.definition_group = QGroupBox("Definition")
        self.definition_group.setToolTip("How the selected signal is named and decoded")
        definition = QFormLayout(self.definition_group)
        self.def_name = QLineEdit()
        self.def_name.setToolTip("Display name; empty uses the catalog name")
        self.def_formula = QLineEdit()
        self.def_formula.setToolTip("Decode formula in terms of raw, e.g. raw / 10 - 40; empty uses the catalog formula")
        self.def_unit = QLineEdit()
        self.def_unit.setToolTip("Unit; empty uses the catalog unit")
        self.def_evidence = QComboBox()
        self.def_evidence.addItems(EVIDENCE_STATES)
        self.def_evidence.setToolTip(
            "How well this signal's meaning is established. Candidate: from the database only. "
            "Observed: seen in a capture. Correlated: matches a reference. Validated: checked across its range. "
            "\"and replayed\": read again independently with this toolkit."
        )
        self.def_notes = QLineEdit()
        definition.addRow("Name", self.def_name)
        definition.addRow("Formula", self.def_formula)
        definition.addRow("Unit", self.def_unit)
        definition.addRow("Evidence", self.def_evidence)
        definition.addRow("Notes", self.def_notes)
        self.def_apply = QPushButton("Apply")
        self.def_apply.clicked.connect(self.apply_definition)
        self.context.register("signal.set", self.def_apply, "Apply to the unsaved draft; Save writes it.")
        definition.addRow(self.def_apply)
        layout.addWidget(self.definition_group)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(320)
        return scroll

    def _action_bar(self) -> QHBoxLayout:
        self.add_endpoint_button = QPushButton("Add endpoint")
        self.confirm_button = QPushButton("Confirm endpoint")
        self.verify_button = mark_live(QPushButton("Verify"))
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Stop after the current probe")
        self.stop_button.setVisible(False)
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip("Write the draft to the review and policy files")
        self.discard_button = QPushButton("Discard")
        self.discard_button.setToolTip("Drop the draft and reload the project files")
        for operation, button, tip in (
            ("endpoint.add", self.add_endpoint_button,
             "Add an endpoint candidate by its tester and response CAN IDs"),
            ("endpoint.confirm", self.confirm_button,
             "Record the selected responding endpoint in the transport settings"),
            ("verify", self.verify_button,
             f"Probe the selected endpoints, then read their DIDs. {LIVE_TIP}"),
        ):
            self.context.register(operation, button, tip)
        self.add_endpoint_button.clicked.connect(self.add_endpoint)
        self.confirm_button.clicked.connect(self.confirm_endpoint)
        self.verify_button.clicked.connect(self.verify_checked)
        self.stop_button.clicked.connect(self.stop_job)
        self.save_button.clicked.connect(self.save)
        self.discard_button.clicked.connect(self.discard)
        row = button_row(self.add_endpoint_button, self.confirm_button, self.verify_button, self.stop_button)
        row.addWidget(self.save_button)
        row.addWidget(self.discard_button)
        return row

    # Loading ------------------------------------------------------------------

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
        """Reload the table in the background; the current table stays until then."""

        service = self.context.service
        if self._restore is None:
            self._restore = self._view_state()
        if restore is not None:
            self._restore["selected"] = [restore]
            self._restore["current"] = restore
        if service is None:
            self._show_tree(None)
            return
        if self.model.rowCount() == 0:
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

    def _view_state(self) -> dict[str, Any]:
        return {
            "expanded": {key for key, index in self._proxy_rows() if self.view.isExpanded(index)},
            "selected": self._selected_keys(),
            "current": self._current(),
        }

    def _show_tree(self, data: InventoryTree | None) -> None:
        state = self._restore or {"expanded": set(), "selected": [], "current": None}
        self._restore = None
        self.tree_data = data
        self._dids = {did.key: (endpoint, did) for endpoint in data.endpoints for did in endpoint.dids} if data else {}
        self._signals = {
            signal.signal_ref: (endpoint, did, signal) for endpoint, did in self._dids.values() for signal in did.signals
        }
        self._update_ecu_filter(data)
        self.view.setUpdatesEnabled(False)
        try:
            self.model.removeRows(0, self.model.rowCount())
            if data is not None:
                self._populate(data)
            self.apply_filters(expand=False)
            expanded = state["expanded"] or (
                {("endpoint", endpoint.key) for endpoint in data.endpoints} if data and len(data.endpoints) <= 3 else set()
            )
            for key, index in self._proxy_rows():
                if key in expanded:
                    self.view.setExpanded(index, True)
            self._select_keys(state["selected"], state["current"])
        finally:
            self.view.setUpdatesEnabled(True)
        has_rows = bool(data and data.endpoints)
        self.empty_label.setVisible(not has_rows)
        if self.context.service is None:
            self.empty_label.setText("No project open.")
        elif not has_rows:
            self.empty_label.setText("No candidates.")
            self.empty_label.setToolTip("Import a candidate list in Setup.")
        self.show_details()

    def _populate(self, data: InventoryTree) -> None:
        root = self.model.invisibleRootItem()
        dim = self.palette().color(QPalette.ColorRole.PlaceholderText)
        for endpoint in data.endpoints:
            collected = sum(1 for did in endpoint.dids if did.settings and did.settings["included"])
            targeted = sum(1 for did in endpoint.dids for signal in did.signals if signal.target)
            readable = sum(1 for did in endpoint.dids if did.state == VERIFIED)
            if endpoint.confirmed:
                status, status_tip = f"{readable} of {len(endpoint.dids)}", "DIDs that answered when verified"
            else:
                status, status_tip = ENDPOINT_READABLE.get(endpoint.state, (endpoint.state.capitalize(), ""))
            row = self._row(("endpoint", endpoint.key), [
                endpoint_label(endpoint), status,
                count(collected, "DID") if collected else "", "", "", "",
                count(targeted, "signal") if targeted else "",
            ], sort=[endpoint_name(endpoint).lower(), readable if endpoint.confirmed else -1,
                     collected, None, None, None, targeted])
            row[0].setToolTip(_endpoint_tip(endpoint))
            row[1].setToolTip(status_tip)
            root.appendRow(row)
            for did in endpoint.dids:
                settings = did.settings or {}
                included = bool(settings.get("included"))
                did_targets = sum(1 for signal in did.signals if signal.target)
                did_row = self._row(("did", did.key), [
                    f"DID {did.data_id}" + (f" · {len(did.signals)} signals" if len(did.signals) > 1 else ""),
                    did_readable(did.state), "Yes" if included else "No",
                    duration_text(settings.get("target_interval_ms")),
                    duration_text(settings.get("maximum_interval_ms")),
                    str(settings["priority"]) if settings else "",
                    f"{did_targets} of {len(did.signals)}" if did_targets else "",
                ], sort=[did.data_id, {VERIFIED: 2, FAILED: 0}.get(did.state, 1), int(included),
                         settings.get("target_interval_ms", -1), settings.get("maximum_interval_ms", -1),
                         settings.get("priority", -1), did_targets])
                did_row[1].setToolTip(did.detail or did.state)
                if not included:
                    # Settings of DIDs that are not read stay visible but recede.
                    for item in did_row[2:6]:
                        item.setForeground(dim)
                    if settings:
                        did_row[2].setToolTip("Has collection settings but is not read")
                row[0].appendRow(did_row)
                for signal in did.signals:
                    unit = f"  {signal.unit}" if signal.unit else ""
                    conflict = "  (definitions disagree)" if signal.conflict else ""
                    met = bool(signal.progress and signal.progress["complete"])
                    target = target_summary(signal.target)
                    signal_row = self._row(("signal", signal.signal_ref), [
                        f"{signal.name}{unit}{conflict}", "", "", "", "", "",
                        f"{target}  ✓" if target and met else target,
                    ])
                    signal_row[0].setToolTip(signal.signal_ref)
                    if target:
                        signal_row[6].setToolTip(target + (". Already met." if met else ""))
                    did_row[0].appendRow(signal_row)

    @staticmethod
    def _row(identity: tuple[str, str], texts: list[str], sort: list[Any] | None = None) -> list[QStandardItem]:
        items = []
        for column, text in enumerate(texts):
            item = QStandardItem(text)
            value = sort[column] if sort and sort[column] is not None else text.lower()
            item.setData(value, SORT_ROLE)
            if column in NUMERIC:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items.append(item)
        items[0].setData(identity, ROLE)
        return items

    # Filters ----------------------------------------------------------------

    def apply_filters(self, *, expand: bool = True) -> None:
        data = self.tree_data
        if data is None:
            self.proxy.refilter(None)
            return
        search = self.search.text().strip().lower()
        button = self.filter_button
        ecus, readable, collect, targets = (
            button.active("ECU"), button.active("Readable"), button.active("Collect"), button.active("Target"),
        )
        self.clear_filters_button.setEnabled(bool(search or button.count()))
        signal_level = bool(search) or bool(targets)
        if not (search or button.count()):
            self.proxy.refilter(None)
            self._selection_timer.start()
            return
        visible: set[tuple[str, str]] = set()
        for endpoint in data.endpoints:
            if ecus and f"ecu:{endpoint.key}" not in ecus:
                continue
            for did in endpoint.dids:
                if readable and {VERIFIED: "readable", FAILED: "refused"}.get(did.state, "unchecked") not in readable:
                    continue
                included = bool(did.settings and did.settings["included"])
                if collect and ("collected" if included else "not-collected") not in collect:
                    continue
                did_match = bool(search) and search in did.data_id.lower()
                signals = [
                    signal for signal in did.signals
                    if (not targets or _target_state(signal) & targets)
                    and (not search or did_match or search in signal.name.lower() or search in signal.signal_ref.lower())
                ]
                if signal_level and not signals and not (did_match and not targets):
                    continue
                visible.add(("endpoint", endpoint.key))
                visible.add(("did", did.key))
                visible.update(("signal", signal.signal_ref) for signal in signals)
        self.proxy.refilter(visible)
        if expand:
            for key, index in self._proxy_rows():
                if key[0] == "endpoint" or (key[0] == "did" and search):
                    self.view.setExpanded(index, True)
        self._selection_timer.start()

    def _filters_changed(self) -> None:
        self.apply_filters()

    def clear_filters(self) -> None:
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.filter_button.clear()

    def toggle_expanded(self) -> None:
        if self.expand_button.text() == "Expand all":
            self.view.expandAll()
            self.expand_button.setText("Collapse")
        else:
            self.view.collapseAll()
            self.expand_button.setText("Expand all")

    def _update_ecu_filter(self, data: InventoryTree | None) -> None:
        endpoints = sorted(data.endpoints if data else [], key=lambda row: endpoint_name(row).lower())
        self.filter_button.set_group("ECU", [
            (f"ecu:{endpoint.key}", endpoint_label(endpoint), _endpoint_tip(endpoint)) for endpoint in endpoints
        ])

    # Selection --------------------------------------------------------------

    def _proxy_rows(self, parent: QModelIndex = QModelIndex()):
        """(identity, proxy index) for every visible row, depth first."""

        for row in range(self.proxy.rowCount(parent)):
            index = self.proxy.index(row, 0, parent)
            yield index.data(ROLE), index
            yield from self._proxy_rows(index)

    def _selected_indexes(self) -> list[QModelIndex]:
        return self.view.selectionModel().selectedRows(0)

    def _selected_keys(self) -> list[tuple[str, str]]:
        return [index.data(ROLE) for index in self._selected_indexes()]

    def selection(self) -> Selection:
        """The DIDs and signals the selected rows stand for (visible rows only)."""

        selection = Selection()
        if self.tree_data is None:
            return selection
        dids: dict[str, None] = {}
        signals: dict[str, None] = {}

        def add_did(index: QModelIndex) -> None:
            dids[index.data(ROLE)[1]] = None
            for row in range(self.proxy.rowCount(index)):
                signals[self.proxy.index(row, 0, index).data(ROLE)[1]] = None

        for index in self._selected_indexes():
            kind, key = index.data(ROLE)
            if kind == "endpoint":
                if self.tree_data.endpoint(key).tester_can_id is None:
                    continue
                for row in range(self.proxy.rowCount(index)):
                    add_did(self.proxy.index(row, 0, index))
            elif kind == "did":
                add_did(index)
            else:
                dids[index.parent().data(ROLE)[1]] = None
                signals[key] = None
        selection.dids = [key for key in dids if self._dids[key][0].tester_can_id is not None]
        selection.signals = [key for key in signals if self._signals[key][0].tester_can_id is not None]
        return selection

    checked_selection = selection

    def select(self, kind: str, key: str, *, add: bool = False) -> None:
        """Select a row by stable identity, revealing it (automation and tests)."""

        for identity, index in self._proxy_rows():
            if identity == (kind, key):
                parent = index.parent()
                while parent.isValid():
                    self.view.setExpanded(parent, True)
                    parent = parent.parent()
                flags = self.view.selectionModel().SelectionFlag.Rows
                flags |= self.view.selectionModel().SelectionFlag.Select if add else \
                    self.view.selectionModel().SelectionFlag.ClearAndSelect
                self.view.selectionModel().select(index, flags)
                self.view.selectionModel().setCurrentIndex(index, self.view.selectionModel().SelectionFlag.NoUpdate)
                self.view.scrollTo(index)
                self._selection_timer.stop()
                self.show_details()
                return
        raise KeyError(key)

    def select_row(self, kind: str, key: str) -> None:
        self.select(kind, key)

    def focus(self, kind: str, key: str) -> None:
        """Select and reveal a row, now or as soon as the table has loaded."""

        try:
            self.select(kind, key)
        except KeyError:
            self._restore = {"expanded": set(), "selected": [(kind, key)], "current": (kind, key)}

    def _select_keys(self, keys: Iterable[tuple[str, str]], current: tuple[str, str] | None) -> None:
        wanted = set(keys)
        if not wanted:
            return
        model = self.view.selectionModel()
        for identity, index in self._proxy_rows():
            if identity in wanted:
                model.select(index, model.SelectionFlag.Select | model.SelectionFlag.Rows)
            if identity == current:
                model.setCurrentIndex(index, model.SelectionFlag.NoUpdate)
                self.view.scrollTo(index)

    def _current(self) -> tuple[str, str] | None:
        selected = self._selected_indexes()
        if len(selected) == 1:
            return selected[0].data(ROLE)
        index = self.view.selectionModel().currentIndex()
        return index.siblingAtColumn(0).data(ROLE) if index.isValid() and selected else None

    # Panel ----------------------------------------------------------------

    def show_details(self) -> None:
        data = self.tree_data
        keys = self._selected_keys()
        selection = self.selection()
        kinds = [kind for kind, _ in keys]
        if not keys or data is None:
            self.selection_title.setText("Nothing selected")
            self.detail_text.setPlainText("Select rows to see and change their settings.")
        elif len(keys) == 1:
            kind, key = keys[0]
            if kind == "endpoint":
                title = endpoint_name(data.endpoint(key))
            elif kind == "did":
                title = f"DID {self._dids[key][1].data_id}"
            else:
                title = self._signals[key][2].name
            self.selection_title.setText(title)
            self.detail_text.setPlainText("\n".join(self._detail_lines(kind, key)))
        else:
            parts = [f"{kinds.count(kind)} {name}" for kind, name in
                     (("endpoint", "endpoints"), ("did", "DIDs"), ("signal", "signals")) if kind in kinds]
            self.selection_title.setText(", ".join(parts) + " selected")
            self.detail_text.setPlainText(
                f"{len(selection.dids)} DID(s) and {len(selection.signals)} signal(s) in the selection."
            )
        self._load_collection(selection)
        self._load_targets(selection)
        single_signal = len(keys) == 1 and kinds[0] == "signal"
        self.definition_group.setVisible(single_signal)
        if single_signal:
            endpoint, _did, signal = self._signals[keys[0][1]]
            self._load_definition(endpoint, signal)
        self.update_actions()

    def _detail_lines(self, kind: str, key: str) -> list[str]:
        data = self.tree_data
        if kind == "endpoint":
            endpoint = data.endpoint(key)
            state = "Confirmed" if endpoint.confirmed else ENDPOINT_READABLE.get(endpoint.state, (endpoint.state,))[0]
            return [
                f"{endpoint_name(endpoint)}: {state}",
                f"Address: {endpoint_address(endpoint) or 'unknown'}",
                f"ECU keys: {', '.join(map(str, endpoint.ecu_keys)) or '-'}",
                f"Diagnostic session: {endpoint.session_recipe_id or 'chosen when verified'}",
                f"Endpoint ID: {endpoint.endpoint_id or endpoint.key}",
                f"DIDs: {len(endpoint.dids)}",
            ]
        if kind == "did":
            endpoint, did = self._dids[key]
            return [
                f"{did_readable(did.state)[2:]}" + (f": {did.detail}" if did.detail else ""),
                f"On: {endpoint_label(endpoint)}",
                f"Last answer: {did.baseline_hex or '-'}",
                f"Latency: {did.latency_ms if did.latency_ms is not None else '-'} ms at {did.observed_utc or '-'}",
                f"Signals: {len(did.signals)}",
            ]
        endpoint, did, signal = self._signals[key]
        lines = [
            f"Reference: {signal.signal_ref}",
            f"Unit: {signal.unit or '-'}   Formula: {signal.formula}",
            f"DID {did.data_id} on {endpoint_name(endpoint)}: {did_readable(did.state)[2:]}",
            f"Evidence: {_status_tip(signal, did)}",
        ]
        if signal.conflict:
            lines.append(f"{signal.variants} catalog variants disagree; review the definition.")
        if signal.review and signal.review.get("notes"):
            lines.append(f"Notes: {signal.review['notes']}")
        return lines

    def _selected_dids(self, selection: Selection):
        return [self._dids[key] for key in selection.dids]

    def _load_collection(self, selection: Selection) -> None:
        found = self._selected_dids(selection)
        settings = [{**DEFAULTS, **(did.settings or {})} for _endpoint, did in found]
        self._included_touched = False
        included = {row["included"] for row in settings}
        self.did_included.setTristate(len(included) > 1)
        self.did_included.setCheckState(
            Qt.CheckState.PartiallyChecked if len(included) > 1
            else Qt.CheckState.Checked if included == {True} else Qt.CheckState.Unchecked
        )
        for spin, name in ((self.did_interval, "target_interval_ms"), (self.did_maximum, "maximum_interval_ms"),
                           (self.did_priority, "priority")):
            spin.load({row[name] for row in settings} or {DEFAULTS[name]})
        unconfirmed = next((endpoint for endpoint, _did in found if not endpoint.confirmed), None)
        enabled = bool(found) and unconfirmed is None
        reason = "Select DIDs first." if not found else f"Confirm endpoint {unconfirmed.key} first." if unconfirmed else ""
        for widget in (self.did_included, self.did_interval, self.did_maximum, self.did_priority):
            set_enabled(widget, enabled, reason)
        title = "Collection" if len(found) <= 1 else f"Collection: {len(found)} DIDs"
        self.collection_group.setTitle(title)
        self._update_apply()

    def _included_clicked(self) -> None:
        self.did_included.setTristate(False)
        self._included_touched = True
        self._update_apply()

    def _update_apply(self) -> None:
        touched = self._included_touched or any(
            spin.touched for spin in (self.did_interval, self.did_maximum, self.did_priority)
        )
        enabled = self.did_interval.isEnabled()
        set_enabled(self.did_apply, enabled and touched,
                    self.did_interval.toolTip() if not enabled else "Change a field first.")

    def _load_targets(self, selection: Selection) -> None:
        found = [self._signals[ref][2] for ref in selection.signals]
        targeted = [signal for signal in found if signal.target]
        met = sum(1 for signal in targeted if signal.progress and signal.progress["complete"])
        if not found:
            text = "Select signals, DIDs, or endpoints."
        elif len(found) == 1:
            signal = found[0]
            text = f"Target: {target_summary(signal.target) or 'none'}"
            if signal.target and met:
                text += "\nAlready met."
        else:
            summaries = {target_summary(signal.target) for signal in targeted}
            text = f"{len(found)} signals: {len(targeted)} with targets" + (f", {met} already met." if met else ".")
            if len(summaries) == 1 and len(targeted) == len(found):
                text += f"\nAll: {next(iter(summaries))}"
            elif targeted:
                text += "\nTargets are mixed."
        self.targets_text.setText(text)

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

    # Actions ----------------------------------------------------------------

    def update_actions(self) -> None:
        has_project = self.context.service is not None
        busy = self.job is not None and self.job.running
        selection = self.selection() if has_project else Selection()
        current = self._current()
        responded = False
        if current and current[0] == "endpoint" and self.tree_data is not None:
            responded = self.tree_data.endpoint(current[1]).state == "responded"
        blocked = "Open a project first." if not has_project else "Wait for verification to finish." if busy else ""
        ready = has_project and not busy
        set_enabled(self.add_endpoint_button, ready, blocked)
        set_enabled(self.confirm_button, ready and responded,
                    blocked or "Select an endpoint that responded during verification.")
        set_enabled(self.verify_button, ready and bool(selection.dids),
                    blocked or "Select DIDs, or endpoints with a known address, first.")
        set_enabled(self.target_button, ready and bool(selection.signals), blocked or "Select signals first.")
        set_enabled(self.clear_button, ready and bool(selection.signals), blocked or "Select signals first.")
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
        self.context.message(f"Confirmed {endpoint_label(endpoint)}.")
        self.context.changed()

    def verify_checked(self) -> None:
        service = self.context.service
        if service is None or self.tree_data is None:
            return
        selection = self.selection()
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
        selection = self.selection()
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
        selection = self.selection()
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
        selection = self.selection()
        if self.context.service is None or not selection.dids:
            return
        fields: dict[str, Any] = {}
        for spin, name in ((self.did_interval, "target_interval_ms"), (self.did_maximum, "maximum_interval_ms"),
                           (self.did_priority, "priority")):
            fields[name] = spin.value() if spin.touched else UNSET
        if self._included_touched:
            fields["included"] = self.did_included.checkState() == Qt.CheckState.Checked
        try:
            result = self.context.service.edit_dids(
                self.tree_data, selection.dids, selected_signals=selection.signals,
                policy=self.draft_policy, **fields,
            )
        except ValueError as exc:
            self.context.error("Collection settings not applied", exc)
            return
        siblings = result.impact()["unselected_siblings"]
        if siblings:
            self.context.message(f"Settings also apply to {len(siblings)} other signal(s) on the same DIDs.")
        self._set_draft(policy=result.policy)

    def _set_draft(self, *, review=None, policy=None) -> None:
        if self._base_bytes is None and self.context.service is not None:
            self._base_bytes = self.context.service.authority_bytes()
        if review is not None:
            self.draft_review = review
        if policy is not None:
            self.draft_policy = policy
        self.dirty_changed.emit(True)
        self.rebuild()

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
        self.context.message("Saved review and collection settings.")
        self.context.changed()
        return True

    def discard(self) -> None:
        self.reload()

    def prompt_unsaved(self) -> bool:
        """Return True when it is fine to continue (saved or discarded)."""

        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save changes to signal review and collection settings?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        if answer == QMessageBox.StandardButton.Discard:
            self.discard()
            return True
        return False


def _target_state(signal) -> set[str]:
    """The Target filter options a signal matches."""

    if not signal.target:
        return {"untargeted"}
    return {"targeted"} if signal.progress and signal.progress["complete"] else {"targeted", "unmet"}


def _endpoint_tip(endpoint) -> str:
    lines = [endpoint_label(endpoint), f"Endpoint ID: {endpoint.endpoint_id or endpoint.key}"]
    if endpoint.session_recipe_id:
        lines.append(f"Diagnostic session: {endpoint.session_recipe_id}")
    if endpoint.tester_can_id is None:
        lines.append("No CAN address is known for this ECU. Add an endpoint for it to verify it.")
    return "\n".join(lines)


def _impact_text(impact: dict[str, Any], selected: int) -> str:
    text = f"{selected} signal(s) on {len(impact['dids'])} DID(s) will change."
    created = len(impact["created_did_settings"])
    if created:
        text += f" {created} DID(s) will start being read every 1 s (change this under Collection)."
    return text


def _status_tip(signal, did) -> str:
    if signal.status == "Reviewed":
        return f"reviewed as {signal.review['evidence_state']}"
    if signal.status == "Recorded":
        return f"{signal.observations} samples recorded across drives; meaning not reviewed yet"
    if signal.status == "Refused":
        return "none; its DID was refused"
    return "nothing recorded yet"
