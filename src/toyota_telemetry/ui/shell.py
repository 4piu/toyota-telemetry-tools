"""Stable application shell for the offline desktop workflow prototype."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QStatusBar,
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
        self.setStyleSheet(
            "QPushButton[liveAction=\"true\"] { border-width: 2px; padding: 6px 12px; }"
        )
        self._settings = QSettings("70Y074", "TelemetryOperator")
        self._restore_settings = restore_settings
        self._project_path: Path | None = None

        self.navigation = QListWidget()
        self.navigation.setObjectName("workflow-navigation")
        self.navigation.setAccessibleName("Workflow navigation")
        self.navigation.setMinimumWidth(150)
        self.navigation.setMaximumWidth(230)
        for index, name in enumerate(self.NAVIGATION):
            item = QListWidgetItem(name, self.navigation)
            item.setToolTip(f"Alt+{index + 1}")
            item.setData(
                Qt.ItemDataRole.AccessibleDescriptionRole,
                f"Workflow destination {index + 1}; shortcut Alt+{index + 1}",
            )

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
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("main-splitter")
        splitter.addWidget(self.navigation)
        splitter.addWidget(self.pages)
        splitter.setSizes([180, 1260])
        splitter.setStretchFactor(1, 1)
        self._splitter = splitter
        self.setCentralWidget(splitter)

        self.navigation.currentRowChanged.connect(self._navigate)
        self.navigation.setCurrentRow(0)
        self.overview_page.choose_project.connect(self.choose_project)
        self.overview_page.navigate_requested.connect(self.navigation.setCurrentRow)
        self.connect_page.connector.currentTextChanged.connect(self._connector_changed)
        self.collect_page.issue_requested.connect(self._open_readiness_issue)
        self.verify_page.return_to_collect.connect(self._return_to_collect)
        self.targets_page.return_to_collect.connect(self._return_to_collect)
        self.targets_page.dirty_changed.connect(self._set_dirty)
        self._dirty = False
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

        help_menu = self.menuBar().addMenu("&Help")
        concepts = QAction("Concepts and workflow", self)
        concepts.setShortcut(QKeySequence.StandardKey.HelpContents)
        concepts.triggered.connect(self._show_help)
        help_menu.addAction(concepts)

        for index in range(len(self.NAVIGATION)):
            shortcut = QShortcut(QKeySequence(f"Alt+{index + 1}"), self)
            shortcut.activated.connect(lambda row=index: self.navigation.setCurrentRow(row))

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        self.setStatusBar(status)
        self.transport_status = QLabel("Offline")
        self.adapter_status = QLabel("Adapter closed")
        self.project_status = QLabel("No project open")
        self.draft_status = QLabel("Saved")
        for widget in (self.transport_status, self.adapter_status, self.project_status, self.draft_status):
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
        self._update_window_title()
        self.statusBar().showMessage(
            "Project marker found. Preview data remains synthetic; project loading is not implemented.", 6000
        )
        return True

    def _navigate(self, row: int) -> None:
        if not 0 <= row < self.pages.count():
            return
        self.pages.setCurrentIndex(row)

    def _connector_changed(self, name: str) -> None:
        self.verify_page.set_connector(name)
        self.overview_page.cards[0].detail.setText(
            f"{name} selected; example settings not saved or validated"
        )

    def _open_readiness_issue(self, issue_id: str) -> None:
        if issue_id in self.verify_page.did_items:
            self.navigation.setCurrentRow(3)
            self.verify_page.focus_issue(issue_id)
        else:
            self.navigation.setCurrentRow(4)
            self.targets_page.focus_issue(issue_id)

    def _return_to_collect(self) -> None:
        self.navigation.setCurrentRow(5)
        self.verify_page.return_button.setVisible(False)
        self.verify_page.arrival_notice.setVisible(False)
        self.targets_page.return_button.setVisible(False)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.draft_status.setText("Unsaved example changes" if dirty else "Saved")
        self._update_window_title()

    def _update_window_title(self) -> None:
        project = f" - {self._project_path.name}" if self._project_path else ""
        dirty = " *" if self._dirty else ""
        self.setWindowTitle(f"Telemetry Operator{project}{dirty}")

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "Telemetry Operator concepts",
            "Endpoint: an ECU address plus diagnostic session.\n\n"
            "DID: one data request. A DID can supply several signals, so its polling interval is shared.\n\n"
            "Baseline: the value returned during vehicle verification.\n\n"
            "Estimated bus load: the admitted share of diagnostic communication bandwidth.\n\n"
            "Maximum storage: the run's configured disk limit.\n\n"
            "Compiled hash: the identity of the exact reviewed collection scope.\n\n"
            "Projects are created from the project template outside this application, then opened here. "
            "CAN bitrate comes from the reviewed vehicle transport configuration.\n\n"
            "Keyboard: Alt+1 through Alt+7 open the workflow destinations.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self.analyze_page.cancel()
        if self._restore_settings:
            self._settings.setValue("window/geometry", self.saveGeometry())
            self._settings.setValue("window/splitter", self._splitter.saveState())
        super().closeEvent(event)
