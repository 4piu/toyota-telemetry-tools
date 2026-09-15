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
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
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
from .mock_data import EXAMPLE_DIDS, EXAMPLE_ENDPOINTS, EXAMPLE_SIGNAL_COUNT, EXAMPLE_TARGETS, READINESS_ISSUES, REQUIRED_DIDS
from .tasks import LatestTaskController
from .widgets import MetricPlot, ReadinessCard, disable_preview, page_title, preview_notice


PREVIEW_DISABLED = "Preview only: project services are not connected."
LIVE_DISABLED = "Preview only: no adapter is opened and nothing is transmitted."


def _button_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    return row


def _table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            table.setItem(row_index, column, QTableWidgetItem(value))
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _spin(value: int, suffix: str = "") -> QSpinBox:
    control = QSpinBox()
    control.setRange(0, 1_000_000)
    control.setValue(value)
    control.setSuffix(suffix)
    return control


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

        rail = QFrame()
        rail.setFrameShape(QFrame.Shape.StyledPanel)
        rail_layout = QHBoxLayout(rail)
        phases = (
            ("Connect\nhardware", "Needs setup", 1),
            ("Build\nsignal list", "Current", 2),
            ("Verify with\nvehicle", "2 checks needed", 3),
            ("Set collection\ntargets", "Review needed", 4),
            ("Collect\ndata", "Not ready", 5),
            ("Analyze\nrecordings", "Available", 6),
        )
        for phase, status, destination in phases:
            button = QPushButton(f"{phase}\n{status}")
            button.clicked.connect(
                lambda checked=False, page=destination: self.navigate_requested.emit(page)
            )
            rail_layout.addWidget(button)
        layout.addWidget(rail)
        layout.addWidget(
            preview_notice(
                "Recommended next action - Connect hardware: validate a local connector. "
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
            grid.addWidget(card, index // 4, index % 4)
            self.cards.append(card)
        layout.addLayout(grid)
        layout.addStretch(1)
        open_button = QPushButton("Open existing project")
        open_button.clicked.connect(self.choose_project)
        layout.addLayout(_button_row(open_button))

    def set_project(self, path: Path | None) -> None:
        if path is None:
            self.project_label.setText(
                "Preview project | No project open | Profile: example | Adapter closed"
            )
        else:
            self.project_label.setText(
                f"{path.name} | {path} | Profile: example | Adapter closed"
            )


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
        self.connector_status = QLabel("Example connector: PCAN / PCAN_USBBUS1 | Adapter closed")
        layout.addWidget(self.connector_status)

        settings = QGroupBox("Connector settings")
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
        j2534_form.addRow("PassThru DLL", self.j2534_dll)
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
        self.connector.currentTextChanged.connect(
            lambda name: self.connector_status.setText(f"Example connector: {name} | Adapter closed")
        )
        layout.addWidget(settings)
        self.feedback = QLabel("Preview checks required fields only; driver and worker self-test are not run.")
        layout.addWidget(self.feedback)
        validate = QPushButton("Validate settings")
        validate.clicked.connect(self._validate_preview)
        save = QPushButton("Save local settings")
        disable_preview(save, PREVIEW_DISABLED)
        layout.addLayout(_button_row(validate, save))
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

        tis = QGroupBox("TIS database selection")
        tis_form = QFormLayout(tis)
        self.database = QLineEdit()
        self.database.setPlaceholderText("Path to the locally available TIS database")
        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText("Vehicle model")
        self.model_year = QComboBox()
        self.model_year.addItems(("Select year...",) + tuple(str(value) for value in range(2026, 2017, -1)))
        tis_form.addRow("Database", self.database)
        tis_form.addRow("Model", self.model_name)
        tis_form.addRow("Model year", self.model_year)
        self.source_settings.addWidget(tis)

        normalized = QGroupBox("Normalized candidate file")
        file_form = QFormLayout(normalized)
        self.candidate_file = QLineEdit()
        self.candidate_file.setPlaceholderText("Path to normalized catalog JSON")
        file_form.addRow("File", self.candidate_file)
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
        layout.addWidget(self.summary)

        self.tabs = QTabWidget()
        example_rows = tuple(
            (did.source, did.endpoint, f"0x{did.key}", signal, did.state)
            for did in EXAMPLE_DIDS for signal in did.signals
        )
        self.example_table = _table(("Example source", "ECU", "DID", "Signal", "Verification"), example_rows)
        self.tabs.addTab(self.example_table, "Example inventory")
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
        self.tabs.addTab(self.table, "10,000-row performance demo")
        layout.addWidget(self.tabs, 1)
        accept = QPushButton("Use this candidate list")
        disable_preview(accept, PREVIEW_DISABLED)
        layout.addLayout(_button_row(accept))

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
            "Verification facts are temporary local results until reviewed and saved."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        filters = QHBoxLayout()
        self.source_filter = QComboBox()
        self.source_filter.addItems(("All sources", "TIS database", "Normalized file"))
        self.ecu_filter = QComboBox()
        self.ecu_filter.addItems(("All ECUs", "Powertrain", "Hybrid control"))
        self.state_filter = QComboBox()
        self.state_filter.addItems(("All states", "Needs verification", "Responded", "Not attempted"))
        for combo in (self.source_filter, self.ecu_filter, self.state_filter):
            filters.addWidget(combo)
            combo.currentIndexChanged.connect(self._apply_filters)
        filters.addStretch(1)
        layout.addLayout(filters)

        self.tree = QTreeWidget()
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
        self.scope_label = QLabel()
        layout.addWidget(self.scope_label)
        self.verify_button = QPushButton()
        self.verify_button.setObjectName("verify-live")
        disable_preview(self.verify_button, LIVE_DISABLED)
        self.return_button = QPushButton("Return to collection readiness")
        self.return_button.setVisible(False)
        self.return_button.clicked.connect(self.return_to_collect)
        layout.addLayout(_button_row(self.return_button, self.verify_button))
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
                    and (state == "All states"
                         or (state == "Needs verification" and child_state in ("Pending", "Waiting for endpoint"))
                         or (state == "Responded" and child_state == "Responded")
                         or (state == "Not attempted" and child_state == "Not attempted"))
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
        self.verify_button.setText(f"Verify {endpoints} endpoints and {len(selected)} DIDs (Live)")

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
        self.return_button.setVisible(True)


class CollectionTargetsPage(QWidget):
    return_to_collect = Signal()

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
        disable_preview(batch, "Batch editing is not simulated in this offline preview.")
        toolbar.addWidget(batch)
        layout.addLayout(toolbar)

        splitter = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Include / endpoint / DID / signal", "Support", "Target", "Recorded progress"))
        group_brush = self.palette().brush(QPalette.ColorRole.AlternateBase)
        self.signal_items: dict[str, QTreeWidgetItem] = {}
        self.did_groups: dict[str, QTreeWidgetItem] = {}
        for did in EXAMPLE_DIDS:
            interval = "100 / 250 ms" if did.key == "F421" else "1 / 5 s"
            parent = QTreeWidgetItem((
                f"{did.endpoint} / DID 0x{did.key} {did.label}",
                did.state, interval, "Example group",
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
                child = QTreeWidgetItem((signal, did.state, status, coverage))
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
        details.setMinimumWidth(420)
        details_layout = QVBoxLayout(details)
        definition = QGroupBox("Signal definition")
        definition_form = QFormLayout(definition)
        self.definition_name = QLabel()
        self.definition_field = QLabel()
        self.definition_formula = QLabel()
        self.definition_source = QLabel()
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
        progress_form.addRow("Valid samples", self.progress_samples)
        progress_form.addRow("Changes", self.progress_changes)
        progress_form.addRow("Coverage", self.progress_coverage)
        progress_form.addRow("Last value", self.progress_last)
        details_layout.addWidget(progress)
        splitter.addWidget(details)
        splitter.setSizes((620, 470))
        layout.addWidget(splitter, 1)
        self.feedback = QLabel("Example draft only. Selection updates these details; Save is not connected.")
        layout.addWidget(self.feedback)
        self.condition.currentTextChanged.connect(self._draft_changed)
        for field in (self.minimum_samples, self.minimum_changes, self.minimum_difference):
            field.valueChanged.connect(self._draft_changed)
        self.did_target.valueChanged.connect(self._did_draft_changed)
        self.did_maximum.valueChanged.connect(self._did_draft_changed)
        save = QPushButton("Save signal review and collection targets")
        disable_preview(save, PREVIEW_DISABLED)
        self.return_button = QPushButton("Return to collection readiness")
        self.return_button.setVisible(False)
        self.return_button.clicked.connect(self.return_to_collect)
        layout.addLayout(_button_row(self.return_button, save))
        self.tree.setCurrentItem(self.signal_items["Wheel speed FL"])

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
                    status == "All targets" or child.text(2).startswith(status)
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
        item.setText(2, EXAMPLE_TARGETS[signal][-1] + " (draft)")
        self.feedback.setText(f"{signal}: local example draft changed; Save is not connected.")

    def _inclusion_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or item.parent() is None:
            return
        signal = item.data(0, Qt.ItemDataRole.UserRole)
        if signal:
            self.feedback.setText(
                f"{signal}: {'included' if item.checkState(0) == Qt.CheckState.Checked else 'excluded'} "
                "in this local example draft only."
            )

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

    def focus_issue(self, signal: str) -> None:
        item = self.signal_items[signal]
        self.search.clear()
        self.state_filter.setCurrentIndex(0)
        item.parent().setExpanded(True)
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
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
        summary = QHBoxLayout()
        for name, value in (
            ("Unfinished targets", "7"),
            ("Endpoints / DIDs", f"{len(EXAMPLE_ENDPOINTS)} / {len(REQUIRED_DIDS)}"),
            ("Estimated load", "Not checked"),
            ("Storage bound", "Not checked"),
        ):
            card = QGroupBox(name)
            card_layout = QVBoxLayout(card)
            card_layout.addWidget(QLabel(value), alignment=Qt.AlignmentFlag.AlignCenter)
            summary.addWidget(card)
        layout.addLayout(summary)

        readiness = QGroupBox("Problems to resolve")
        readiness_layout = QVBoxLayout(readiness)
        for issue_id, explanation, action_text in READINESS_ISSUES:
            row = QHBoxLayout()
            row.addWidget(QLabel(explanation), 1)
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
        technical_layout.addRow("Compiled scope", QLabel("Unavailable: example inputs have not passed readiness"))
        technical_layout.addRow("Source state", QLabel("A real compiled hash would be tied to exact reviewed inputs"))
        layout.addWidget(technical)

        monitor = QGroupBox("Run monitor preview - not live")
        monitor_layout = QGridLayout(monitor)
        monitor_layout.addWidget(QLabel("00:42:18 elapsed"), 0, 0)
        monitor_layout.addWidget(QLabel("Segment 3 / 24 MiB"), 0, 1)
        monitor_layout.addWidget(QLabel("0.7% recent errors"), 0, 2)
        monitor_layout.addWidget(QLabel("Heartbeat 0.4 s"), 0, 3)
        for row, (name, value, text) in enumerate(
            (
                ("Wheel-speed change coverage", 70, "84 / 120 changes"),
                ("Coolant elapsed coverage", 50, "6 / 12 minutes"),
                ("Brake-switch state coverage", 57, "4 / 7 states"),
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
        disable_preview(start, LIVE_DISABLED)
        stop = QPushButton("Stop safely")
        stop.setObjectName("stop-live")
        disable_preview(stop, LIVE_DISABLED)
        layout.addLayout(_button_row(start, stop))


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

        layout = QHBoxLayout(self)
        selector = QWidget()
        selector_layout = QVBoxLayout(selector)
        self.signal_search = QLineEdit()
        self.signal_search.setPlaceholderText("Find a signal")
        selector_layout.addWidget(self.signal_search)
        self.signals = QListWidget()
        self.signals.addItems(
            (
                "Coolant temperature\n12 runs - 6 min remaining",
                "Wheel speed FL\n8 runs - 36 changes remaining",
                "Wheel speed FR\n8 runs - 41 changes remaining",
                "Brake switch\n4 of 7 states seen",
                "Battery current\n21 runs - complete",
            )
        )
        self.signals.setCurrentRow(0)
        selector_layout.addWidget(self.signals, 1)
        self.signals.currentRowChanged.connect(self._signal_changed)
        self.signal_search.textChanged.connect(self._filter_signals)
        layout.addWidget(selector, 3)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        filters = QHBoxLayout()
        self.run_filter = QComboBox()
        self.run_filter.addItems(("All example runs", "Example run 1", "Example run 2", "Example run 3"))
        self.time_filter = QComboBox()
        self.time_filter.addItems(("All time", "First half", "Second half"))
        self.quality_filter = QComboBox()
        self.quality_filter.addItems(("All outcomes", "Valid only", "Errors only"))
        for name, combo in (("Runs", self.run_filter), ("Time", self.time_filter), ("Quality", self.quality_filter)):
            filters.addWidget(QLabel(name))
            filters.addWidget(combo)
        self.run_filter.currentIndexChanged.connect(self.refresh)
        self.time_filter.currentIndexChanged.connect(self.refresh)
        self.quality_filter.currentIndexChanged.connect(self.refresh)
        filters.addStretch(1)
        content_layout.addLayout(filters)
        toolbar = QHBoxLayout()
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
        for button in (reset, previous, next_run, select_range, autoscale):
            toolbar.addWidget(button)
        self.quality_toggle = QCheckBox("Show quality lane")
        self.quality_toggle.setChecked(True)
        toolbar.addWidget(self.quality_toggle)
        export = QPushButton("Export visible data")
        disable_preview(export, PREVIEW_DISABLED)
        toolbar.addWidget(export)
        toolbar.addStretch(1)
        content_layout.addLayout(toolbar)
        self.status = QLabel("Waiting to build synthetic recording view...")
        content_layout.addWidget(self.status)
        self.selection_status = QLabel("Selected: Coolant temperature | all three example runs")
        content_layout.addWidget(self.selection_status)
        self.plot = MetricPlot()
        self.plot.viewport_width_changed.connect(self._schedule_refresh)
        content_layout.addWidget(self.plot, 1)
        self.quality_message = QLabel("Example quality lane: valid observations and error intervals")
        content_layout.addWidget(self.quality_message)
        self.quality_toggle.toggled.connect(self.quality_message.setVisible)
        self.quality_filter.currentIndexChanged.connect(self._update_quality_message)
        layout.addWidget(content, 7)
        QTimer.singleShot(0, self.refresh)

    def _schedule_refresh(self, _width: int) -> None:
        self._debounce.start()

    def refresh(self) -> None:
        width = max(100, int(self.plot.getPlotItem().getViewBox().width()))
        x = self._x
        row = self.signals.currentRow()
        y = (
            np.where(np.sin(x * 0.013) > 0, 1.0, 0.0)
            if row == 3 else self._y * (1.0 if row == 0 else 0.25 + max(row, 0) * 0.15)
        )
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
        self.status.setText(
            f"Rendered {value.rendered_points:,} representative points from "
            f"{value.source_points:,} synthetic source points. No recording data was loaded."
        )

    def _set_failure(self, detail: str) -> None:
        self.status.setText(f"Could not build recording preview: {detail.splitlines()[-1]}")

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.status.setText("Building the newest bounded viewport...")


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
        self.evidence_view = EvidenceView()
        self.tabs.addTab(self.evidence_view, "Signal analysis")
        self.tabs.addTab(self._run_details(), "Run details")
        layout.addWidget(self.tabs, 1)

    def _run_details(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.run_chooser = QComboBox()
        self.run_chooser.addItems(
            (
                "2026-09-11 13:40 - Stopped safely - 42 min - road loop",
                "2026-09-09 09:12 - Completed - 18 min - stationary",
                "2026-09-07 16:03 - Failed and finalized - 7 min",
            )
        )
        layout.addWidget(self.run_chooser)
        summary = QGroupBox("Run summary")
        summary_form = QFormLayout(summary)
        self.run_endpoint = QLabel()
        self.run_result = QLabel()
        self.run_settings = QLabel()
        self.run_hash = QLabel()
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
        )
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

    def cancel(self) -> None:
        self.evidence_view.cancel()
