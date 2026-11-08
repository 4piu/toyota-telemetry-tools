"""Signals: review and set how each DID is collected, verify, and set signal targets.

One endpoint -> DID -> signal table with standard row selection. The panel on
the right edits everything selected at once and shows "mixed" where the
selected rows disagree.
"""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStyle,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.i18n import N_, _, ngettext
from toyota_telemetry.operator.editing import UNSET
from toyota_telemetry.operator.inventory import (
    ENDPOINT_SILENT, EVIDENCE_STATES, FAILED, VERIFIED, InventoryTree, describe_deletion,
)
from toyota_telemetry.operator.selection import Selection
from toyota_telemetry.operator.verification import progress_text

from .context import PageContext
from .dialogs import EndpointConfirmDialog, LiveConfirmDialog, TargetEditDialog
from .import_dialog import ImportDialog
from .watch_window import WatchWindow
from .jobs import LiveJob
from .names import (
    ENDPOINT_READABLE,
    count,
    did_readable,
    endpoint_address,
    endpoint_label,
    endpoint_name,
    session_label,
    states_lines,
    target_summary,
)
from .widgets import (
    LIVE_TIP,
    FilterButton,
    LoadingOverlay,
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
COLUMNS = (N_("Name"), N_("Readable"), N_("Collect"), N_("Every"), N_("Late after"), N_("Priority"), N_("Target"))
COLUMN_TIPS = (
    N_("ECU → DID (one request) → signal"),
    N_("Whether the vehicle answers"),
    N_("Read during collection"),
    N_("How often the DID is read"),
    N_("Late after this long"),
    N_("0-100; higher goes first"),
    N_("What to collect; ✓ when met"),
)
NUMERIC = {3, 4, 5}
# Group titles and option labels/tips are marked here and translated where shown;
# the option keys are filter identifiers and stay as they are.
FILTER_GROUPS = {
    N_("Readable"): [
        ("readable", N_("Readable"), N_("DIDs that answered when verified")),
        ("unchecked", N_("Not checked"), N_("DIDs not read from the vehicle yet")),
        ("no-answer", N_("No answer"), N_("DIDs whose ECU did not answer")),
        ("refused", N_("Refused"), N_("DIDs the ECU refused")),
    ],
    N_("Collect"): [
        ("collected", N_("Collected"), N_("DIDs read during collection")),
        ("not-collected", N_("Not collected"), N_("DIDs not read during collection")),
    ],
    N_("Target"): [
        ("targeted", N_("Has target"), N_("Signals with a target")),
        ("untargeted", N_("No target"), N_("Signals without a target")),
        ("unmet", N_("Not met"), N_("Signals whose target is not met yet")),
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
        self.setSpecialValueText(_("mixed"))
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
        self._sessions: dict[str, str] = {}
        self.draft_review: dict[str, Any] | None = None
        self.draft_policy: dict[str, Any] | None = None
        self._base_bytes: dict[str, bytes] | None = None
        self._restore: dict[str, Any] | None = None
        self.job: LiveJob | None = None
        self.confirm_live = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted
        self.run_dialog = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted
        self.run_menu = lambda menu, position: menu.exec(position)
        self.confirm_delete = lambda text: QMessageBox.question(
            self, _("Delete signals"), text,
        ) == QMessageBox.StandardButton.Yes
        self.import_dialog = ImportDialog(context, self)
        self.watch_window: WatchWindow | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            _("Signals"),
            _("What to collect, and how often"),
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
        self.job_log.setAccessibleName(_("Verification progress"))
        layout.addWidget(self.job_log)
        self._unsaved: set[str] = set()
        self.job_progress = QProgressBar()
        # %v, %m, and %p are Qt's progress placeholders (value, maximum, percent).
        self.job_progress.setFormat(_("%v of %m probes, %p%"))
        self.job_progress.setToolTip(_("Endpoint probes and DID reads done so far"))
        self.job_progress.setVisible(False)
        layout.addWidget(self.job_progress)
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
        self.search.setPlaceholderText(_("Search signal or DID"))
        self.search.setToolTip(_("Signal name or DID"))
        self.search.setAccessibleName(_("Search signals"))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(search_icon(), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        row.addWidget(self.search, 1)
        self.filter_button = FilterButton()
        # The group title is both shown and used to look the group up, so the
        # translated title is used for both (see apply_filters).
        for title, options in FILTER_GROUPS.items():
            self.filter_button.set_group(_(title), [(key, _(label), _(tip)) for key, label, tip in options])
        self.filter_button.changed.connect(self._filters_changed)
        row.addWidget(self.filter_button)
        row.addSpacing(12)
        self._expanded = False
        self.expand_button = QPushButton(_("Expand all"))
        self.expand_button.setToolTip(_("Show every row"))
        self.expand_button.clicked.connect(self.toggle_expanded)
        row.addWidget(self.expand_button)
        self.refresh_button = QPushButton(_("Refresh"))
        self.refresh_button.setToolTip(_("Reread project files"))
        self.refresh_button.clicked.connect(self.refresh_from_disk)
        row.addWidget(self.refresh_button)
        return row

    def _table_panel(self) -> QWidget:
        self.model = QStandardItemModel(0, len(COLUMNS), self)
        self.model.setHorizontalHeaderLabels([_(name) for name in COLUMNS])
        for column, tip in enumerate(COLUMN_TIPS):
            self.model.setHeaderData(column, Qt.Orientation.Horizontal, _(tip), Qt.ItemDataRole.ToolTipRole)
        self.proxy = SignalFilter(self)
        self.proxy.setSourceModel(self.model)
        self.view = QTreeView()
        self.view.setModel(self.proxy)
        self.view.setAccessibleName(_("Endpoint, DID, and signal table"))
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
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.show_context_menu)
        self.verify_action = QAction(_("Verify"), self)
        self.verify_action.triggered.connect(self.verify_checked)
        self.watch_action = QAction(_("Live view"), self)
        self.context.register("watch", self.watch_action, _("Read this DID at its interval and show values"))
        self.watch_action.triggered.connect(self.watch_current)
        warning = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        for action in (self.verify_action, self.watch_action):
            action.setIcon(warning)  # these transmit, like the Verify button
        self.delete_action = QAction(_("Delete"), self)
        self.context.register("signal.delete", self.delete_action, _("Remove from the candidate list"))
        self.delete_action.triggered.connect(self.delete_selected)
        self.context.register(
            "signals.list", self.view,
            _("Select rows to change their plan"),
        )
        self.loading = LoadingOverlay(self.view)
        self.empty_label = QLabel(_("No project open."))
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
        self.selection_title = QLabel(_("Nothing selected"))
        font = self.selection_title.font()
        font.setBold(True)
        self.selection_title.setFont(font)
        layout.addWidget(self.selection_title)
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setAccessibleName(_("Selected row details"))
        self.detail_text.setMaximumHeight(130)
        self.context.register("signals.show", self.detail_text, _("Details of the selected rows"))
        layout.addWidget(self.detail_text)

        self.collection_group = QGroupBox(_("Collection"))
        self.collection_group.setToolTip(_("\"mixed\" fields stay as they are unless changed"))
        form = QFormLayout(self.collection_group)
        self.did_included = QCheckBox(_("Collect"))
        self.did_included.setToolTip(_("Read during collection"))
        self.did_included.clicked.connect(lambda _checked: self._included_clicked())
        self._included_touched = False
        self.did_interval = MixedSpinBox(1, 3_600_000, " ms")
        self.did_interval.setToolTip(_("How often to read"))
        self.did_maximum = MixedSpinBox(1, 3_600_000, " ms")
        self.did_maximum.setToolTip(_("Late after this long"))
        self.did_priority = MixedSpinBox(0, 100)
        self.did_priority.setToolTip(_("0-100; higher goes first"))
        for spin in (self.did_interval, self.did_maximum, self.did_priority):
            spin.valueChanged.connect(lambda _value: self._update_apply())
        form.addRow(self.did_included)
        form.addRow(_("Every"), self.did_interval)
        form.addRow(_("Late after"), self.did_maximum)
        form.addRow(_("Priority"), self.did_priority)
        self.did_apply = QPushButton(_("Apply"))
        self.did_apply.clicked.connect(self.apply_did_settings)
        self.context.register(
            "did.set", self.did_apply,
            _("Applied to the draft until Save"),
        )
        form.addRow(self.did_apply)
        layout.addWidget(self.collection_group)

        self.targets_group = QGroupBox(_("Targets"))
        self.targets_group.setToolTip(_("What to collect per signal"))
        targets_layout = QVBoxLayout(self.targets_group)
        self.targets_text = QLabel()
        self.targets_text.setWordWrap(True)
        targets_layout.addWidget(self.targets_text)
        self.target_button = QPushButton(_("Edit targets"))
        self.clear_button = QPushButton(_("Remove targets"))
        self.context.register("target.set", self.target_button, _("Set what to collect"))
        self.context.register(
            "target.clear", self.clear_button, _("Recordings are kept")
        )
        self.target_button.clicked.connect(self.edit_targets)
        self.clear_button.clicked.connect(self.clear_targets)
        targets_layout.addLayout(button_row(self.target_button, self.clear_button))
        layout.addWidget(self.targets_group)

        self.definition_group = QGroupBox(_("Definition"))
        self.definition_group.setToolTip(_("Name and decoding"))
        definition = QFormLayout(self.definition_group)
        self.def_name = QLineEdit()
        self.def_name.setToolTip(_("Empty uses the catalog name"))
        self.def_formula = QLineEdit()
        self.def_formula.setToolTip(_("e.g. raw / 10 - 40; empty uses the catalog"))
        self.def_unit = QLineEdit()
        self.def_unit.setToolTip(_("Empty uses the catalog unit"))
        self.def_evidence = QComboBox()
        # Shown translated; the stored evidence state is the item data.
        for state in EVIDENCE_STATES:
            self.def_evidence.addItem(_(state), state)
        self.def_evidence.setToolTip(_("How well its meaning is established"))
        self.def_notes = QLineEdit()
        self.def_states = QLabel()
        self.def_states.setWordWrap(True)
        self.def_states.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.def_states.setToolTip(_("Text the manufacturer tool shows"))
        definition.addRow(_("Name"), self.def_name)
        definition.addRow(_("Formula"), self.def_formula)
        definition.addRow(_("Unit"), self.def_unit)
        definition.addRow(_("States"), self.def_states)
        definition.addRow(_("Evidence"), self.def_evidence)
        definition.addRow(_("Notes"), self.def_notes)
        self.def_apply = QPushButton(_("Apply"))
        self.def_apply.clicked.connect(self.apply_definition)
        self.context.register("signal.set", self.def_apply, _("Applied to the draft until Save"))
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
        self.add_endpoint_button = QPushButton(_("Add candidates"))
        self.add_endpoint_button.setToolTip(_("From a JSON file, the TIS database, or by CAN IDs"))
        self.confirm_button = QPushButton(_("Confirm endpoint"))
        self.verify_button = mark_live(QPushButton(_("Verify")))
        self.stop_button = QPushButton(_("Stop"))
        self.stop_button.setToolTip(_("Stops after the current probe"))
        self.stop_button.setVisible(False)
        self.save_button = QPushButton(_("Save"))
        self.save_button.setToolTip(_("Write your changes"))
        self.discard_button = QPushButton(_("Discard"))
        self.discard_button.setToolTip(_("Drop your changes"))
        for operation, button, tip in (
            ("endpoint.confirm", self.confirm_button,
             _("Record that an answering endpoint is this ECU\nNothing is sent; needed before planning")),
            ("verify", self.verify_button,
             _("Read the selected DIDs once to see which answer") + "\n" + _(LIVE_TIP)),
        ):
            self.context.register(operation, button, tip)
        self.add_endpoint_button.clicked.connect(self.show_import)
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
                show_notice(self.external_notice, _("Project files changed outside this window."))
                self.external_notice.setToolTip(_("Discard reloads them"))
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
        self.loading.start(_("Loading signals…"))
        review, policy = self.draft_review, self.draft_policy

        def failed(detail: str) -> None:
            self.loading.stop()
            self.empty_label.setText(
                _("Cannot read the project: {detail}").format(detail=detail.split(chr(10) * 2, 1)[0])
            )
            self.empty_label.setVisible(True)

        def work(service):
            repository = service.repository()
            sessions = repository.transport["session_recipes"]
            unsaved = unsaved_keys(repository.review, repository.policy, review, policy)
            return (
                service.tree(review=review, policy=policy),
                {row["id"]: session_label(row) for row in sessions},
                unsaved,
            )

        def done(result) -> None:
            self._sessions = result[1]
            self._unsaved = result[2]
            self._show_tree(result[0])

        self.context.run("signals.tree", work, done, failed)

    def _view_state(self) -> dict[str, Any]:
        return {
            "expanded": {key for key, index in self._proxy_rows() if self.view.isExpanded(index)},
            "selected": self._selected_keys(),
            "current": self._current(),
        }

    def _show_tree(self, data: InventoryTree | None) -> None:
        self.loading.stop()
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
            self.empty_label.setText(_("No project open."))
        elif not has_rows:
            self.empty_label.setText(_("No candidates."))
            self.empty_label.setToolTip(_("Import a candidate list in Setup."))
        self.show_details()

    def _populate(self, data: InventoryTree) -> None:
        root = self.model.invisibleRootItem()
        dim = self.palette().color(QPalette.ColorRole.PlaceholderText)
        accent = self.palette().color(QPalette.ColorRole.Link)
        for endpoint in data.endpoints:
            collected = sum(1 for did in endpoint.dids if did.settings and did.settings["included"])
            targeted = sum(1 for did in endpoint.dids for signal in did.signals if signal.target)
            readable = sum(1 for did in endpoint.dids if did.state == VERIFIED)
            if endpoint.confirmed:
                status = _("{done} of {total}").format(done=readable, total=len(endpoint.dids))
                status_tip = _("DIDs that answered when verified")
            else:
                status, status_tip = ENDPOINT_READABLE.get(endpoint.state, (endpoint.state.capitalize(), ""))
                status, status_tip = _shown(status), _shown(status_tip)
            row = self._row(("endpoint", endpoint.key), [
                endpoint_label(endpoint), status,
                count(collected, "DID") if collected else "", "", "", "",
                count(targeted, "signal") if targeted else "",
            ], sort=[endpoint_name(endpoint).lower(), readable if endpoint.confirmed else -1,
                     collected, None, None, None, targeted])
            row[0].setToolTip(_endpoint_tip(endpoint, self._sessions))
            row[1].setToolTip(status_tip)
            root.appendRow(row)
            for did in endpoint.dids:
                settings = did.settings or {}
                included = bool(settings.get("included"))
                did_targets = sum(1 for signal in did.signals if signal.target)
                if len(did.signals) > 1:
                    did_name = _("DID {did} · {signals}").format(
                        did=did.data_id, signals=count(len(did.signals), "signal"),
                    )
                else:
                    did_name = _("DID {did}").format(did=did.data_id)
                did_row = self._row(("did", did.key), [
                    did_name,
                    did_readable(did.state), _("Yes") if included else _("No"),
                    duration_text(settings.get("target_interval_ms")),
                    duration_text(settings.get("maximum_interval_ms")),
                    str(settings["priority"]) if settings else "",
                    _("{done} of {total}").format(done=did_targets, total=len(did.signals)) if did_targets else "",
                ], sort=[did.data_id, {VERIFIED: 2, FAILED: 0}.get(did.state, 1), int(included),
                         settings.get("target_interval_ms", -1), settings.get("maximum_interval_ms", -1),
                         settings.get("priority", -1), did_targets])
                did_row[1].setToolTip(did.detail or _shown(did.state))
                if not included:
                    # Settings of DIDs that are not read stay visible but recede.
                    for item in did_row[2:6]:
                        item.setForeground(dim)
                    if settings:
                        did_row[2].setToolTip(_("Not read during collection"))
                if did.acquisition_id in self._unsaved:
                    mark_unsaved(did_row, accent)
                row[0].appendRow(did_row)
                for signal in did.signals:
                    unit = f"  {signal.unit}" if signal.unit else ""
                    conflict = "  " + _("(definitions disagree)") if signal.conflict else ""
                    met = bool(signal.progress and signal.progress["complete"])
                    target = target_summary(signal.target, signal.states)
                    signal_row = self._row(("signal", signal.signal_ref), [
                        f"{signal.name}{unit}{conflict}", "", "", "", "", "",
                        f"{target}  ✓" if target and met else target,
                    ])
                    if target:
                        signal_row[6].setToolTip(
                            _("{target}. Already met.").format(target=target) if met else target
                        )
                    if signal.states:
                        signal_row[0].setToolTip(_("States") + "\n" + states_lines(signal.states))
                    if signal.signal_ref in self._unsaved:
                        mark_unsaved(signal_row, accent)
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
        # Groups are registered under their translated titles (see _filter_bar).
        ecus, readable, collect, targets = (
            button.active(_("ECU")), button.active(_("Readable")), button.active(_("Collect")),
            button.active(_("Target")),
        )
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
                if readable and {VERIFIED: "readable", FAILED: "refused", ENDPOINT_SILENT: "no-answer"}.get(did.state, "unchecked") not in readable:
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

    def toggle_expanded(self) -> None:
        if not self._expanded:
            self.view.expandAll()
            self.expand_button.setText(_("Collapse"))
        else:
            self.view.collapseAll()
            self.expand_button.setText(_("Expand all"))
        self._expanded = not self._expanded

    def _update_ecu_filter(self, data: InventoryTree | None) -> None:
        endpoints = sorted(data.endpoints if data else [], key=lambda row: endpoint_name(row).lower())
        self.filter_button.set_group(_("ECU"), [
            (f"ecu:{endpoint.key}", endpoint_label(endpoint), _endpoint_tip(endpoint, self._sessions))
            for endpoint in endpoints
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
        kinds = [kind for kind, _key in keys]
        if not keys or data is None:
            self.selection_title.setText(_("Nothing selected"))
            self.detail_text.setPlainText(_("Select rows to see and change their settings."))
        elif len(keys) == 1:
            kind, key = keys[0]
            if kind == "endpoint":
                title = endpoint_name(data.endpoint(key))
            elif kind == "did":
                title = _("DID {did}").format(did=self._dids[key][1].data_id)
            else:
                title = self._signals[key][2].name
            self.selection_title.setText(title)
            self.detail_text.setPlainText("\n".join(self._detail_lines(kind, key)))
        else:
            parts = []
            if "endpoint" in kinds:
                number = kinds.count("endpoint")
                parts.append(ngettext("{count} endpoint", "{count} endpoints", number).format(count=f"{number:,}"))
            if "did" in kinds:
                parts.append(count(kinds.count("did"), "DID"))
            if "signal" in kinds:
                parts.append(count(kinds.count("signal"), "signal"))
            self.selection_title.setText(_("{items} selected").format(items=", ".join(parts)))
            self.detail_text.setPlainText(
                _("{dids} DID(s) and {signals} signal(s) in the selection.").format(
                    dids=len(selection.dids), signals=len(selection.signals),
                )
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
            state = _("Confirmed") if endpoint.confirmed else _shown(
                ENDPOINT_READABLE.get(endpoint.state, (endpoint.state,))[0]
            )
            return [
                _("{endpoint}: {state}").format(endpoint=endpoint_name(endpoint), state=state),
                _("Address: {address}").format(address=endpoint_address(endpoint) or _("unknown")),
                _("ECU keys: {keys}").format(keys=", ".join(map(str, endpoint.ecu_keys)) or "-"),
                _("Diagnostic session: {session}").format(session=(
                    self._sessions.get(endpoint.session_recipe_id, endpoint.session_recipe_id)
                    if endpoint.session_recipe_id else _("chosen when verified")
                )),
                _("DIDs: {count}").format(count=len(endpoint.dids)),
            ]
        if kind == "did":
            endpoint, did = self._dids[key]
            state = did_readable(did.state)[2:]
            return [
                _("{state}: {detail}").format(state=state, detail=did.detail) if did.detail else state,
                _("On: {endpoint}").format(endpoint=endpoint_label(endpoint)),
                _("Last answer: {answer}").format(answer=did.baseline_hex or "-"),
                _("Latency: {latency} ms at {time}").format(
                    latency=did.latency_ms if did.latency_ms is not None else "-", time=did.observed_utc or "-",
                ),
                _("Signals: {count}").format(count=len(did.signals)),
            ]
        endpoint, did, signal = self._signals[key]
        lines = [
            _("Bits: {bits} of DID {did}").format(bits=signal.signal_ref.rsplit("/", 1)[-1], did=did.data_id),
            _("Unit: {unit}   Formula: {formula}").format(unit=signal.unit or "-", formula=signal.formula),
            _("DID {did} on {endpoint}: {state}").format(
                did=did.data_id, endpoint=endpoint_name(endpoint), state=did_readable(did.state)[2:],
            ),
            _("Evidence: {evidence}").format(evidence=_status_tip(signal, did)),
        ]
        if signal.conflict:
            lines.append(_("{count} catalog variants disagree; review the definition.").format(count=signal.variants))
        if signal.review and signal.review.get("notes"):
            lines.append(_("Notes: {notes}").format(notes=signal.review["notes"]))
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
        if not found:
            reason = _("Select DIDs first.")
        elif unconfirmed:
            reason = _("Confirm {endpoint} first.").format(endpoint=endpoint_name(unconfirmed))
        else:
            reason = ""
        for widget in (self.did_included, self.did_interval, self.did_maximum, self.did_priority):
            set_enabled(widget, enabled, reason)
        title = _("Collection") if len(found) <= 1 else _("Collection: {dids}").format(dids=count(len(found), "DID"))
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
                    self.did_interval.toolTip() if not enabled else _("Change a field first."))

    def _load_targets(self, selection: Selection) -> None:
        found = [self._signals[ref][2] for ref in selection.signals]
        targeted = [signal for signal in found if signal.target]
        met = sum(1 for signal in targeted if signal.progress and signal.progress["complete"])
        if not found:
            text = _("Select signals, DIDs, or endpoints.")
        elif len(found) == 1:
            signal = found[0]
            text = _("Target: {target}").format(target=target_summary(signal.target, signal.states) or _("none"))
            if signal.target and met:
                text += "\n" + _("Already met.")
        else:
            summaries = {target_summary(signal.target) for signal in targeted}
            signals = count(len(found), "signal")
            if met:
                text = _("{signals}: {targeted} with targets, {met} already met.").format(
                    signals=signals, targeted=len(targeted), met=met,
                )
            else:
                text = _("{signals}: {targeted} with targets.").format(signals=signals, targeted=len(targeted))
            if len(summaries) == 1 and len(targeted) == len(found):
                text += "\n" + _("All: {target}").format(target=next(iter(summaries)))
            elif targeted:
                text += "\n" + _("Targets are mixed.")
        self.targets_text.setText(text)

    def _load_definition(self, endpoint, signal) -> None:
        review = signal.review or {}
        self.def_name.setText(review.get("display_name_override") or "")
        self.def_name.setPlaceholderText(signal.name)
        self.def_formula.setText(review.get("formula_override") or "")
        self.def_formula.setPlaceholderText(signal.formula)
        self.def_unit.setText(review.get("unit_override") or "")
        self.def_unit.setPlaceholderText(signal.unit)
        self.def_states.setText(states_lines(signal.states))
        self.definition_group.layout().setRowVisible(self.def_states, bool(signal.states))
        state = review.get("evidence_state") or "Candidate"
        if self.def_evidence.findData(state) < 0:
            self.def_evidence.addItem(_(state), state)
        self.def_evidence.setCurrentIndex(self.def_evidence.findData(state))
        self.def_notes.setText(review.get("notes") or "")
        set_enabled(self.def_apply, endpoint.confirmed, _("Confirm the endpoint first."))

    # Actions ----------------------------------------------------------------

    def update_actions(self) -> None:
        has_project = self.context.service is not None
        busy = self.job is not None and self.job.running
        selection = self.selection() if has_project else Selection()
        current = self._current()
        responded = False
        if current and current[0] == "endpoint" and self.tree_data is not None:
            responded = self.tree_data.endpoint(current[1]).state == "responded"
        blocked = _("Open a project first.") if not has_project else _("Wait for verification to finish.") if busy else ""
        ready = has_project and not busy
        set_enabled(self.add_endpoint_button, ready, blocked)
        set_enabled(self.confirm_button, ready and responded,
                    blocked or _("Select an endpoint that responded during verification."))
        set_enabled(self.verify_button, ready and bool(selection.dids),
                    blocked or _("Select DIDs, or endpoints with a known address, first."))
        set_enabled(self.target_button, ready and bool(selection.signals), blocked or _("Select signals first."))
        set_enabled(self.clear_button, ready and bool(selection.signals), blocked or _("Select signals first."))
        set_enabled(self.save_button, self.dirty and not busy, blocked or _("No unsaved changes."))
        live_free = ready and not self.context.live_job_running
        self.verify_action.setEnabled(live_free and bool(selection.dids))
        self.watch_action.setEnabled(live_free and self._current_did() is not None)
        self.delete_action.setEnabled(ready and not self.dirty and bool(self._selected_signal_refs()))
        set_enabled(self.discard_button, self.dirty and not busy, blocked or _("No unsaved changes."))

    def show_import(self) -> None:
        if self.context.service is None:
            return
        self.import_dialog.refresh()
        self.import_dialog.open()

    def show_context_menu(self, position) -> None:
        if self.context.service is None or not self.view.indexAt(position).isValid():
            return
        self.update_actions()
        menu = QMenu(self)
        menu.addAction(self.verify_action)
        menu.addAction(self.watch_action)
        menu.addSeparator()
        menu.addAction(self.delete_action)
        self.run_menu(menu, self.view.viewport().mapToGlobal(position))

    def _current_did(self) -> str | None:
        """The DID of the single selected DID or signal row."""

        current = self._current()
        if current is None or len(self._selected_indexes()) != 1:
            return None
        kind, key = current
        if kind == "did":
            return key
        if kind == "signal" and key in self._signals:
            return self._signals[key][1].key
        return None

    def watch_current(self) -> None:
        service = self.context.service
        did_key = self._current_did()
        if service is None or self.tree_data is None or did_key is None:
            return
        if self.watch_window is not None and self.watch_window.running:
            self.context.error(_("Live view"), _("Stop the open live view first."))
            return
        try:
            scope = service.watch_scope(self.tree_data, did_key)
        except ValueError as exc:
            self.context.error(_("Cannot show live values"), exc)
            return
        endpoint, _did = self._dids[did_key]
        title = _("{endpoint} DID {did}").format(endpoint=endpoint_label(endpoint), did=scope.data_id)
        details = [f"{signal['name']}" for signal in scope.signals]
        details.append(
            _("Every {interval} ms until you pause or stop. Nothing is recorded.").format(interval=scope.interval_ms)
        )
        technical = [
            _("{endpoint}: request {request}").format(endpoint=scope.endpoint_id, request=scope.request_hex),
            _("Session: {steps}").format(
                steps=", ".join(step["request_hex"] for step in scope.session["session_setup"]),
            ),
            _("Scope fingerprint: {digest}").format(digest=scope.digest),
        ]
        dialog = LiveConfirmDialog(_("Live view"), scope.summary() + "?", details, self, technical=technical)
        if not self.confirm_live(dialog):
            return
        if self.watch_window is not None:
            self.watch_window.close()
        self.watch_window = WatchWindow(self.context, scope, title, self)
        self.watch_window.show()
        self.watch_window.start()

    def _selected_signal_refs(self) -> list[str]:
        """Signals under the selected rows, whatever their endpoint's address."""

        refs: dict[str, None] = {}

        def collect(index: QModelIndex) -> None:
            kind, key = index.data(ROLE)
            if kind == "signal":
                refs[key] = None
                return
            for row in range(self.proxy.rowCount(index)):
                collect(self.proxy.index(row, 0, index))

        for index in self._selected_indexes():
            collect(index)
        return list(refs)

    def delete_selected(self) -> None:
        service = self.context.service
        refs = self._selected_signal_refs()
        if service is None or not refs:
            return
        if self.dirty:
            self.context.error(_("Signals not deleted"), _("Save or discard your changes first."))
            return

        def confirm(preview) -> None:
            if not preview.deleted:
                self.context.error(_("Signals not deleted"), "\n".join(describe_deletion(preview)))
                return
            if not self.confirm_delete("\n".join(describe_deletion(preview))):
                return

            def done(_result) -> None:
                self.context.message(_("Deleted {signals}.").format(signals=count(len(preview.deleted), "signal")))
                self.context.changed()

            self.context.run("signals.delete", lambda service: service.delete_signals(preview), done,
                             title=_("Signals not deleted"))

        self.context.run("signals.delete.preview", lambda service: service.preview_delete_signals(refs), confirm,
                         title=_("Signals not deleted"))

    def confirm_endpoint(self) -> None:
        current = self._current()
        if self.context.service is None or current is None or current[0] != "endpoint":
            return
        endpoint = self.tree_data.endpoint(current[1])
        dialog = EndpointConfirmDialog(endpoint_label(endpoint), endpoint.ecu_keys, self)
        if not self.run_dialog(dialog):
            return
        try:
            ecu_keys, sources = dialog.values()
            self.context.service.confirm_endpoint(endpoint.endpoint_id, ecu_keys, sources)
        except (OSError, ValueError) as exc:
            self.context.error(_("Endpoint not confirmed"), exc)
            return
        self.context.message(_("Confirmed {endpoint}.").format(endpoint=endpoint_label(endpoint)))
        self.context.changed()

    def verify_checked(self) -> None:
        service = self.context.service
        if service is None or self.tree_data is None:
            return
        selection = self.selection()
        try:
            # A new endpoint's session is chosen automatically: each reviewed
            # recipe is tried until one opens a session.
            scope = service.verification_scope(self.tree_data, selection)
        except ValueError as exc:
            self.context.error(_("Cannot verify"), exc)
            return
        details = []
        technical = []
        for row in scope.endpoints:
            endpoint = self.tree_data.endpoint(row.key)
            name = endpoint_label(endpoint) if endpoint else row.key
            dids = count(len(row.dids), "DID")
            if row.probe_endpoint:
                details.append(_("{endpoint}: probe, then read {dids}").format(endpoint=name, dids=dids))
            else:
                details.append(_("{endpoint}: read {dids}").format(endpoint=name, dids=dids))
            details.append("    " + ", ".join(did.rsplit("/", 1)[-1] for did in row.dids))
            if row.target_address:
                technical.append(_("{key}: {tester}/{response} address {address}, {session}").format(
                    key=row.key, tester=row.tester_can_id, response=row.response_can_id,
                    address=row.target_address, session=row.session_text,
                ))
            else:
                technical.append(f"{row.key}: {row.tester_can_id}/{row.response_can_id}, {row.session_text}")
        details.append(_("{probes} probe(s), at most {seconds} s.").format(
            probes=scope.probe_count, seconds=f"{scope.upper_bound_seconds:.0f}",
        ))
        technical.append(_("Scope fingerprint: {digest}").format(digest=scope.digest))
        dialog = LiveConfirmDialog(_("Verify"), scope.summary() + "?", details, self, technical=technical)
        if not self.confirm_live(dialog):
            return
        self.job_log.clear()
        self.job_log.setVisible(True)
        self.job_log.appendPlainText(scope.summary())
        self.job_progress.setRange(0, max(1, scope.probe_count))
        self.job_progress.setValue(0)
        self.job_progress.setVisible(True)

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
        progress = progress_text(event)
        if event.get("done") is not None:
            self.job_progress.setValue(int(event["done"]))
        if event["event"] == "endpoint":
            if event["responded"]:
                text = _("{progress} Endpoint {endpoint}: responded")
            else:
                text = _("{progress} Endpoint {endpoint}: no response")
            self.job_log.appendPlainText(text.format(progress=progress, endpoint=event["key"]))
        else:
            # The outcome and detail come from the verification service as they are.
            detail = f" {event['detail']}" if event.get("detail") else ""
            self.job_log.appendPlainText(f"{progress} {event['key']}: {event['outcome']}{detail}")

    def _job_finished(self, result) -> None:
        summary = result.as_dict()
        outcomes = ", ".join(f"{number} {name}" for name, number in sorted(summary["outcomes"].items()))
        outcomes = outcomes or _("no DIDs read")
        if result.stopped:
            self.job_log.appendPlainText(_("Stopped early: {outcomes}").format(outcomes=outcomes))
        else:
            self.job_log.appendPlainText(_("Finished: {outcomes}").format(outcomes=outcomes))
        self._job_done()

    def _job_failed(self, detail: str) -> None:
        self.job_log.appendPlainText(_("Failed: {detail}").format(detail=detail.splitlines()[0]))
        self._job_done()
        self.context.error(_("Verification failed"), detail.splitlines()[0])

    def _job_done(self) -> None:
        self.stop_button.setVisible(False)
        self.job_progress.setVisible(False)
        self.context.set_live(False)
        self.job = None
        self.context.changed()

    def stop_job(self) -> None:
        if self.job is not None:
            self.job.request_stop()
            self.job_log.appendPlainText(_("Stopping after the current probe…"))

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

        # Offer states by name when every selected signal shares one set.
        found = [self.tree_data.find_signal(ref) for ref in selection.signals]
        shared = {tuple(sorted(item[2].states.items())) for item in found if item}
        states = dict(next(iter(shared))) if len(shared) == 1 else None
        dialog = TargetEditDialog(len(selection.signals), current, preview, self, states=states or None)
        if not self.run_dialog(dialog):
            return
        try:
            result = service.edit_targets(
                self.tree_data, selection.signals, policy=self.draft_policy, **dialog.fields()
            )
        except ValueError as exc:
            self.context.error(_("Targets not changed"), exc)
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
                evidence_state=self.def_evidence.currentData(), notes=self.def_notes.text(),
            )
        except ValueError as exc:
            self.context.error(_("Definition not applied"), exc)
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
            self.context.error(_("Collection settings not applied"), exc)
            return
        siblings = result.impact()["unselected_siblings"]
        if siblings:
            self.context.message(
                _("Settings also apply to {count} other signal(s) on the same DIDs.").format(count=len(siblings))
            )
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
            show_notice(self.external_notice, _("Not saved: project files changed outside this window."))
            self.external_notice.setToolTip(_("Discard, then edit again"))
            return False
        try:
            service.save(review=self.draft_review, policy=self.draft_policy)
        except (OSError, ValueError) as exc:
            self.context.error(_("Not saved"), exc)
            return False
        self.reload()
        self.context.message(_("Saved review and collection settings."))
        self.context.changed()
        return True

    def discard(self) -> None:
        self.reload()

    def prompt_unsaved(self) -> bool:
        """Return True when it is fine to continue (saved or discarded)."""

        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, _("Unsaved changes"), _("Save changes to signal review and collection settings?"),
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


def _endpoint_tip(endpoint, sessions: dict[str, str]) -> str:
    if endpoint.tester_can_id is None:
        return _("Address unknown; add it under Add candidates")
    recipe = endpoint.session_recipe_id
    return _("Session: {steps}").format(steps=sessions.get(recipe, recipe)) if recipe else ""


def _shown(text: str) -> str:
    """Translate a value for display; empty stays empty (gettext("") is the catalog header)."""

    return _(text) if text else ""


def _impact_text(impact: dict[str, Any], selected: int) -> str:
    text = _("{selected} signal(s) on {dids} DID(s) will change.").format(
        selected=selected, dids=len(impact["dids"]),
    )
    created = len(impact["created_did_settings"])
    if created:
        text += " " + _("{created} DID(s) will start being read every 1 s (change this under Collection).").format(
            created=created,
        )
    return text


def _status_tip(signal, did) -> str:
    if signal.status == "Reviewed":
        return _("reviewed as {state}").format(state=_shown(signal.review["evidence_state"]))
    if signal.status == "Recorded":
        return _("{count} samples recorded across runs; meaning not reviewed yet").format(count=signal.observations)
    if signal.status == "Refused":
        return _("none; its DID was refused")
    return _("nothing recorded yet")


def unsaved_keys(saved_review, saved_policy, draft_review, draft_policy) -> set[str]:
    """Signal references and acquisition IDs whose draft differs from the saved files."""

    changed: set[str] = set()

    def rows(document, section, key):
        return {row[key]: row for row in (document or {}).get(section, [])}

    if draft_policy is not None:
        for section, key in (("targets", "signal_ref"), ("acquisitions", "acquisition_id")):
            before, after = rows(saved_policy, section, key), rows(draft_policy, section, key)
            changed |= {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    if draft_review is not None:
        before, after = rows(saved_review, "signals", "signal_ref"), rows(draft_review, "signals", "signal_ref")
        changed |= {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    return changed


def mark_unsaved(row: list, color) -> None:
    """Italic, accent-coloured cells: this row's edits are a draft until Save."""

    for item in row:
        font = item.font()
        font.setItalic(True)
        item.setFont(font)
        item.setForeground(color)
        tip = item.toolTip()
        item.setToolTip((tip + "\n" if tip else "") + _("Not saved yet"))
