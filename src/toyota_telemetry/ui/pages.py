"""Offline workflow mockups for the desktop operator application."""
from __future__ import annotations

from pathlib import Path
import threading
import numpy as np
from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QStackedWidget, QTabWidget,
    QTableView, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)
from toyota_telemetry.operator import BoundedSeries, bounded_minmax
from .models import SyntheticSignalModel
from .tasks import LatestTaskController
from .widgets import MetricPlot, ReadinessCard, disable_preview, page_title, preview_notice

PREVIEW_DISABLED = "Preview only: project services are not connected."
LIVE_DISABLED = "Preview only: no adapter is opened and nothing is transmitted."


def _buttons(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout(); row.addStretch(1)
    for button in buttons: row.addWidget(button)
    return row


def _table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers)); table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True); table.verticalHeader().setVisible(False)
    for r, row in enumerate(rows):
        for c, value in enumerate(row): table.setItem(r, c, QTableWidgetItem(value))
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return table


class HomePage(QWidget):
    choose_project = Signal(); navigate_requested = Signal(int)

    def __init__(self, project_path: Path | None = None) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24)
        layout.addWidget(page_title("Telemetry workspace", "Readiness and the next useful action."))
        self.project_label = QLabel(); self.set_project(project_path); layout.addWidget(self.project_label)
        rail = QFrame(); rail.setFrameShape(QFrame.Shape.StyledPanel); rail_layout = QHBoxLayout(rail)
        for phase, state, destination in (("Prepare", "Needs attention", 1), ("Curate", "Current", 2),
            ("Plan", "Needs attention", 3), ("Collect", "Blocked", 4), ("Evidence", "Available", 5)):
            button = QPushButton(f"{phase}\n{state}")
            button.clicked.connect(lambda checked=False, page=destination: self.navigate_requested.emit(page))
            rail_layout.addWidget(button)
        layout.addWidget(rail)
        layout.addWidget(preview_notice("Recommended next action - Prepare: configure and validate a local connector. Example readiness data is shown."))
        grid = QGridLayout(); self.cards = []
        definitions = (("Local connection", "Needs setup", "No connector settings saved", "Configure", 1),
            ("Endpoints", "2 confirmed", "4 candidate addresses available", "Review", 1),
            ("DID preflight", "18 pending", "42 of 60 candidates observed", "Preflight", 1),
            ("Signal review", "76% reviewed", "456 of 600 signals reviewed", "Curate", 2),
            ("Collection goals", "12 active", "3 signals have no goal", "Edit goals", 2),
            ("Current plan", "Out of date", "Goal draft changed after validation", "Rebuild", 3),
            ("Recoverable runs", "None", "No unfinished capture segments", "Inspect", 6),
            ("Evidence", "2,105 runs", "Latest capture: 2 days ago", "Browse", 5))
        for i, (name, status, evidence, action, destination) in enumerate(definitions):
            card = ReadinessCard(name, status, evidence, action)
            card.action.clicked.connect(lambda checked=False, page=destination: self.navigate_requested.emit(page))
            grid.addWidget(card, i // 4, i % 4); self.cards.append(card)
        layout.addLayout(grid)
        layout.addStretch(1)
        open_button = QPushButton("Open existing project"); open_button.clicked.connect(self.choose_project)
        layout.addLayout(_buttons(open_button))

    def set_project(self, path: Path | None) -> None:
        self.project_label.setText("Preview project | No project open | Profile: example | Refreshed just now" if path is None
            else f"{path.name} | {path} | Profile: loading deferred | Refreshed just now")


class PreparePrototypePage(QWidget):
    def __init__(self) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Prepare", "Configure the connector, discover explicit endpoints, and preflight DIDs."))
        layout.addWidget(preview_notice("OFFLINE PREVIEW - adapter closed; example facts only."))
        self.tabs = QTabWidget(); self.tabs.addTab(self._connection(), "Connection")
        self.tabs.addTab(self._endpoints(), "Endpoints"); self.tabs.addTab(self._dids(), "DIDs")
        layout.addWidget(self.tabs, 1)

    def _connection(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Worker available | PCAN | PCAN_USBBUS1 | Adapter closed"))
        box = QGroupBox("Local connector settings"); form = QFormLayout(box)
        self.connector = QComboBox(); self.connector.addItems(("PCAN", "J2534", "gs_usb", "slcan")); form.addRow("Connector", self.connector)
        self.connector_settings = QStackedWidget()
        specifications = (("Channel", ("PCAN_USBBUS1", "PCAN_USBBUS2", "PCAN_USBBUS3")),
            ("PassThru DLL", ("Select vendor DLL...",)), ("USB identity", ("VID 0x1D50 / PID 0x606F",)),
            ("Serial port", ("COM3", "COM4", "Enter another port...")))
        for label, values in specifications:
            panel = QWidget(); panel_form = QFormLayout(panel); field = QComboBox(); field.addItems(values)
            panel_form.addRow(label, field); self.connector_settings.addWidget(panel)
        form.addRow("Settings", self.connector_settings); self.connector.currentIndexChanged.connect(self.connector_settings.setCurrentIndex)
        layout.addWidget(box); self.connection_feedback = QLabel("Settings have not been validated."); layout.addWidget(self.connection_feedback)
        validate = QPushButton("Validate settings")
        validate.clicked.connect(lambda: self.connection_feedback.setText("Preview validation passed: fields are well formed; no device was opened."))
        save = QPushButton("Save local settings"); disable_preview(save, PREVIEW_DISABLED)
        layout.addLayout(_buttons(validate, save)); layout.addStretch(1); return page

    def _endpoints(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); split = QSplitter()
        candidates = QGroupBox("Explicit candidates"); cl = QVBoxLayout(candidates)
        cl.addWidget(_table(("Tester ID", "Response ID", "Session", "Validation"),
            (("0x7E0", "0x7E8", "default", "Ready"), ("0x7E2", "0x7EA", "extended", "Ready"), ("0x750", "0x758", "default", "Check IDs"))))
        results = QGroupBox("Discovery results"); rl = QVBoxLayout(results)
        rl.addWidget(_table(("State", "Seen", "Latency", "Detail"),
            (("Responded", "Yesterday", "18 ms", "Session available"), ("Silent", "Never", "-", "Timed out"), ("Not attempted", "-", "-", "Needs review"))))
        split.addWidget(candidates); split.addWidget(results); layout.addWidget(split, 1)
        add = QPushButton("Add candidate"); paste = QPushButton("Paste rows")
        discover = QPushButton("Discover 3 explicit candidates (Live)"); discover.setObjectName("discover-live")
        confirm = QPushButton("Confirm and save endpoint")
        for button, reason in ((add, PREVIEW_DISABLED), (paste, PREVIEW_DISABLED), (discover, LIVE_DISABLED), (confirm, PREVIEW_DISABLED)): disable_preview(button, reason)
        layout.addLayout(_buttons(add, paste, discover, confirm)); return page

    def _dids(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); controls = QHBoxLayout()
        endpoint = QComboBox(); endpoint.addItems(("Powertrain - 7E0/7E8 (confirmed)", "Hybrid - 7E2/7EA (provisional)"))
        search = QLineEdit(); search.setPlaceholderText("Filter catalog or enter an explicit DID")
        controls.addWidget(QLabel("Endpoint")); controls.addWidget(endpoint); controls.addWidget(search, 1); layout.addLayout(controls)
        layout.addWidget(_table(("Use", "DID", "Name", "Outcome", "Baseline", "Bytes", "Latency"),
            (("Selected", "0xF190", "Vehicle identifier", "Responded", "JT...", "17", "22 ms"),
             ("Selected", "0xF40D", "Coolant temperature", "Responded", "82 C", "2", "17 ms"),
             ("Selected", "0xF421", "Wheel speeds", "Not attempted", "-", "-", "-"),
             ("", "0xF423", "Brake switches", "Negative response", "-", "3", "16 ms"))), 1)
        layout.addWidget(QLabel("Preflight tray: 3 DIDs | project safety range | 7E0/7E8 | default | <= 2.4 s"))
        preflight = QPushButton("Preflight 3 DIDs (Live)"); preflight.setObjectName("preflight-live"); disable_preview(preflight, LIVE_DISABLED)
        layout.addLayout(_buttons(preflight)); return page


class SignalPrototypePage(QWidget):
    def __init__(self) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout(); self.search = QLineEdit(); self.search.setPlaceholderText("Search signals, ECUs, endpoints, or DIDs"); controls.addWidget(self.search, 1)
        for values in (("All ECUs", "Powertrain", "Body"), ("All support", "Supported", "Unsupported"),
                       ("All review states", "Needs review", "Reviewed"), ("All goals", "Has goal", "No goal")):
            combo = QComboBox(); combo.addItems(values); controls.addWidget(combo)
        layout.addLayout(controls); actions = QHBoxLayout(); actions.addWidget(QLabel("10,000 signals; four signals per endpoint + DID group")); actions.addStretch(1)
        for text in ("Set review state", "Add provenance", "Create goals", "Columns"):
            button = QPushButton(text); disable_preview(button, PREVIEW_DISABLED); actions.addWidget(button)
        layout.addLayout(actions); self.model = SyntheticSignalModel(); self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model); self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1); self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.table = QTableView(); self.table.setModel(self.proxy); self.table.setSortingEnabled(True); self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False); self.table.setColumnWidth(0, 210); self.table.setColumnWidth(8, 125)
        self.table.setToolTip("Linked cadence is shared by all signals read from this endpoint + DID."); layout.addWidget(self.table, 1)


class GoalsPrototypePage(QWidget):
    def __init__(self) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout(); search = QLineEdit(); search.setPlaceholderText("Find a signal or goal"); toolbar.addWidget(search, 1)
        batch = QPushButton("Batch edit goals..."); toolbar.addWidget(batch); layout.addLayout(toolbar)
        split = QSplitter(); split.addWidget(_table(("Signal", "Evidence", "Remaining"),
            (("Coolant temperature", "12 runs", "6 minutes"), ("Wheel speed FL", "8 runs", "18 events"),
             ("Wheel speed FR", "8 runs", "18 events"), ("Brake switch", "4 states", "3 states"))))
        editor = QGroupBox("Goal editor - Wheel speed FL"); form = QFormLayout(editor)
        kind = QComboBox(); kind.addItems(("Descriptive", "Snapshot", "Switch / enum", "Fast-changing", "Slow trend", "Counter / history", "Exploration")); kind.setCurrentText("Fast-changing"); form.addRow("Goal type", kind)
        fields = (("Target", 500, " samples", False), ("Linked  Desired interval", 100, " ms", True),
                  ("Linked  Maximum interval", 250, " ms", True), ("Linked  Priority", 70, "", True))
        for label, value, suffix, linked in fields:
            field = QSpinBox(); field.setRange(1, 1_000_000); field.setValue(value); field.setSuffix(suffix)
            if linked: field.setToolTip("Shared by all signals read from this DID")
            form.addRow(label, field)
        included = QCheckBox("Include this goal when building a plan"); included.setChecked(True); form.addRow("Included", included)
        condition = QComboBox(); condition.addItems(("Always", "Vehicle moving", "ICE running", "Custom condition...")); form.addRow("Collect when", condition)
        expression = QLineEdit("vehicle_speed > 0"); expression.setReadOnly(True); form.addRow("Condition preview", expression)
        notes = QTextEdit(); notes.setMaximumHeight(60); notes.setPlaceholderText("Goal notes"); form.addRow("Notes", notes); split.addWidget(editor); split.setSizes((420, 620)); layout.addWidget(split, 1)
        self.feedback = QLabel("Linked fields edit the acquisition rule shared by sibling signals on this DID."); layout.addWidget(self.feedback)
        batch.clicked.connect(lambda: self.feedback.setText("Batch preview: 6 goals across 3 unique DIDs; shared fields update once per DID."))
        save = QPushButton("Save goals"); disable_preview(save, PREVIEW_DISABLED); layout.addLayout(_buttons(save))


class CuratePrototypePage(QWidget):
    def __init__(self) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Curate", "Review signal meaning and directly define collection goals. Linked fields are shared by DID."))
        self.tabs = QTabWidget(); self.signals_page = SignalPrototypePage(); self.goals_page = GoalsPrototypePage()
        self.tabs.addTab(self.signals_page, "Signals"); self.tabs.addTab(self.goals_page, "Goals"); layout.addWidget(self.tabs, 1); self.model = self.signals_page.model


class PlanPrototypePage(QWidget):
    def __init__(self) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Plan", "Build offline work from current goals, then review its exact scope."))
        controls = QHBoxLayout(); disk = QSpinBox(); disk.setRange(128, 1_000_000); disk.setValue(8192); disk.setSuffix(" MiB")
        self.build_button = QPushButton("Build plan"); controls.addWidget(QLabel("Available disk")); controls.addWidget(disk); controls.addStretch(1); controls.addWidget(self.build_button); layout.addLayout(controls)
        self.status = QLabel("Plan needs changes - example goals changed after the last validation."); layout.addWidget(self.status)
        summaries = QHBoxLayout()
        for name, value in (("Endpoints", "2"), ("Acquisitions", "48"), ("Objectives", "12"), ("Estimated bus", "17%"), ("Storage", "8 GiB"), ("Unfinished", "7")):
            box = QGroupBox(name); bl = QVBoxLayout(box); bl.addWidget(QLabel(value), alignment=Qt.AlignmentFlag.AlignCenter); summaries.addWidget(box)
        layout.addLayout(summaries); layout.addWidget(preview_notice("2 missing preflight timings | 1 condition input needs review | Storage and estimated load pass"))
        layout.addWidget(_table(("Endpoint", "DID / name", "Purpose", "Cadence", "Remaining", "Collect when", "Timing", "Cost"),
            (("7E0/7E8", "F40D Coolant", "Slow trend", "1 s / 5 s", "6 min", "ICE running", "Observed", "19 ms"),
             ("7E2/7EA", "F421 Wheel speeds", "Fast change", "100 / 250 ms", "18 events", "Moving", "Observed", "24 ms"),
             ("7E0/7E8", "F423 Brake switch", "Switch states", "250 ms / 1 s", "3 states", "Always", "Missing", "Estimate"))), 1)
        self.finish_review = QPushButton("Finish plan review"); disable_preview(self.finish_review, PREVIEW_DISABLED); layout.addLayout(_buttons(self.finish_review))
        self.build_button.clicked.connect(lambda: self.status.setText("Preview rebuilt offline - 3 diagnostics remain; no adapter opened and no file written."))


class CollectPrototypePage(QWidget):
    def __init__(self) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Collect", "Glanceable run progress and a permanently visible safe-stop control.")); layout.addWidget(preview_notice("PREVIEW - ADAPTER CLOSED - the example run below is not live."))
        top = QGroupBox("Would run: PCAN / PCAN_USBBUS1 / endpoint 7E0-7E8-default"); tl = QHBoxLayout(top)
        for text in ("00:42:18 elapsed", "Segment 3 / 24 MiB", "0.7% recent errors", "Heartbeat 0.4 s"): tl.addWidget(QLabel(text))
        self.stop_button = QPushButton("Stop safely"); disable_preview(self.stop_button, LIVE_DISABLED); tl.addWidget(self.stop_button); layout.addWidget(top)
        split = QSplitter(); goals = QGroupBox("Goal progress"); gl = QFormLayout(goals)
        for label, value, text in (("Wheel speed variation", 82, "82%"), ("Coolant slow change", 50, "6 / 12 minutes"), ("Switch states", 57, "4 / 7 seen")):
            bar = QProgressBar(); bar.setValue(value); bar.setFormat(text); gl.addRow(label, bar)
        split.addWidget(goals); transport = QGroupBox("Transport"); tr = QFormLayout(transport)
        for label, value in (("State", "connected / response pending (example)"), ("Current DID", "0xF421 Wheel speeds"), ("Latency", "23.8 ms"), ("Retry / backoff", "0 / none"), ("Last segment", "run-00042.segment-003.sqlite")): tr.addRow(label, QLabel(value))
        split.addWidget(transport); layout.addWidget(split, 1); bottom = QHBoxLayout(); bottom.addWidget(QCheckBox("Show transaction details")); bottom.addWidget(QLabel("Run note")); bottom.addWidget(QLineEdit("Road loop - warm engine"), 1)
        start = QPushButton("Start collection"); start.setObjectName("start-live"); disable_preview(start, LIVE_DISABLED); bottom.addWidget(start); layout.addLayout(bottom)


class EvidencePrototypePage(QWidget):
    SOURCE_POINTS = 1_000_000
    def __init__(self) -> None:
        super().__init__(); self._controller = LatestTaskController(parent=self); self._controller.result.connect(self._set_result); self._controller.failed.connect(self._set_failure); self._controller.busy_changed.connect(self._set_busy)
        self._debounce = QTimer(self); self._debounce.setSingleShot(True); self._debounce.setInterval(120); self._debounce.timeout.connect(self.refresh)
        self._x = np.arange(self.SOURCE_POINTS, dtype=np.float64) / 100.0; self._y = np.sin(self._x * .017) * 18 + np.sin(self._x * .29) * 2.5 + np.cos(self._x * .003) * 6
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 20, 24, 20); layout.addWidget(page_title("Evidence", "Compare all valid runs; filters only change presentation.")); split = QSplitter()
        selector = QWidget(); sl = QVBoxLayout(selector); search = QLineEdit(); search.setPlaceholderText("Find a signal"); sl.addWidget(search); signals = QListWidget(); signals.addItems(("Coolant temperature\n12 runs - 6 min remaining", "Wheel speed FL\n8 runs - 18 events remaining", "Wheel speed FR\n8 runs - 18 events remaining", "Brake switch\n4 of 7 states seen", "Battery current\n21 runs - complete")); signals.setCurrentRow(0); sl.addWidget(signals, 1); split.addWidget(selector)
        content = QWidget(); cl = QVBoxLayout(content); filters = QHBoxLayout()
        for name, values in (("Run range", ("All runs", "Latest run", "Choose...")), ("Time", ("All time", "Last hour", "Last 7 days")), ("Quality", ("All outcomes", "Valid only", "Errors only"))):
            filters.addWidget(QLabel(name)); combo = QComboBox(); combo.addItems(values); filters.addWidget(combo)
        filters.addStretch(1); cl.addLayout(filters); toolbar = QHBoxLayout()
        for text in ("Reset", "Previous run", "Next run", "Select range", "Autoscale Y"):
            button = QPushButton(text); toolbar.addWidget(button)
            if text in ("Reset", "Autoscale Y"): button.clicked.connect(self.refresh)
        quality = QCheckBox("Show quality lane"); quality.setChecked(True); toolbar.addWidget(quality); export = QPushButton("Export visible data"); disable_preview(export, PREVIEW_DISABLED); toolbar.addWidget(export); toolbar.addStretch(1); cl.addLayout(toolbar)
        self.status = QLabel("Waiting to build synthetic evidence view..."); cl.addWidget(self.status); self.plot = MetricPlot(); self.plot.viewport_width_changed.connect(self._schedule_refresh); cl.addWidget(self.plot, 1)
        cl.addWidget(QLabel("Cursor: 2026-09-11 14:22:04Z | Run 42 | 82.1 C | Valid | ICE-running condition true")); split.addWidget(content); split.setSizes((280, 760)); layout.addWidget(split, 1); QTimer.singleShot(0, self.refresh)
    def _schedule_refresh(self, _width: int) -> None: self._debounce.start()
    def refresh(self) -> None:
        width = max(100, int(self.plot.getPlotItem().getViewBox().width())); x, y = self._x, self._y
        self._controller.start(lambda cancel: bounded_minmax(x, y, width, cancelled=cancel.is_set))
    def cancel(self) -> None: self._debounce.stop(); self._controller.cancel()
    def _set_result(self, value: object) -> None:
        if not isinstance(value, BoundedSeries): self._set_failure("Unexpected evidence result"); return
        self.plot.set_series(value); self.status.setText(f"Rendered {value.rendered_points:,} representative points from {value.source_points:,} source points. Example run metadata.")
    def _set_failure(self, detail: str) -> None: self.status.setText(f"Could not build evidence preview: {detail.splitlines()[-1]}")
    def _set_busy(self, busy: bool) -> None:
        if busy: self.status.setText("Building the newest bounded viewport...")


class ReplayPrototypePage(QWidget):
    def __init__(self) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(page_title("Replay", "Explain scheduler choices using the immutable snapshot captured with a run.")); chooser = QComboBox(); chooser.addItems(("2026-09-11 13:40 - 7E0/7E8 - Stopped safely - road loop", "2026-09-09 09:12 - 7E2/7EA - Completed - stationary", "2026-09-07 16:03 - 7E0/7E8 - Failed and finalized")); layout.addWidget(chooser)
        split = QSplitter(Qt.Orientation.Vertical); split.addWidget(_table(("Time", "DID", "Decision", "Reason", "Priority", "Due", "Remaining", "Cost", "Outcome", "Segment"),
            (("00:00.100", "F421", "Selected", "Overdue", "70", "now", "18 events", "24 ms", "Positive", "1"), ("00:00.124", "F40D", "Deferred", "Not yet due", "40", "+876 ms", "6 min", "19 ms", "-", "1"), ("00:00.148", "F423", "Selected", "Condition true", "55", "now", "3 states", "21 ms", "Timeout", "1"))))
        detail = QGroupBox("Selected decision snapshot (read-only)"); dl = QFormLayout(detail); dl.addRow("Goal", QLabel("Observe wheel-speed variation across rotating tires")); dl.addRow("Acquisition", QLabel("7E2/7EA + 0xF421, desired 100 ms, maximum 250 ms")); dl.addRow("Plan hash", QLabel("4a51...c9e2 (example)")); split.addWidget(detail); layout.addWidget(split, 1)
