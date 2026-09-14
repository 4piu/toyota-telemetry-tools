"""Stable application shell for the offline desktop workflow prototype."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
)

from .pages import (
    AnalyzeRecordingsPage,
    BuildSignalListPage,
    CollectionTargetsPage,
    CollectDataPage,
    ConnectHardwarePage,
    OverviewPage,
    VerifyVehiclePage,
)


class OperatorWindow(QMainWindow):
    NAVIGATION = (
        "Overview",
        "Connect hardware",
        "Build signal list",
        "Verify with vehicle",
        "Set collection targets",
        "Collect data",
        "Analyze recordings",
    )

    def __init__(self, project_path: Path | None = None, *, restore_settings: bool = True) -> None:
        super().__init__()
        self.setObjectName("operator-window")
        self.setWindowTitle("Telemetry Operator")
        self.resize(1440, 900)
        self.setMinimumSize(1024, 700)
        self._settings = QSettings("70Y074", "TelemetryOperator")
        self._restore_settings = restore_settings
        self._project_path: Path | None = None

        self.navigation = QListWidget()
        self.navigation.setObjectName("workflow-navigation")
        self.navigation.setAccessibleName("Workflow navigation")
        self.navigation.setMinimumWidth(150)
        self.navigation.setMaximumWidth(230)
        for name in self.NAVIGATION:
            QListWidgetItem(name, self.navigation)

        self.pages = QStackedWidget()
        self.overview_page = OverviewPage()
        self.connect_page = ConnectHardwarePage()
        self.signal_list_page = BuildSignalListPage()
        self.verify_page = VerifyVehiclePage()
        self.targets_page = CollectionTargetsPage()
        self.collect_page = CollectDataPage()
        self.analyze_page = AnalyzeRecordingsPage()
        for page in (
            self.overview_page,
            self.connect_page,
            self.signal_list_page,
            self.verify_page,
            self.targets_page,
            self.collect_page,
            self.analyze_page,
        ):
            self.pages.addWidget(page)

        inspector = QFrame()
        inspector.setObjectName("inspector")
        inspector.setFrameShape(QFrame.Shape.StyledPanel)
        inspector.setMinimumWidth(220)
        inspector.setMaximumWidth(360)
        inspector_layout = QVBoxLayout(inspector)
        self.inspector_title = QLabel("Overview")
        title_font = self.inspector_title.font()
        title_font.setBold(True)
        self.inspector_title.setFont(title_font)
        self.inspector_body = QLabel(
            "This prototype is fully offline. It previews the intended controls and "
            "information architecture without loading or changing project authority."
        )
        self.inspector_body.setWordWrap(True)
        inspector_layout.addWidget(self.inspector_title)
        inspector_layout.addWidget(self.inspector_body)
        inspector_layout.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("main-splitter")
        splitter.addWidget(self.navigation)
        splitter.addWidget(self.pages)
        splitter.addWidget(inspector)
        splitter.setSizes([180, 970, 290])
        splitter.setStretchFactor(1, 1)
        self._splitter = splitter
        self.setCentralWidget(splitter)

        self.navigation.currentRowChanged.connect(self._navigate)
        self.navigation.setCurrentRow(0)
        self.overview_page.choose_project.connect(self.choose_project)
        self.overview_page.navigate_requested.connect(self.navigation.setCurrentRow)
        self._build_actions()
        self._build_status_bar()

        if project_path is not None:
            self.select_project(project_path)
        if restore_settings:
            geometry = self._settings.value("window/geometry")
            if geometry is not None:
                self.restoreGeometry(geometry)
            state = self._settings.value("window/splitter")
            if state is not None:
                self._splitter.restoreState(state)

    @property
    def project_path(self) -> Path | None:
        return self._project_path

    def _build_actions(self) -> None:
        project_menu = self.menuBar().addMenu("&Project")
        open_action = QAction("Open existing project…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.choose_project)
        project_menu.addAction(open_action)
        project_menu.addSeparator()
        close_action = QAction("Exit", self)
        close_action.setShortcut(QKeySequence.StandardKey.Quit)
        close_action.triggered.connect(self.close)
        project_menu.addAction(close_action)

        for index in range(len(self.NAVIGATION)):
            shortcut = QShortcut(QKeySequence(f"Alt+{index + 1}"), self)
            shortcut.activated.connect(lambda row=index: self.navigation.setCurrentRow(row))

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        self.setStatusBar(status)
        self.transport_status = QLabel("Offline")
        self.adapter_status = QLabel("Adapter closed")
        self.project_status = QLabel("No project open")
        for widget in (self.transport_status, self.adapter_status, self.project_status):
            status.addPermanentWidget(widget)

    def choose_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Open existing telemetry project")
        if selected:
            self.select_project(Path(selected))

    def select_project(self, path: Path) -> bool:
        resolved = path.resolve()
        marker = resolved / "telemetry-project.toml"
        if not marker.is_file():
            self.statusBar().showMessage(
                f"Not a telemetry project: {resolved} has no telemetry-project.toml", 8000
            )
            return False
        self._project_path = resolved
        self.overview_page.set_project(resolved)
        self.project_status.setText(resolved.name)
        self.setWindowTitle(f"Telemetry Operator — {resolved.name}")
        self.statusBar().showMessage(
            "Project marker found. Preview data remains synthetic; project loading is not implemented.", 6000
        )
        return True

    def _navigate(self, row: int) -> None:
        if not 0 <= row < self.pages.count():
            return
        self.pages.setCurrentIndex(row)
        name = self.NAVIGATION[row]
        self.inspector_title.setText(name)
        descriptions = {
            "Overview": "Readiness cards distinguish evidence, status, and the next action. All counts are examples.",
            "Connect hardware": "Only machine-local connector settings belong here. Vehicle endpoints and DIDs are verified later.",
            "Build signal list": "Source adapters normalize TIS, file, or future log input into one provisional inventory with provenance.",
            "Verify with vehicle": "A finite live job probes each required endpoint before preflighting its selected DIDs.",
            "Set collection targets": "DID acquisition settings are linked. Signal definitions, completion requirements, and recorded progress are visually separate.",
            "Collect data": "Readiness compilation is automatic. Scheduler detail stays collapsed while overall progress and safe stop remain prominent.",
            "Analyze recordings": "All valid runs are visible by default. Advanced run detail can explain past scheduler decisions when needed.",
        }
        self.inspector_body.setText(descriptions[name])

    def closeEvent(self, event: QCloseEvent) -> None:
        self.analyze_page.cancel()
        if self._restore_settings:
            self._settings.setValue("window/geometry", self.saveGeometry())
            self._settings.setValue("window/splitter", self._splitter.saveState())
        super().closeEvent(event)
