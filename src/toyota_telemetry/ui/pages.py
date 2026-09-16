"""Offline second-draft workflow mockups for the desktop operator application."""

from __future__ import annotations

from pathlib import Path
import threading

import numpy as np
from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator import BoundedSeries, bounded_minmax

from .models import SyntheticSignalModel
from .mock_data import (
    DID_VERIFICATION_STATES,
    EXAMPLE_DIDS,
    EXAMPLE_ENDPOINTS,
    EXAMPLE_RUNS,
    EXAMPLE_SIGNAL_COUNT,
    EXAMPLE_TARGETS,
    READINESS_ISSUES,
    REQUIRED_DIDS,
)
from .tasks import LatestTaskController
from .widgets import MetricPlot, ReadinessCard, disable_preview, disabled_action, page_title, preview_notice


PREVIEW_DISABLED = "Preview only: project services are not connected."
LIVE_DISABLED = "Preview only: no adapter is opened and nothing is transmitted."


def _button_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    return row


def _table(
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    *,
    stretch_column: int | None = None,
    numeric_columns: tuple[int, ...] = (),
) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    _emphasize_selection(table)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            item = QTableWidgetItem(value)
            if column in numeric_columns:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row_index, column, item)
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    if stretch_column is not None:
        header.setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
    return table


def _path_field(field: QLineEdit, caption: str, *, directory: bool = False) -> QWidget:
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(field, 1)
    browse = QPushButton("Browse...")
    browse.setAccessibleName(f"Browse for {caption}")

    def choose() -> None:
        selected = (
            QFileDialog.getExistingDirectory(container, caption)
            if directory
            else QFileDialog.getOpenFileName(container, caption)[0]
        )
        if selected:
            field.setText(selected)

    browse.clicked.connect(choose)
    row.addWidget(browse)
    return container


def _spin(value: int, suffix: str = "") -> QSpinBox:
    control = QSpinBox()
    control.setRange(0, 1_000_000)
    control.setValue(value)
    control.setSuffix(suffix)
    return control


def _emphasize_selection(view: QAbstractItemView) -> None:
    view.setStyleSheet(
        "QAbstractItemView::item:selected {"
        " background: palette(highlight); color: palette(highlighted-text);"
        " border: 1px solid palette(highlight); }"
    )


class OverviewPage(QWidget):
    choose_project = Signal()
    navigate_requested = Signal(int)

    def __init__(self, project_path: Path | None = None) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        layout.addWidget(page_title("Telemetry workspace", "Readiness and the next useful action."))
        self.project_label = QLabel()
        self.set_project(project_path)
        layout.addWidget(self.project_label)

        layout.addWidget(
            preview_notice(
                "Recommended next action: Connect hardware and validate a local connector. "
                "All readiness values in this draft are examples."
            )
        )

        definitions = (
            ("Connector", "Needs setup", "PCAN selected; settings not validated", "Configure", 1),
            ("Candidate signal list", "Example only", f"{EXAMPLE_SIGNAL_COUNT} signals in 5 DIDs; no source imported", "Review list", 2),
            ("Vehicle verification", "2 checks needed", "Hybrid endpoint and brake-switch DID pending", "Verify", 3),
            ("Collection targets", "Review needed", "Coolant-validity condition source needs review", "Set targets", 4),
            ("Collection readiness", f"{len(READINESS_ISSUES)} problems", "2 verification facts and 1 review needed", "Resolve", 5),
            ("Recoverable runs", "None", "No unfinished capture segments", "Inspect", 6),
            ("Recordings", "3 example runs", "Synthetic plot; no recordings loaded", "Analyze", 6),
        )
        grid = QGridLayout()
        self.cards: list[ReadinessCard] = []
        for index, (name, status, evidence, action, destination) in enumerate(definitions):
            card = ReadinessCard(name, status, evidence, action)
            card.action.clicked.connect(
                lambda checked=False, page=destination: self.navigate_requested.emit(page)
            )
            grid.addWidget(card, index // 3, index % 3)
            self.cards.append(card)
        layout.addLayout(grid)
        layout.addStretch(1)
        open_button = QPushButton("Open existing project")
        open_button.clicked.connect(self.choose_project)
        layout.addLayout(_button_row(open_button))

    def set_project(self, path: Path | None) -> None:
        if path is None:
            self.project_label.setText("Example data is shown because no project is open.")
        else:
            self.project_label.setText(f"Project marker: {path.name} | Example data remains in use.")


class ConnectHardwarePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(
            page_title(
                "Connect hardware",
                "Configure only the machine-local route to the CAN adapter. Vehicle addresses do not belong here.",
            )
        )
        layout.addWidget(preview_notice("OFFLINE PREVIEW - adapter closed; no device will be opened."))

        settings = QGroupBox("Connector settings")
        settings.setMaximumWidth(680)
        form = QFormLayout(settings)
        self.connector = QComboBox()
        self.connector.addItems(("PCAN", "J2534", "gs_usb", "slcan"))
        form.addRow("Connector", self.connector)
        self.connector_settings = QStackedWidget()
        pcan = QWidget()
        pcan_form = QFormLayout(pcan)
        self.pcan_channel = QComboBox()
        self.pcan_channel.addItems(("PCAN_USBBUS1", "PCAN_USBBUS2", "PCAN_USBBUS3"))
        pcan_form.addRow("Channel", self.pcan_channel)
        self.connector_settings.addWidget(pcan)

        j2534 = QWidget()
        j2534_form = QFormLayout(j2534)
        self.j2534_dll = QLineEdit()
        self.j2534_dll.setPlaceholderText("Path to vendor PassThru DLL")
        j2534_form.addRow("PassThru DLL", _path_field(self.j2534_dll, "Select PassThru DLL"))
        self.connector_settings.addWidget(j2534)

        gs_usb = QWidget()
        gs_form = QFormLayout(gs_usb)
        self.usb_vendor = QLineEdit()
        self.usb_vendor.setPlaceholderText("Hex vendor ID, e.g. 0x1D50")
        self.usb_product = QLineEdit()
        self.usb_product.setPlaceholderText("Hex product ID, e.g. 0x606F")
        self.usb_serial = QLineEdit()
        self.usb_serial.setPlaceholderText("Optional serial")
        gs_form.addRow("Vendor ID", self.usb_vendor)
        gs_form.addRow("Product ID", self.usb_product)
        gs_form.addRow("Serial", self.usb_serial)
        self.connector_settings.addWidget(gs_usb)

        slcan = QWidget()
        slcan_form = QFormLayout(slcan)
        self.serial_port = QComboBox()
        self.serial_port.addItems(("Select port...", "COM3", "COM4"))
        self.serial_port.setEditable(True)
        slcan_form.addRow("Serial port", self.serial_port)
        self.connector_settings.addWidget(slcan)
        form.addRow("Settings", self.connector_settings)
        self.connector.currentIndexChanged.connect(self.connector_settings.setCurrentIndex)
        layout.addWidget(settings)
        self.feedback = QLabel("Preview checks required fields only; driver and worker self-test are not run.")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        validate = QPushButton("Validate settings")
        validate.clicked.connect(self._validate_preview)
        save = QPushButton("Save local settings")
        action = disabled_action(save, PREVIEW_DISABLED)
        layout.addWidget(validate, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(action)
        layout.addStretch(1)

    def _validate_preview(self) -> None:
        connector = self.connector.currentText()
        missing = {
            "PCAN": (),
            "J2534": ("PassThru DLL",) if not self.j2534_dll.text().strip() else (),
            "gs_usb": tuple(
                name for name, field in (("Vendor ID", self.usb_vendor), ("Product ID", self.usb_product))
                if not field.text().strip()
            ),
            "slcan": ("Serial port",) if self.serial_port.currentText().strip() in ("", "Select port...") else (),
        }[connector]
        if missing:
            self.feedback.setText(f"Preview field check: enter {', '.join(missing)}. No device was opened.")
        else:
            self.feedback.setText(
                "Preview field check complete. Driver, path, and worker self-test have not run; adapter closed."
            )


class BuildSignalListPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(
            page_title(
                "Build signal list",
                "Choose where candidate ECUs, DIDs, signal definitions, and provenance come from.",
            )
        )

        source_row = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItems(("TIS database", "Normalized file"))
        source_row.addWidget(QLabel("Source"))
        source_row.addWidget(self.source)
        source_row.addStretch(1)
        layout.addLayout(source_row)
        self.source_settings = QStackedWidget()
        self.source_settings.setMaximumWidth(680)

        tis = QGroupBox("TIS database selection")
        tis_form = QFormLayout(tis)
        self.database = QLineEdit()
        self.database.setPlaceholderText("Path to the locally available TIS database")
        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText("Vehicle model")
        self.model_year = QComboBox()
        self.model_year.addItems(("Select year...",) + tuple(str(value) for value in range(2026, 2017, -1)))
        tis_form.addRow("Database", _path_field(self.database, "Select TIS database"))
        tis_form.addRow("Model", self.model_name)
        tis_form.addRow("Model year", self.model_year)
        self.source_settings.addWidget(tis)

        normalized = QGroupBox("Normalized candidate file")
        file_form = QFormLayout(normalized)
        self.candidate_file = QLineEdit()
        self.candidate_file.setPlaceholderText("Path to normalized catalog JSON")
        file_form.addRow("File", _path_field(self.candidate_file, "Select normalized candidate file"))
        self.source_settings.addWidget(normalized)

        self.source.currentIndexChanged.connect(self.source_settings.setCurrentIndex)
        self.source.currentIndexChanged.connect(self._clear_preview)
        layout.addWidget(self.source_settings)

        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter candidate signals, ECUs, endpoints, or DIDs")
        controls.addWidget(self.search, 1)
        self.preview_button = QPushButton("Preview import")
        self.preview_button.clicked.connect(self._preview_import)
        controls.addWidget(self.preview_button)
        layout.addLayout(controls)
        self.summary = QLabel(
            f"No source imported. The example inventory below has {EXAMPLE_SIGNAL_COUNT} signals in {len(EXAMPLE_DIDS)} DIDs."
        )
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        example_rows = tuple(
            (did.source, did.endpoint, f"0x{did.key}", signal, did.state)
            for did in EXAMPLE_DIDS for signal in did.signals
        )
        self.example_table = _table(
            ("Source", "ECU", "DID", "Signal", "Verification"),
            example_rows,
            stretch_column=3,
        )
        self.search.textChanged.connect(self._filter_examples)
        self.model = SyntheticSignalModel()
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 210)
        # The scale fixture remains available to automated/developer tests, but
        # is deliberately absent from the operator workflow.
        self.table.setVisible(False)
        layout.addWidget(self.example_table, 1)
        accept = QPushButton("Use this candidate list")
        layout.addWidget(disabled_action(accept, PREVIEW_DISABLED))

    def _clear_preview(self, _index: int) -> None:
        if hasattr(self, "summary"):
            self.summary.setText("Source changed. No file or database has been read in this offline preview.")

    def _preview_import(self) -> None:
        if self.source.currentIndex() == 0:
            missing = tuple(name for name, value in (
                ("Database", self.database.text().strip()),
                ("Model", self.model_name.text().strip()),
                ("Model year", "" if self.model_year.currentIndex() == 0 else self.model_year.currentText()),
            ) if not value)
        else:
            missing = ("File",) if not self.candidate_file.text().strip() else ()
        if missing:
            self.summary.setText(f"Preview input check: enter {', '.join(missing)}. No source was read.")
            return
        self.summary.setText(
            f"Preview input captured for {self.source.currentText()}; not parsed. "
            f"The {EXAMPLE_SIGNAL_COUNT}-signal table is independent example data."
        )

    def _filter_examples(self, needle: str) -> None:
        query = needle.casefold().strip()
        for row in range(self.example_table.rowCount()):
            visible = not query or any(
                query in self.example_table.item(row, column).text().casefold()
                for column in range(self.example_table.columnCount())
            )
            self.example_table.setRowHidden(row, not visible)


class VerifyVehiclePage(QWidget):
    return_to_collect = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.connector_name = "PCAN"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(
            page_title(
                "Verify with vehicle",
                "Probe each required endpoint first, then preflight its selected DIDs and record baselines.",
            )
        )
        layout.addWidget(preview_notice("LIVE OPERATION PREVIEW - explicit finite scope; adapter remains closed."))
        help_text = QLabel(
            "Endpoint = ECU address and session. DID = one data request; its response may contain several signals. "
            "Verified means a response was recorded; Not verified means no successful check exists; "
            "Endpoint not verified means its ECU route must be checked first. Results stay temporary until saved."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        filters = QHBoxLayout()
        self.source_filter = QComboBox()
        self.source_filter.addItems(("All sources", "TIS database", "Normalized file"))
        self.ecu_filter = QComboBox()
        self.ecu_filter.addItems(("All ECUs", "Powertrain", "Hybrid control"))
        self.state_filter = QComboBox()
        self.state_filter.addItems(("All states",) + DID_VERIFICATION_STATES)
        for combo in (self.source_filter, self.ecu_filter, self.state_filter):
            filters.addWidget(combo)
            combo.currentIndexChanged.connect(self._apply_filters)
        filters.addStretch(1)
        layout.addLayout(filters)

        self.tree = QTreeWidget()
        _emphasize_selection(self.tree)
        self.tree.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.tree.setHeaderLabels(("Endpoint / DID", "Selection", "State", "Baseline", "Bytes", "Latency", "Source"))
        group_brush = self.palette().brush(QPalette.ColorRole.AlternateBase)
        self.did_items: dict[str, QTreeWidgetItem] = {}
        for ecu, address, state in EXAMPLE_ENDPOINTS:
            parent = QTreeWidgetItem((f"{ecu} - {address}", "", state, "", "", "", "Example inventory"))
            parent.setData(0, Qt.ItemDataRole.UserRole, ecu)
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            for column in range(self.tree.columnCount()):
                parent.setBackground(column, group_brush)
            self.tree.addTopLevelItem(parent)
            for did in (item for item in EXAMPLE_DIDS if item.endpoint == ecu):
                selected = did.key in REQUIRED_DIDS
                child = QTreeWidgetItem((
                    f"DID 0x{did.key}  {did.label}",
                    "Selected" if selected else "Not selected",
                    did.state, did.baseline, did.response_bytes, did.latency, did.source,
                ))
                child.setData(0, Qt.ItemDataRole.UserRole, did.key)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
                parent.addChild(child)
                self.did_items[did.key] = child
            parent.setExpanded(True)
        self.tree.itemChanged.connect(self._update_scope)
        for column in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(column)
        layout.addWidget(self.tree, 1)
        self.arrival_notice = QLabel()
        self.arrival_notice.setWordWrap(True)
        self.arrival_notice.setVisible(False)
        layout.addWidget(self.arrival_notice)
        self.scope_label = QLabel()
        self.scope_label.setWordWrap(True)
        layout.addWidget(self.scope_label)
        self.verify_button = QPushButton()
        self.verify_button.setObjectName("verify-live")
        verify_action = disabled_action(self.verify_button, LIVE_DISABLED, live=True)
        self.return_button = QPushButton("Return to collection readiness")
        self.return_button.setVisible(False)
        self.return_button.clicked.connect(self.return_to_collect)
        action_row = QHBoxLayout()
        action_row.addWidget(self.return_button, alignment=Qt.AlignmentFlag.AlignTop)
        action_row.addStretch(1)
        action_row.addWidget(verify_action)
        layout.addLayout(action_row)
        self._update_scope()

    def _apply_filters(self, _index: int) -> None:
        source = self.source_filter.currentText()
        ecu = self.ecu_filter.currentText()
        state = self.state_filter.currentText()
        for parent_index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(parent_index)
            ecu_name = parent.data(0, Qt.ItemDataRole.UserRole)
            visible_children = 0
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                child_state = child.text(2)
                match = (
                    (source == "All sources" or child.text(6) == source)
                    and (ecu == "All ECUs" or ecu_name == ecu)
                    and (state == "All states" or child_state == state)
                )
                child.setHidden(not match)
                visible_children += int(match)
            parent.setHidden(visible_children == 0)

    def _update_scope(self, *_args: object) -> None:
        selected = [item for item in self.did_items.values() if item.checkState(0) == Qt.CheckState.Checked]
        endpoints = len({item.parent().data(0, Qt.ItemDataRole.UserRole) for item in selected})
        for item in self.did_items.values():
            item.setText(1, "Selected" if item.checkState(0) == Qt.CheckState.Checked else "Not selected")
        self.scope_label.setText(
            f"Example scope: {self.connector_name} | {endpoints} endpoints | {len(selected)} DIDs | "
            "reviewed sessions = saved ECU setup | <= 12 s example bound | temporary local results"
        )
        self.verify_button.setText(f"LIVE - Verify {endpoints} endpoints and {len(selected)} DIDs")

    def set_connector(self, name: str) -> None:
        self.connector_name = name
        self._update_scope()

    def focus_issue(self, did_key: str) -> None:
        item = self.did_items[did_key]
        item.setCheckState(0, Qt.CheckState.Checked)
        self.source_filter.setCurrentIndex(0)
        self.ecu_filter.setCurrentIndex(0)
        self.state_filter.setCurrentIndex(0)
        item.parent().setExpanded(True)
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        self.arrival_notice.setText(
            f"Collection readiness sent you here to verify DID 0x{did_key}. "
            "It is selected below; return when the issue is resolved."
        )
        self.arrival_notice.setVisible(True)
        self.return_button.setVisible(True)


class CollectionTargetsPage(QWidget):
    return_to_collect = Signal()
    dirty_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._draft_targets = {
            name: {"condition": row[2], "samples": row[3], "changes": row[4], "difference": row[5]}
            for name, row in EXAMPLE_TARGETS.items()
        }
        self._did_draft = {
            did.key: {"target": 100 if did.key == "F421" else 1000,
                      "maximum": 250 if did.key == "F421" else 5000}
            for did in EXAMPLE_DIDS
        }
        self._loading_detail = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(
            page_title(
                "Set collection targets",
                "Choose how much evidence is enough. Acquisition settings are shared by endpoint and DID.",
            )
        )
        explanation = QLabel(
            "One DID read can supply several signals. Its polling intervals affect every child signal. "
            "A target says what evidence to collect; the checkboxes include or exclude it."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a signal, endpoint, or DID")
        toolbar.addWidget(self.search, 1)
        self.state_filter = QComboBox()
        self.state_filter.addItems(("All targets", "Needs review", "No target", "Incomplete"))
        toolbar.addWidget(self.state_filter)
        batch = QPushButton("Batch edit collection targets...")
        toolbar.addWidget(
            disabled_action(batch, "Batch editing is not simulated in this offline preview.")
        )
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter = splitter
        self.tree = QTreeWidget()
        _emphasize_selection(self.tree)
        self.tree.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.tree.setHeaderLabels(
            ("Signal groups and signals", "Vehicle verification", "Polling interval", "Target status", "Recorded progress")
        )
        group_brush = self.palette().brush(QPalette.ColorRole.AlternateBase)
        self.signal_items: dict[str, QTreeWidgetItem] = {}
        self.did_groups: dict[str, QTreeWidgetItem] = {}
        for did in EXAMPLE_DIDS:
            interval = "100 / 250 ms" if did.key == "F421" else "1 / 5 s"
            parent = QTreeWidgetItem((
                f"{did.endpoint} / DID 0x{did.key} {did.label}",
                did.state, interval, "", "",
            ))
            parent.setData(0, Qt.ItemDataRole.UserRole, did.key)
            parent.setFlags(
                parent.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            parent.setCheckState(0, Qt.CheckState.Checked)
            parent.setToolTip(2, "Shared by all signals read from this endpoint and DID")
            for column in range(self.tree.columnCount()):
                parent.setBackground(column, group_brush)
            self.tree.addTopLevelItem(parent)
            self.did_groups[did.key] = parent
            for signal in did.signals:
                unit, formula, condition, samples, changes, threshold, coverage, status = EXAMPLE_TARGETS[signal]
                child = QTreeWidgetItem((signal, did.state, "", status, coverage))
                child.setData(0, Qt.ItemDataRole.UserRole, signal)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked if status == "No target" else Qt.CheckState.Checked)
                parent.addChild(child)
                self.signal_items[signal] = child
            parent.setExpanded(True)
        self.search.textChanged.connect(self._apply_filters)
        self.state_filter.currentIndexChanged.connect(self._apply_filters)
        self.tree.currentItemChanged.connect(self._show_selection)
        self.tree.itemChanged.connect(self._inclusion_changed)
        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(2)
        splitter.addWidget(self.tree)

        details = QWidget()
        details.setMinimumWidth(0)
        details_layout = QVBoxLayout(details)
        definition = QGroupBox("Signal definition")
        definition_form = QFormLayout(definition)
        self.definition_name = QLabel()
        self.definition_field = QLabel()
        self.definition_formula = QLabel()
        self.definition_source = QLabel()
        for value in (
            self.definition_name,
            self.definition_field,
            self.definition_formula,
            self.definition_source,
        ):
            value.setWordWrap(True)
        definition_form.addRow("Signal / group", self.definition_name)
        definition_form.addRow("Field", self.definition_field)
        definition_form.addRow("Unit / formula", self.definition_formula)
        definition_form.addRow("Verification / source", self.definition_source)
        details_layout.addWidget(definition)

        target = QGroupBox("Collection target")
        target_form = QFormLayout(target)
        self.condition = QComboBox()
        self.condition.addItems(("Always", "Vehicle moving", "ICE running", "Custom condition..."))
        target_form.addRow("Collect when", self.condition)
        self.minimum_samples = _spin(0)
        self.minimum_changes = _spin(0)
        target_form.addRow("Minimum samples", self.minimum_samples)
        target_form.addRow("Minimum changes", self.minimum_changes)
        change_help = QLabel(
            "A change counts when consecutive decoded values differ by at least "
            "the minimum difference below."
        )
        change_help.setWordWrap(True)
        target_form.addRow(change_help)
        self.minimum_difference = _spin(0)
        target_form.addRow("Minimum difference", self.minimum_difference)
        self.did_target = _spin(100, " ms")
        self.did_target.setToolTip("Shared by all signals read from this endpoint and DID")
        target_form.addRow("DID target interval", self.did_target)
        self.did_maximum = _spin(250, " ms")
        self.did_maximum.setToolTip("Shared by all signals read from this endpoint and DID")
        target_form.addRow("DID maximum interval", self.did_maximum)
        details_layout.addWidget(target)

        progress = QGroupBox("Recorded progress")
        progress_form = QFormLayout(progress)
        self.progress_samples = QLabel()
        self.progress_changes = QLabel()
        self.progress_coverage = QLabel()
        self.progress_last = QLabel("No recording loaded; these are example counters")
        for value in (
            self.progress_samples,
            self.progress_changes,
            self.progress_coverage,
            self.progress_last,
        ):
            value.setWordWrap(True)
        progress_form.addRow("Valid samples", self.progress_samples)
        progress_form.addRow("Changes", self.progress_changes)
        progress_form.addRow("Coverage", self.progress_coverage)
        progress_form.addRow("Last value", self.progress_last)
        details_layout.addWidget(progress)
        splitter.addWidget(details)
        splitter.setSizes((720, 390))
        layout.addWidget(splitter, 1)
        self.feedback = QLabel("Example draft only. Selection updates these details; Save is not connected.")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self.condition.currentTextChanged.connect(self._draft_changed)
        for field in (self.minimum_samples, self.minimum_changes, self.minimum_difference):
            field.valueChanged.connect(self._draft_changed)
        self.did_target.valueChanged.connect(self._did_draft_changed)
        self.did_maximum.valueChanged.connect(self._did_draft_changed)
        save = QPushButton("Save signal review and collection targets")
        save_action = disabled_action(save, PREVIEW_DISABLED)
        self.return_button = QPushButton("Return to collection readiness")
        self.return_button.setVisible(False)
        self.return_button.clicked.connect(self.return_to_collect)
        action_row = QHBoxLayout()
        action_row.addWidget(self.return_button, alignment=Qt.AlignmentFlag.AlignTop)
        action_row.addStretch(1)
        action_row.addWidget(save_action)
        layout.addLayout(action_row)
        self.tree.setCurrentItem(self.signal_items["Wheel speed FL"])

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        narrow = event.size().width() < 980
        orientation = Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes((330, 520) if narrow else (720, 390))

    def _apply_filters(self, *_args: object) -> None:
        needle = self.search.text().casefold().strip()
        status = self.state_filter.currentText()
        for parent_index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(parent_index)
            group_match = needle in parent.text(0).casefold()
            visible_children = 0
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                match = (not needle or group_match or needle in child.text(0).casefold()) and (
                    status == "All targets" or child.text(3).startswith(status)
                )
                child.setHidden(not match)
                visible_children += int(match)
            parent.setHidden(visible_children == 0)

    def _show_selection(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        self._loading_detail = True
        if item.parent() is None:
            did = next(row for row in EXAMPLE_DIDS if row.key == item.data(0, Qt.ItemDataRole.UserRole))
            shared = self._did_draft[did.key]
            self.definition_name.setText(f"DID 0x{did.key}: {did.label}")
            self.definition_field.setText(f"One request supplies {len(did.signals)} signal(s)")
            self.definition_formula.setText("Shared acquisition; select a child for signal definition")
            self.definition_source.setText(f"{did.state} | {did.source}")
            self.progress_samples.setText("See child signals")
            self.progress_changes.setText("See child signals")
            self.progress_coverage.setText("Group baseline: " + did.baseline)
            for control in (self.condition, self.minimum_samples, self.minimum_changes, self.minimum_difference):
                control.setEnabled(False)
            self.did_target.setValue(shared["target"])
            self.did_maximum.setValue(shared["maximum"])
            self._loading_detail = False
            return

        signal = item.data(0, Qt.ItemDataRole.UserRole)
        did_key = item.parent().data(0, Qt.ItemDataRole.UserRole)
        did = next(row for row in EXAMPLE_DIDS if row.key == did_key)
        unit, formula, condition, samples, changes, difference, coverage, status = EXAMPLE_TARGETS[signal]
        self.definition_name.setText(signal)
        self.definition_field.setText(f"DID 0x{did.key} child field; example bit mapping")
        self.definition_formula.setText(f"{unit} | {formula}")
        self.definition_source.setText(f"{did.state} | {did.source}")
        for control in (self.condition, self.minimum_samples, self.minimum_changes, self.minimum_difference):
            control.setEnabled(True)
        draft = self._draft_targets[signal]
        self.condition.setCurrentText(draft["condition"])
        self.minimum_samples.setValue(draft["samples"])
        self.minimum_changes.setValue(draft["changes"])
        self.minimum_difference.setValue(draft["difference"])
        self.minimum_difference.setSuffix(" " + unit if difference else "")
        shared = self._did_draft[did.key]
        self.did_target.setValue(shared["target"])
        self.did_maximum.setValue(shared["maximum"])
        self.progress_samples.setText("Example: " + coverage if samples else "Not a sample target")
        self.progress_changes.setText("Example: 84 / 120" if changes else "Not a change target")
        self.progress_coverage.setText(coverage)
        self.feedback.setText(f"Selected: {signal} | {status} | example draft only; Save is not connected.")
        self._loading_detail = False

    def _draft_changed(self, *_args: object) -> None:
        if self._loading_detail:
            return
        item = self.tree.currentItem()
        if item is None or item.parent() is None:
            return
        signal = item.data(0, Qt.ItemDataRole.UserRole)
        draft = self._draft_targets[signal]
        draft.update({
            "condition": self.condition.currentText(),
            "samples": self.minimum_samples.value(),
            "changes": self.minimum_changes.value(),
            "difference": self.minimum_difference.value(),
        })
        item.setText(3, EXAMPLE_TARGETS[signal][-1] + " (draft)")
        self.feedback.setText(f"{signal}: local example draft changed; Save is not connected.")
        self.dirty_changed.emit(True)

    def _inclusion_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or item.parent() is None:
            return
        signal = item.data(0, Qt.ItemDataRole.UserRole)
        if signal:
            self.feedback.setText(
                f"{signal}: {'included' if item.checkState(0) == Qt.CheckState.Checked else 'excluded'} "
                "in this local example draft only."
            )
            self.dirty_changed.emit(True)

    def _did_draft_changed(self, *_args: object) -> None:
        if self._loading_detail:
            return
        item = self.tree.currentItem()
        if item is None:
            return
        parent = item.parent() or item
        did_key = parent.data(0, Qt.ItemDataRole.UserRole)
        shared = self._did_draft[did_key]
        shared["target"] = self.did_target.value()
        shared["maximum"] = self.did_maximum.value()
        parent.setText(2, f"{shared['target']} / {shared['maximum']} ms (draft)")
        self.feedback.setText(
            f"DID 0x{did_key}: shared example intervals changed for {parent.childCount()} child signals; Save is not connected."
        )
        self.dirty_changed.emit(True)

    def focus_issue(self, signal: str) -> None:
        item = self.signal_items[signal]
        self.search.clear()
        self.state_filter.setCurrentIndex(0)
        item.parent().setExpanded(True)
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        self.feedback.setText(
            f"Collection readiness sent you here to review {signal}. "
            "The highlighted row and details show the affected target."
        )
        self.return_button.setVisible(True)


class CollectDataPage(QWidget):
    issue_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(
            page_title(
                "Collect data",
                "Readiness is checked automatically. During a run, only overall progress and health stay prominent.",
            )
        )
        layout.addWidget(
            preview_notice(
                "Example collection readiness: 3 unresolved inputs. No real plan has been compiled."
            )
        )
        summary = QGridLayout()
        for index, (name, value) in enumerate((
            ("Unfinished targets", "7"),
            ("Endpoints / DIDs", f"{len(EXAMPLE_ENDPOINTS)} / {len(REQUIRED_DIDS)}"),
            ("Estimated bus load", "Not checked"),
            ("Maximum storage", "Not checked"),
        )):
            card = QGroupBox(name)
            card_layout = QVBoxLayout(card)
            card_layout.addWidget(QLabel(value), alignment=Qt.AlignmentFlag.AlignCenter)
            summary.addWidget(card, index // 2, index % 2)
        layout.addLayout(summary)

        readiness = QGroupBox("Problems to resolve")
        readiness_layout = QVBoxLayout(readiness)
        for issue_id, explanation, action_text in READINESS_ISSUES:
            row = QHBoxLayout()
            issue_label = QLabel(explanation)
            issue_label.setWordWrap(True)
            row.addWidget(issue_label, 1)
            action = QPushButton(action_text)
            action.clicked.connect(
                lambda checked=False, key=issue_id: self.issue_requested.emit(key)
            )
            row.addWidget(action)
            readiness_layout.addLayout(row)
        layout.addWidget(readiness)

        technical = QGroupBox("Technical scope (advanced)")
        technical.setCheckable(True)
        technical.setChecked(False)
        technical_layout = QFormLayout(technical)
        compiled_scope = QLabel("Unavailable: example inputs have not passed readiness")
        compiled_scope.setWordWrap(True)
        source_state = QLabel("A real compiled hash would be tied to exact reviewed inputs")
        source_state.setWordWrap(True)
        technical_layout.addRow("Compiled scope", compiled_scope)
        technical_layout.addRow("Source state", source_state)
        layout.addWidget(technical)

        monitor = QGroupBox("Run monitor - no collection is running")
        monitor_layout = QGridLayout(monitor)
        monitor_layout.addWidget(QLabel("--:--:-- elapsed"), 0, 0)
        monitor_layout.addWidget(QLabel("No segment"), 0, 1)
        monitor_layout.addWidget(QLabel("Errors --"), 0, 2)
        monitor_layout.addWidget(QLabel("Heartbeat --"), 0, 3)
        for row, (name, value, text) in enumerate(
            (
                ("Wheel-speed change coverage", 0, "Waiting for a run"),
                ("Coolant elapsed coverage", 0, "Waiting for a run"),
                ("Brake-switch state coverage", 0, "Waiting for a run"),
            ),
            start=1,
        ):
            monitor_layout.addWidget(QLabel(name), row, 0)
            progress = QProgressBar()
            progress.setValue(value)
            progress.setFormat(text)
            monitor_layout.addWidget(progress, row, 1, 1, 3)
        monitor.setMaximumHeight(230)
        layout.addWidget(monitor)
        layout.addStretch(1)
        start = QPushButton("Review and start collection")
        start.setObjectName("start-live")
        layout.addWidget(disabled_action(start, LIVE_DISABLED, live=True))


class EvidenceView(QWidget):
    SOURCE_POINTS = 1_000_000

    def __init__(self) -> None:
        super().__init__()
        self._controller = LatestTaskController(parent=self)
        self._controller.result.connect(self._set_result)
        self._controller.failed.connect(self._set_failure)
        self._controller.busy_changed.connect(self._set_busy)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self.refresh)
        self._x = np.arange(self.SOURCE_POINTS, dtype=np.float64) / 100.0
        self._y = (
            np.sin(self._x * 0.017) * 18.0
            + np.sin(self._x * 0.29) * 2.5
            + np.cos(self._x * 0.003) * 6.0
        )

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter = splitter
        selector = QWidget()
        selector_layout = QVBoxLayout(selector)
        self.signal_search = QLineEdit()
        self.signal_search.setPlaceholderText("Find a signal")
        selector_layout.addWidget(self.signal_search)
        self.signals = QListWidget()
        self._signal_names = tuple(EXAMPLE_TARGETS)
        self.signals.addItems(
            tuple(f"{name}\n{EXAMPLE_TARGETS[name][6]}" for name in self._signal_names)
        )
        self.signals.setCurrentRow(1)
        selector_layout.addWidget(self.signals, 1)
        self.signals.currentRowChanged.connect(self._signal_changed)
        self.signal_search.textChanged.connect(self._filter_signals)
        splitter.addWidget(selector)

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        content_layout = QVBoxLayout(content)
        filters = QGridLayout()
        self.run_filter = QComboBox()
        self.run_filter.addItems(("All recordings",) + EXAMPLE_RUNS)
        self.run_filter.setMinimumContentsLength(14)
        self.run_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.time_filter = QComboBox()
        self.time_filter.addItems(("All time", "First half", "Second half"))
        self.quality_filter = QComboBox()
        self.quality_filter.addItems(("All outcomes", "Valid only", "Errors only"))
        filters.addWidget(QLabel("Runs"), 0, 0)
        filters.addWidget(self.run_filter, 0, 1, 1, 3)
        filters.addWidget(QLabel("Time"), 1, 0)
        filters.addWidget(self.time_filter, 1, 1)
        filters.addWidget(QLabel("Quality"), 1, 2)
        filters.addWidget(self.quality_filter, 1, 3)
        self.run_filter.currentIndexChanged.connect(self.refresh)
        self.time_filter.currentIndexChanged.connect(self.refresh)
        self.quality_filter.currentIndexChanged.connect(self.refresh)
        filters.setColumnStretch(4, 1)
        content_layout.addLayout(filters)
        toolbar = QGridLayout()
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset_view)
        previous = QPushButton("Previous run")
        previous.clicked.connect(lambda: self._step_run(-1))
        next_run = QPushButton("Next run")
        next_run.clicked.connect(lambda: self._step_run(1))
        select_range = QPushButton("Select range")
        disable_preview(select_range, "Range selection is not simulated in the offline plot preview.")
        autoscale = QPushButton("Autoscale Y")
        autoscale.clicked.connect(self._autoscale_y)
        for column, button in enumerate((reset, previous, next_run, autoscale)):
            toolbar.addWidget(button, 0, column)
        self.quality_toggle = QCheckBox("Show quality lane")
        self.quality_toggle.setChecked(True)
        toolbar.addWidget(select_range, 1, 0)
        toolbar.addWidget(self.quality_toggle, 1, 1)
        export = QPushButton("Export visible data")
        disable_preview(export, PREVIEW_DISABLED)
        toolbar.addWidget(export, 1, 2)
        toolbar.setColumnStretch(3, 1)
        content_layout.addLayout(toolbar)
        unavailable = QLabel(
            "Unavailable in this preview: range selection and export require loaded recordings."
        )
        unavailable.setWordWrap(True)
        unavailable.setProperty("muted", True)
        content_layout.addWidget(unavailable)
        self.status = QLabel("Waiting to build synthetic recording view...")
        self.status.setWordWrap(True)
        content_layout.addWidget(self.status)
        self.selection_status = QLabel("Selected: Coolant temperature | all three example runs")
        self.selection_status.setWordWrap(True)
        content_layout.addWidget(self.selection_status)
        self.plot = MetricPlot()
        self.plot.viewport_width_changed.connect(self._schedule_refresh)
        content_layout.addWidget(self.plot, 1)
        self.cursor_status = QLabel("Move the pointer over the plot to inspect elapsed time and value.")
        self.cursor_status.setWordWrap(True)
        self.plot.cursor_changed.connect(self.cursor_status.setText)
        content_layout.addWidget(self.cursor_status)
        self.quality_message = QLabel("Quality overlay: red marks indicate example error intervals.")
        self.quality_message.setWordWrap(True)
        content_layout.addWidget(self.quality_message)
        self.quality_toggle.toggled.connect(self._quality_toggled)
        self.quality_filter.currentIndexChanged.connect(self._update_quality_message)
        splitter.addWidget(content)
        splitter.setSizes((260, 740))
        layout.addWidget(splitter, 1)
        QTimer.singleShot(0, self.refresh)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        narrow = event.size().width() < 980
        orientation = Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes((230, 620) if narrow else (260, 740))

    def _schedule_refresh(self, _width: int) -> None:
        self._debounce.start()

    def refresh(self) -> None:
        width = max(100, int(self.plot.getPlotItem().getViewBox().width()))
        x = self._x
        row = self.signals.currentRow()
        name = self._signal_names[max(row, 0)]
        unit = EXAMPLE_TARGETS[name][0]
        if name in ("Coolant validity", "Brake switch"):
            y = np.where(np.sin(x * 0.013) > 0, 1.0, 0.0)
        elif name == "Coolant temperature":
            y = 82.0 + self._y * 0.35
        elif name.startswith("Wheel speed"):
            y = np.maximum(0.0, 48.0 + self._y * 1.4)
        elif name == "Battery current":
            y = self._y * 3.0
        elif name == "Vehicle identifier":
            y = np.ones_like(x)
        else:
            y = self._y
        self.plot.set_measurement(name, unit)
        if self.run_filter.currentIndex() > 0:
            part = self.run_filter.currentIndex() - 1
            start = part * len(x) // 3
            end = (part + 1) * len(x) // 3
            x, y = x[start:end], y[start:end]
        if self.time_filter.currentIndex() == 1:
            x, y = x[: len(x) // 2], y[: len(y) // 2]
        elif self.time_filter.currentIndex() == 2:
            x, y = x[len(x) // 2 :], y[len(y) // 2 :]
        if self.quality_filter.currentIndex() > 0:
            example_error = np.arange(len(x)) % 97 == 0
            keep = ~example_error if self.quality_filter.currentIndex() == 1 else example_error
            x, y = x[keep], y[keep]
        self._controller.start(
            lambda cancel: bounded_minmax(x, y, width, cancelled=cancel.is_set)
        )

    def _filter_signals(self, needle: str) -> None:
        for row in range(self.signals.count()):
            self.signals.item(row).setHidden(needle.casefold() not in self.signals.item(row).text().casefold())

    def _signal_changed(self, row: int) -> None:
        if row < 0:
            return
        name = self.signals.item(row).text().splitlines()[0]
        self.selection_status.setText(f"Selected: {name} | {self.run_filter.currentText()} | synthetic plot")
        self.refresh()

    def _quality_toggled(self, checked: bool) -> None:
        self.quality_message.setVisible(checked)
        self.plot.set_quality_visible(checked)

    def _step_run(self, direction: int) -> None:
        current = self.run_filter.currentIndex()
        self.run_filter.setCurrentIndex(max(1, min(3, current + direction if current else 1)))

    def _reset_view(self) -> None:
        self.run_filter.setCurrentIndex(0)
        self.time_filter.setCurrentIndex(0)
        self.quality_filter.setCurrentIndex(0)
        self.plot.autoRange()
        self.refresh()

    def _autoscale_y(self) -> None:
        self.plot.getPlotItem().enableAutoRange(axis="y")

    def _update_quality_message(self, _index: int) -> None:
        self.quality_message.setText(
            f"Example quality lane: {self.quality_filter.currentText().lower()} "
            "(synthetic display; no recording query)"
        )

    def cancel(self) -> None:
        self._debounce.stop()
        self._controller.cancel()

    def _set_result(self, value: object) -> None:
        if not isinstance(value, BoundedSeries):
            self._set_failure("Unexpected evidence result")
            return
        self.plot.set_series(value)
        self.status.setText("Showing bounded example data; no recording has been loaded.")

    def _set_failure(self, detail: str) -> None:
        self.status.setText(f"Could not build recording preview: {detail.splitlines()[-1]}")

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.status.setText("Updating the example plot...")


class AnalyzeRecordingsPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(
            page_title(
                "Analyze recordings",
                "Plot all valid runs by default; filters change only what is displayed.",
            )
        )
        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.evidence_view = EvidenceView()
        self.tabs.addTab(self.evidence_view, "Signal analysis")
        self.tabs.addTab(self._run_details(), "Run details")
        self.evidence_view.run_filter.currentIndexChanged.connect(self._sync_run_detail)
        self.run_chooser.currentIndexChanged.connect(self._sync_analysis_run)
        layout.addWidget(self.tabs, 1)

    def _run_details(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.run_chooser = QComboBox()
        self.run_chooser.addItems(EXAMPLE_RUNS)
        self.run_chooser.setMinimumContentsLength(18)
        self.run_chooser.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.run_chooser)
        summary = QGroupBox("Run summary")
        summary_form = QFormLayout(summary)
        self.run_endpoint = QLabel()
        self.run_result = QLabel()
        self.run_settings = QLabel()
        self.run_hash = QLabel()
        for value in (self.run_endpoint, self.run_result, self.run_settings, self.run_hash):
            value.setWordWrap(True)
        summary_form.addRow("Endpoints", self.run_endpoint)
        summary_form.addRow("Result / duration", self.run_result)
        summary_form.addRow("Settings snapshot", self.run_settings)
        summary_form.addRow("Compiled hash", self.run_hash)
        layout.addWidget(summary)
        self.segment_table = _table(
                ("Segment", "Samples", "Valid", "Errors", "Storage"),
                (
                    ("1", "18,442", "18,401", "41", "7.8 MiB"),
                    ("2", "19,106", "19,072", "34", "8.0 MiB"),
                    ("3", "9,821", "9,814", "7", "4.2 MiB"),
                ),
                stretch_column=0,
                numeric_columns=(1, 2, 3, 4),
        )
        self.segment_table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.segment_table, 1)
        self.run_chooser.currentIndexChanged.connect(self._select_run)
        self._select_run(0)
        return page

    def _select_run(self, index: int) -> None:
        examples = (
            ("Powertrain + Hybrid control", "Stopped safely | 42 min 18 s", "PCAN_USBBUS1 | example snapshot", "Example hash A", 3),
            ("Hybrid control", "Completed | 18 min", "gs_usb | example snapshot", "Example hash B", 2),
            ("Powertrain", "Failed and finalized | 7 min", "J2534 | example snapshot", "Example hash C", 1),
        )
        endpoint, result, settings, digest, segments = examples[index]
        self.run_endpoint.setText(endpoint)
        self.run_result.setText(result)
        self.run_settings.setText(settings)
        self.run_hash.setText(digest + " - synthetic run; no capture loaded")
        for row in range(self.segment_table.rowCount()):
            self.segment_table.setRowHidden(row, row >= segments)

    def _sync_run_detail(self, index: int) -> None:
        if index > 0 and self.run_chooser.currentIndex() != index - 1:
            self.run_chooser.setCurrentIndex(index - 1)

    def _sync_analysis_run(self, index: int) -> None:
        target = index + 1
        if self.evidence_view.run_filter.currentIndex() != target:
            self.evidence_view.run_filter.setCurrentIndex(target)

    def cancel(self) -> None:
        self.evidence_view.cancel()
