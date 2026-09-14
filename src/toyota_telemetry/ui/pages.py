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
            ("Verify with\nvehicle", "18 pending", 3),
            ("Set collection\ntargets", "12 active", 4),
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
            ("Candidate signal list", "1,846 rows", "TIS import; 3 conflicts need review", "Review list", 2),
            ("Vehicle verification", "18 pending", "2 endpoints and 42 DIDs observed", "Verify", 3),
            ("Collection targets", "12 active", "3 supported signals need targets", "Set targets", 4),
            ("Collection readiness", "3 problems", "Missing timing and one condition input", "Resolve", 5),
            ("Recoverable runs", "None", "No unfinished capture segments", "Inspect", 6),
            ("Recordings", "2,105 runs", "Latest recording: two days ago", "Analyze", 6),
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
                f"{path.name} | {path} | Profile loading deferred | Adapter closed"
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
        layout.addWidget(QLabel("Worker available | PCAN | PCAN_USBBUS1 | Settings not validated"))

        settings = QGroupBox("Connector settings")
        form = QFormLayout(settings)
        self.connector = QComboBox()
        self.connector.addItems(("PCAN", "J2534", "gs_usb", "slcan"))
        form.addRow("Connector", self.connector)
        self.connector_settings = QStackedWidget()
        specifications = (
            ("Channel", ("PCAN_USBBUS1", "PCAN_USBBUS2", "PCAN_USBBUS3")),
            ("PassThru DLL", ("Select vendor DLL...",)),
            ("USB identity", ("VID 0x1D50 / PID 0x606F",)),
            ("Serial port", ("COM3", "COM4", "Enter another port...")),
        )
        for label, values in specifications:
            panel = QWidget()
            panel_form = QFormLayout(panel)
            field = QComboBox()
            field.addItems(values)
            panel_form.addRow(label, field)
            self.connector_settings.addWidget(panel)
        form.addRow("Settings", self.connector_settings)
        self.connector.currentIndexChanged.connect(self.connector_settings.setCurrentIndex)
        layout.addWidget(settings)
        self.feedback = QLabel("Validate checks fields, paths, and the worker self-test without opening the adapter.")
        layout.addWidget(self.feedback)
        validate = QPushButton("Validate settings")
        validate.clicked.connect(
            lambda: self.feedback.setText(
                "Preview validation passed: fields are well formed; adapter remains closed."
            )
        )
        save = QPushButton("Save local settings")
        disable_preview(save, PREVIEW_DISABLED)
        layout.addLayout(_button_row(validate, save))
        layout.addStretch(1)


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
        self.source.addItems(("TIS database", "Normalized file", "J2534 / Techstream dump (future)"))
        source_row.addWidget(QLabel("Source"))
        source_row.addWidget(self.source)
        source_row.addStretch(1)
        layout.addLayout(source_row)
        self.source_settings = QStackedWidget()

        tis = QGroupBox("TIS database selection")
        tis_form = QFormLayout(tis)
        database = QLineEdit()
        database.setPlaceholderText("Path to the locally available TIS database")
        model = QComboBox()
        model.addItems(("RAV4 HV", "Select another model..."))
        year = QComboBox()
        year.addItems(tuple(str(value) for value in range(2026, 2017, -1)))
        year.setCurrentText("2023")
        tis_form.addRow("Database", database)
        tis_form.addRow("Model", model)
        tis_form.addRow("Model year", year)
        self.source_settings.addWidget(tis)

        normalized = QGroupBox("Normalized candidate file")
        file_form = QFormLayout(normalized)
        candidate_file = QLineEdit()
        candidate_file.setPlaceholderText("Path to normalized catalog JSON")
        file_form.addRow("File", candidate_file)
        self.source_settings.addWidget(normalized)

        future = QGroupBox("Future source")
        future_layout = QVBoxLayout(future)
        future_layout.addWidget(
            QLabel(
                "J2534 parsing utilities exist, but the reviewed candidate-source contract is not implemented."
            )
        )
        self.source_settings.addWidget(future)
        self.source.currentIndexChanged.connect(self.source_settings.setCurrentIndex)
        layout.addWidget(self.source_settings)

        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter candidate signals, ECUs, endpoints, or DIDs")
        controls.addWidget(self.search, 1)
        preview = QPushButton("Preview import")
        preview.clicked.connect(
            lambda: self.summary.setText(
                "Preview: 14 ECUs | 22 endpoint candidates | 612 DIDs | 1,846 signals | 3 conflicts | 0 rejected"
            )
        )
        controls.addWidget(preview)
        layout.addLayout(controls)
        self.summary = QLabel("No source preview has been run. The table below is synthetic scale data.")
        layout.addWidget(self.summary)

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
        layout.addWidget(self.table, 1)
        accept = QPushButton("Use this candidate list")
        disable_preview(accept, PREVIEW_DISABLED)
        layout.addLayout(_button_row(accept))


class VerifyVehiclePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(
            page_title(
                "Verify with vehicle",
                "Probe each required endpoint first, then preflight its selected DIDs and record baselines.",
            )
        )
        layout.addWidget(preview_notice("LIVE OPERATION PREVIEW - explicit finite scope; adapter remains closed."))
        filters = QHBoxLayout()
        for values in (
            ("All sources", "TIS database", "Imported file"),
            ("All ECUs", "Powertrain", "Hybrid control"),
            ("All states", "Pending", "Responded", "Unsupported"),
        ):
            combo = QComboBox()
            combo.addItems(values)
            filters.addWidget(combo)
        filters.addStretch(1)
        layout.addLayout(filters)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Endpoint / DID", "Selection", "State", "Baseline", "Bytes", "Latency", "Source"))
        examples = (
            (
                "Powertrain - 0x7E0 / 0x7E8 / default",
                "Confirmed endpoint",
                (
                    ("DID 0xF190  Vehicle identifier", "Selected", "Responded", "JT...", "17", "22 ms", "TIS"),
                    ("DID 0xF40D  Coolant temperature", "Selected", "Responded", "82 C", "2", "17 ms", "TIS"),
                    ("DID 0xF423  Brake switches", "Selected", "Pending", "-", "-", "-", "TIS"),
                ),
            ),
            (
                "Hybrid control - 0x7E2 / 0x7EA / extended",
                "Endpoint probe required",
                (
                    ("DID 0xF421  Wheel speeds", "Selected", "Waiting for endpoint", "-", "-", "-", "File"),
                    ("DID 0xF430  Battery current", "Not selected", "Not attempted", "-", "-", "-", "TIS"),
                ),
            ),
        )
        group_brush = self.palette().brush(QPalette.ColorRole.AlternateBase)
        for endpoint, state, dids in examples:
            parent = QTreeWidgetItem((endpoint, "All selected", state, "", "", "", "Candidate inventory"))
            for column in range(self.tree.columnCount()):
                parent.setBackground(column, group_brush)
            self.tree.addTopLevelItem(parent)
            for did in dids:
                parent.addChild(QTreeWidgetItem(did))
            parent.setExpanded(True)
        for column in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(column)
        layout.addWidget(self.tree, 1)
        layout.addWidget(
            QLabel(
                "Scope: PCAN_USBBUS1 | 2 endpoints | 4 selected DIDs | reviewed sessions | <= 12 s | provisional cache"
            )
        )
        verify = QPushButton("Verify 2 endpoints and 4 DIDs (Live)")
        verify.setObjectName("verify-live")
        disable_preview(verify, LIVE_DISABLED)
        layout.addLayout(_button_row(verify))


class CollectionTargetsPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(
            page_title(
                "Set collection targets",
                "Choose how much evidence is enough. Acquisition settings are shared by endpoint and DID.",
            )
        )
        toolbar = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Find a signal, endpoint, or DID")
        toolbar.addWidget(search, 1)
        state = QComboBox()
        state.addItems(("All targets", "Needs target", "Incomplete", "Complete"))
        toolbar.addWidget(state)
        batch = QPushButton("Batch edit collection targets...")
        toolbar.addWidget(batch)
        layout.addLayout(toolbar)

        splitter = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Endpoint / DID / signal", "Support", "Target", "Recorded progress"))
        groups = (
            (
                "Powertrain / DID 0xF40D",
                "Linked: 1 s target / 5 s maximum",
                (
                    ("Coolant temperature", "Supported", "Samples + elapsed span", "12 runs / 6 min remaining"),
                    ("Coolant validity", "Supported", "Required states", "2 of 3 states"),
                ),
            ),
            (
                "Hybrid control / DID 0xF421",
                "Linked: 100 ms target / 250 ms maximum",
                (
                    ("Wheel speed FL", "Supported", "Meaningful changes", "82% complete"),
                    ("Wheel speed FR", "Supported", "Meaningful changes", "79% complete"),
                    ("Wheel speed RL", "Supported", "Meaningful changes", "81% complete"),
                    ("Wheel speed RR", "Supported", "Meaningful changes", "80% complete"),
                ),
            ),
        )
        group_brush = self.palette().brush(QPalette.ColorRole.AlternateBase)
        for identity, shared, signals in groups:
            parent = QTreeWidgetItem((identity, "Verified", shared, "DID baseline available"))
            parent.setToolTip(2, "Shared by all signals read from this endpoint and DID")
            for column in range(self.tree.columnCount()):
                parent.setBackground(column, group_brush)
            self.tree.addTopLevelItem(parent)
            for signal in signals:
                parent.addChild(QTreeWidgetItem(signal))
            parent.setExpanded(True)
        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(2)
        splitter.addWidget(self.tree)

        inspector = QWidget()
        inspector_layout = QVBoxLayout(inspector)
        definition = QGroupBox("Signal definition")
        definition_form = QFormLayout(definition)
        definition_form.addRow("Signal", QLabel("Wheel speed FL"))
        definition_form.addRow("Field", QLabel("bits 0..15, unsigned big-endian"))
        definition_form.addRow("Unit / formula", QLabel("km/h | raw * 0.01"))
        definition_form.addRow("Support / source", QLabel("Verified | TIS + vehicle baseline"))
        inspector_layout.addWidget(definition)

        target = QGroupBox("Collection target")
        target_form = QFormLayout(target)
        included = QCheckBox("Include in collection")
        included.setChecked(True)
        target_form.addRow("Enabled", included)
        condition = QComboBox()
        condition.addItems(("Always", "Vehicle moving", "ICE running", "Custom condition..."))
        condition.setCurrentText("Vehicle moving")
        target_form.addRow("Collect when", condition)
        target_form.addRow("Minimum samples", _spin(500))
        target_form.addRow("Minimum meaningful changes", _spin(120))
        target_form.addRow("Change deadband", _spin(1, " km/h"))
        shared_target = _spin(100, " ms")
        shared_target.setToolTip("Shared by all signals read from this endpoint and DID")
        target_form.addRow("Linked  DID target interval", shared_target)
        shared_maximum = _spin(250, " ms")
        shared_maximum.setToolTip("Shared by all signals read from this endpoint and DID")
        target_form.addRow("Linked  DID maximum interval", shared_maximum)
        inspector_layout.addWidget(target)

        progress = QGroupBox("Recorded progress")
        progress_form = QFormLayout(progress)
        progress_form.addRow("Valid samples", QLabel("386 / 500"))
        progress_form.addRow("Meaningful changes", QLabel("84 / 120"))
        progress_form.addRow("Runs", QLabel("8"))
        progress_form.addRow("Last value", QLabel("0.0 km/h, two days ago"))
        inspector_layout.addWidget(progress)
        splitter.addWidget(inspector)
        splitter.setSizes((620, 470))
        layout.addWidget(splitter, 1)
        self.feedback = QLabel(
            "No goal type is required: enable the completion requirements that describe enough evidence."
        )
        layout.addWidget(self.feedback)
        batch.clicked.connect(
            lambda: self.feedback.setText(
                "Batch preview: 6 signals, 3 unique DIDs, 4 unselected siblings affected by linked intervals."
            )
        )
        save = QPushButton("Save signal review and collection targets")
        disable_preview(save, PREVIEW_DISABLED)
        layout.addLayout(_button_row(save))


class CollectDataPage(QWidget):
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
                "Collection readiness: 3 example problems - two missing DID timings and one condition input."
            )
        )
        summary = QHBoxLayout()
        for name, value in (
            ("Unfinished targets", "7"),
            ("Endpoints / DIDs", "2 / 48"),
            ("Estimated load", "17%"),
            ("Storage bound", "8 GiB"),
        ):
            card = QGroupBox(name)
            card_layout = QVBoxLayout(card)
            card_layout.addWidget(QLabel(value), alignment=Qt.AlignmentFlag.AlignCenter)
            summary.addWidget(card)
        layout.addLayout(summary)

        readiness = QGroupBox("Problems to resolve")
        readiness_layout = QVBoxLayout(readiness)
        readiness_layout.addWidget(QLabel("DID 0xF423 - response timing has not been measured   [Verify with vehicle]"))
        readiness_layout.addWidget(QLabel("DID 0xF430 - response size has not been measured   [Verify with vehicle]"))
        readiness_layout.addWidget(QLabel("ICE-running condition - source signal needs review   [Set collection targets]"))
        layout.addWidget(readiness)

        technical = QGroupBox("Technical scope (advanced)")
        technical.setCheckable(True)
        technical.setChecked(False)
        technical_layout = QFormLayout(technical)
        technical_layout.addRow("Compiled scope", QLabel("48 transactions | 2 sessions | example hash 4a51...c9e2"))
        technical_layout.addRow("Source state", QLabel("Would be invalidated whenever relevant tracked inputs change"))
        layout.addWidget(technical)

        monitor = QGroupBox("Run monitor preview - not live")
        monitor_layout = QGridLayout(monitor)
        monitor_layout.addWidget(QLabel("00:42:18 elapsed"), 0, 0)
        monitor_layout.addWidget(QLabel("Segment 3 / 24 MiB"), 0, 1)
        monitor_layout.addWidget(QLabel("0.7% recent errors"), 0, 2)
        monitor_layout.addWidget(QLabel("Heartbeat 0.4 s"), 0, 3)
        for row, (name, value, text) in enumerate(
            (
                ("Wheel-speed change coverage", 82, "82%"),
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
        layout.addWidget(monitor, 1)
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
        search = QLineEdit()
        search.setPlaceholderText("Find a signal")
        selector_layout.addWidget(search)
        signals = QListWidget()
        signals.addItems(
            (
                "Coolant temperature\n12 runs - 6 min remaining",
                "Wheel speed FL\n8 runs - 36 changes remaining",
                "Wheel speed FR\n8 runs - 41 changes remaining",
                "Brake switch\n4 of 7 states seen",
                "Battery current\n21 runs - complete",
            )
        )
        signals.setCurrentRow(0)
        selector_layout.addWidget(signals, 1)
        layout.addWidget(selector, 3)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        filters = QHBoxLayout()
        for name, values in (
            ("Runs", ("All valid runs", "Latest run", "Choose...")),
            ("Time", ("All time", "Last hour", "Last 7 days")),
            ("Quality", ("All outcomes", "Valid only", "Errors only")),
        ):
            filters.addWidget(QLabel(name))
            combo = QComboBox()
            combo.addItems(values)
            filters.addWidget(combo)
        filters.addStretch(1)
        content_layout.addLayout(filters)
        toolbar = QHBoxLayout()
        for text in ("Reset", "Previous run", "Next run", "Select range", "Autoscale Y"):
            button = QPushButton(text)
            toolbar.addWidget(button)
            if text in ("Reset", "Autoscale Y"):
                button.clicked.connect(self.refresh)
        quality = QCheckBox("Show quality lane")
        quality.setChecked(True)
        toolbar.addWidget(quality)
        export = QPushButton("Export visible data")
        disable_preview(export, PREVIEW_DISABLED)
        toolbar.addWidget(export)
        toolbar.addStretch(1)
        content_layout.addLayout(toolbar)
        self.status = QLabel("Waiting to build synthetic recording view...")
        content_layout.addWidget(self.status)
        self.plot = MetricPlot()
        self.plot.viewport_width_changed.connect(self._schedule_refresh)
        content_layout.addWidget(self.plot, 1)
        content_layout.addWidget(
            QLabel("Cursor: 2026-09-11 14:22:04Z | Run 42 | 82.1 C | Valid | ICE-running condition true")
        )
        layout.addWidget(content, 7)
        QTimer.singleShot(0, self.refresh)

    def _schedule_refresh(self, _width: int) -> None:
        self._debounce.start()

    def refresh(self) -> None:
        width = max(100, int(self.plot.getPlotItem().getViewBox().width()))
        x = self._x
        y = self._y
        self._controller.start(
            lambda cancel: bounded_minmax(x, y, width, cancelled=cancel.is_set)
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
            f"{value.source_points:,} source points. Example run metadata."
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
                "Plot all valid runs by default. Scheduling detail is available only for troubleshooting.",
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
        chooser = QComboBox()
        chooser.addItems(
            (
                "2026-09-11 13:40 - Stopped safely - 42 min - road loop",
                "2026-09-09 09:12 - Completed - 18 min - stationary",
                "2026-09-07 16:03 - Failed and finalized - 7 min",
            )
        )
        layout.addWidget(chooser)
        summary = QGroupBox("Run summary")
        summary_form = QFormLayout(summary)
        summary_form.addRow("Endpoints", QLabel("7E0/7E8 and 7E2/7EA"))
        summary_form.addRow("Result / duration", QLabel("Stopped safely | 42 min 18 s"))
        summary_form.addRow("Settings snapshot", QLabel("PCAN_USBBUS1 | 48 DIDs | immutable"))
        summary_form.addRow("Compiled hash", QLabel("4a51...c9e2 (example)"))
        layout.addWidget(summary)
        diagnostics = QGroupBox("Why was this sampled? (advanced scheduling diagnostics)")
        diagnostics.setCheckable(True)
        diagnostics.setChecked(False)
        diagnostics_layout = QVBoxLayout(diagnostics)
        diagnostics_layout.addWidget(
            _table(
                ("Time", "DID", "Decision", "Reason", "Remaining", "Outcome"),
                (
                    ("00:00.100", "F421", "Selected", "Maximum interval due", "36 changes", "Positive"),
                    ("00:00.124", "F40D", "Deferred", "Not due for 876 ms", "6 min", "-"),
                    ("00:00.148", "F423", "Selected", "Required state still unseen", "3 states", "Timeout"),
                ),
            )
        )
        layout.addWidget(diagnostics, 1)
        return page

    def cancel(self) -> None:
        self.evidence_view.cancel()
