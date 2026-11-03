"""Application shell: four task pages, a next-step banner, and live-safe closing."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .collect_page import CollectPage
from .context import PageContext
from .recordings_page import RecordingsPage
from .setup_page import SetupPage
from .signals_page import SignalsPage


PAGES = ("setup", "signals", "collect", "recordings")
TITLES = {"setup": "Setup", "signals": "Signals", "collect": "Collect", "recordings": "Recordings"}
DESCRIPTIONS = {
    "setup": "Project check, connector, and candidate signal list. Nothing here transmits.",
    "signals": "Verify endpoints and DIDs, review signals, and set collection targets.",
    "collect": "Check readiness, then start and stop collection.",
    "recordings": "Recorded runs, recovery, plots, export, and the project result.",
}


class OperatorWindow(QMainWindow):
    def __init__(self, project_path: Path | None = None, *, restore_settings: bool = True) -> None:
        super().__init__()
        self.setObjectName("operator-window")
        self.setWindowTitle("Telemetry Operator")
        self.resize(1360, 880)
        self.setMinimumSize(1024, 700)
        self._settings = QSettings("70Y074", "TelemetryOperator")
        self._restore_settings = restore_settings
        self._closing_after_job = False

        self.context = PageContext(self, self.navigate)
        self.context.message = lambda text: self.statusBar().showMessage(text, 8000)

        self.navigation = QListWidget()
        self.navigation.setAccessibleName("Pages")
        self.navigation.setMaximumWidth(200)
        for index, page in enumerate(PAGES):
            item = QListWidgetItem(TITLES[page], self.navigation)
            item.setToolTip(f"{DESCRIPTIONS[page]} (Alt+{index + 1})")

        self.banner = self._build_banner()
        self.setup_page = SetupPage(self.context)
        self.signals_page = SignalsPage(self.context)
        self.collect_page = CollectPage(self.context)
        self.recordings_page = RecordingsPage(self.context)
        self.pages = QStackedWidget()
        for page in (self.setup_page, self.signals_page, self.collect_page, self.recordings_page):
            if page is self.setup_page:
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QScrollArea.Shape.NoFrame)
                scroll.setWidget(page)
                self.pages.addWidget(scroll)
            else:
                self.pages.addWidget(page)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.banner)
        content_layout.addWidget(self.pages, 1)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.navigation)
        splitter.addWidget(content)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([170, 1190])
        self._splitter = splitter
        self.setCentralWidget(splitter)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)

        self._build_menu()
        self._build_status_bar()
        self.signals_page.dirty_changed.connect(self._update_title)
        self.context.project_changed.connect(self.refresh_status)
        self.context.data_changed.connect(self.refresh_status)
        self.context.busy_changed.connect(self._busy_changed)

        if project_path is not None:
            self.open_project(project_path)
        else:
            self.refresh_status()
        if restore_settings:
            geometry = self._settings.value("window/geometry")
            if geometry is not None:
                self.restoreGeometry(geometry)
        self.installEventFilter(self)

    # Construction -----------------------------------------------------------

    def _build_banner(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setAccessibleName("Next step")
        layout = QHBoxLayout(frame)
        self.next_text = QLabel()
        font = self.next_text.font()
        font.setBold(True)
        self.next_text.setFont(font)
        layout.addWidget(self.next_text, 1)
        self.next_button = QPushButton("Go")
        self.next_button.clicked.connect(self._follow_next)
        self.context.register("status", self.next_button, "Open the page for the next step")
        layout.addWidget(self.next_button)
        self._next_page = "setup"
        return frame

    def _build_menu(self) -> None:
        project_menu = self.menuBar().addMenu("&Project")
        open_action = QAction("Open project", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.choose_project)
        project_menu.addAction(open_action)
        refresh = QAction("Refresh", self)
        refresh.setShortcut(QKeySequence.StandardKey.Refresh)
        refresh.triggered.connect(self.context.changed)
        project_menu.addAction(refresh)
        project_menu.addSeparator()
        close_action = QAction("Exit", self)
        close_action.setShortcut(QKeySequence.StandardKey.Quit)
        close_action.triggered.connect(self.close)
        project_menu.addAction(close_action)
        help_menu = self.menuBar().addMenu("&Help")
        concepts = QAction("Concepts and workflow", self)
        concepts.setShortcut(QKeySequence.StandardKey.HelpContents)
        concepts.triggered.connect(self.show_help)
        help_menu.addAction(concepts)
        for index in range(len(PAGES)):
            shortcut = QShortcut(QKeySequence(f"Alt+{index + 1}"), self)
            shortcut.activated.connect(lambda row=index: self.navigation.setCurrentRow(row))

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        self.setStatusBar(status)
        self.work_status = QLabel("")
        self.work_status.setToolTip("Reading project data in the background")
        self.project_status = QLabel("No project open")
        self.connector_status = QLabel("No connector")
        self.adapter_status = QLabel("Adapter closed")
        self.draft_status = QLabel("Saved")
        for widget in (
            self.work_status, self.project_status, self.connector_status, self.adapter_status, self.draft_status,
        ):
            status.addPermanentWidget(widget)
        self.context.worker.busy_changed.connect(
            lambda busy: self.work_status.setText("Working…" if busy else "")
        )

    # Project and status ----------------------------------------------------

    def choose_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Open existing telemetry project")
        if selected:
            self.open_project(Path(selected))

    def open_project(self, path: Path) -> bool:
        if self.context.live_job_running:
            self.statusBar().showMessage("Stop the live job before opening another project.", 8000)
            return False
        if not self.signals_page.prompt_unsaved():
            return False
        try:
            self.context.open(Path(path))
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(f"Not a telemetry project: {exc}", 10000)
            return False
        self._update_title()
        return True

    @property
    def project_path(self) -> Path | None:
        return self.context.service.root if self.context.service else None

    def refresh_status(self) -> None:
        service = self.context.service
        if service is None:
            self.next_text.setText("Open a project")
            self.next_text.setToolTip("Projects are created from the template, then opened here.")
            self._next_page = "open"
            self.project_status.setText("No project open")
            self.connector_status.setText("No connector")
            return
        self.project_status.setText(service.root.name)
        if not self.next_text.text().startswith("Next:"):
            self.next_text.setText("Loading…")
        self.context.run("status", lambda service: service.status(), self._show_status, lambda _detail: None)

    def _show_status(self, status) -> None:
        name = Path(status.project_root).name
        self.project_status.setText(name if status.valid else f"{name}: invalid")
        self.connector_status.setText(status.connector or "No connector")
        action = status.next_action
        self.next_text.setText(f"Next: {action.text}")
        self.next_text.setToolTip(status.error or "")
        self._next_page = action.page
        self._next_target = action.target

    def _follow_next(self) -> None:
        if self._next_page == "open":
            self.choose_project()
            return
        self.navigate(self._next_page)
        target = getattr(self, "_next_target", None)
        if target is None:
            return
        page = {"signals": self.signals_page, "recordings": self.recordings_page}.get(self._next_page)
        if page is not None:
            page.focus(*target)

    def navigate(self, page: str) -> None:
        if page in PAGES:
            self.navigation.setCurrentRow(PAGES.index(page))

    def _busy_changed(self, running: bool) -> None:
        self.adapter_status.setText("LIVE - adapter open" if running else "Adapter closed")
        self.navigation.setEnabled(True)
        if not running and self._closing_after_job:
            self.close()

    def _update_title(self, *_args) -> None:
        dirty = self.signals_page.dirty
        self.draft_status.setText("Unsaved changes" if dirty else "Saved")
        project = f" - {self.project_path.name}" if self.project_path else ""
        self.setWindowTitle(f"Telemetry Operator{project}{' *' if dirty else ''}")

    def show_help(self) -> None:
        QMessageBox.information(
            self,
            "Telemetry Operator concepts",
            "Endpoint: one diagnostic route to an ECU (tester and response CAN IDs plus a reviewed session). "
            "Confirmed endpoints are recorded in the project; others are candidates until they respond.\n\n"
            "DID: one data request on an endpoint. A DID can carry several signals, so its interval is shared.\n\n"
            "Signal: one interpreted value inside a DID response; it is what you review, target, and plot.\n\n"
            "Target: the evidence still wanted for a signal, such as minimum samples or required states. "
            "A transition is any change of raw value; a change counts only when the decoded value moves by at "
            "least the minimum difference.\n\n"
            "Signal status: Not checked, then Responds (its DID answered), Recorded (samples saved), and "
            "Reviewed (its meaning confirmed). Refused means the ECU rejected the request.\n\n"
            "Session: the diagnostic session opened before reading, from the project's reviewed transport "
            "settings.\n\n"
            "Refusal codes: when an ECU rejects a request it returns a code, e.g. 0x31 (request out of range) "
            "or 0x22 (conditions not correct); the app names common ones.\n\n"
            "Interrupted drive: a recording that stopped without finishing. Recover keeps everything saved "
            "up to the interruption; Quarantine moves the drive aside without deleting it.\n\n"
            "Only the buttons with a warning icon (Verify and Start) transmit to the vehicle, and "
            "each shows its exact scope first.\n\n"
            "Hover over any control for a short explanation.\n\n"
            "Keyboard: Alt+1 to Alt+4 open the pages; F5 refreshes.",
        )

    # Events -----------------------------------------------------------------

    def eventFilter(self, watched, event) -> bool:
        if watched is self and event.type() == QEvent.Type.WindowActivate and self.context.service is not None:
            self.context.check_outside_changes()
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.context.live_job_running:
            self._closing_after_job = True
            for page in (self.signals_page, self.collect_page):
                if page.job is not None:
                    page.job.request_stop()
            self.statusBar().showMessage("Stopping the live job safely; the window closes when it finishes.")
            event.ignore()
            return
        if not self.signals_page.prompt_unsaved():
            event.ignore()
            return
        if self._restore_settings:
            self._settings.setValue("window/geometry", self.saveGeometry())
        super().closeEvent(event)
