"""Application shell: a step bar over four task pages, the project menu, and live-safe closing."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QSettings
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator import OperatorService

from .collect_page import CollectPage
from .context import PageContext
from .recordings_page import RecordingsPage
from .setup_page import SetupPage
from .signals_page import SignalsPage
from .widgets import BusyIndicator
from .widgets.step_bar import ATTENTION, DONE, NEXT, PENDING, Part, StepBar


PAGES = ("setup", "signals", "collect", "recordings")
TITLES = {"setup": "Setup", "signals": "Signals", "collect": "Collect", "recordings": "Recordings"}
DESCRIPTIONS = {
    "setup": "Connector and candidate signals",
    "signals": "What to collect, and how often",
    "collect": "Run collection; target progress",
    "recordings": "Runs, plots, and export",
}


def step_states(status, dirty: bool) -> list[tuple[str, list[Part]]]:
    """Each page's state and status lines, from one readiness snapshot."""

    if status is None:
        return [(NEXT, [Part("Open or create a project", NEXT)])] + [(PENDING, [])] * 3
    if not status.valid:
        return [(ATTENTION, [Part("Project files", ATTENTION, "invalid")])] + [(PENDING, [])] * 3
    counts = status.counts
    setup = [
        Part("Connector", DONE if status.connector else PENDING, (status.connector or "").split(" ")[0]),
        Part("Candidates", DONE if status.candidate_rows else PENDING,
             f"{status.candidate_rows}" if status.candidate_rows else ""),
    ]
    verified, dids, targets = counts.get("verified_dids", 0), counts.get("dids", 0), counts.get("targets", 0)
    signals = [
        Part("Readable", DONE if verified else PENDING, f"{verified}/{dids} DIDs" if dids else ""),
        Part("Targets", DONE if targets else PENDING, f"{targets} set" if targets else ""),
    ]
    if dirty:
        signals.append(Part("Unsaved", ATTENTION, "changes"))
    collection = status.collection
    if status.live_busy:
        collect = [Part("Running", ATTENTION)]
    elif collection is None:
        collect = [Part("Ready", PENDING, "no targets")]
    elif collection["ready"]:
        collect = [Part("Ready", DONE)]
    elif collection["complete"] and not collection["problems"]:
        collect = [Part("Complete", DONE)]
    else:
        problems = len(collection["problems"])
        collect = [Part("Not ready", ATTENTION, f"{problems} problem" + ("s" if problems != 1 else ""))]
    if targets:
        complete = status.targets_complete
        collect.append(Part("Targets met", DONE if complete == targets else PENDING, f"{complete}/{targets}"))
    if dirty:
        # Collection uses the saved settings; say so while Signals holds a draft.
        collect.append(Part("Unsaved", ATTENTION, "not applied"))
    recordings = [Part("Recorded", DONE if status.runs else PENDING, "" if status.runs else "nothing yet")]
    if status.recoverable_runs:
        recordings.append(Part("Interrupted", ATTENTION, f"{len(status.recoverable_runs)}"))
    next_page = status.next_action.page if status.next_action else None
    result = []
    for page, parts in zip(PAGES, (setup, signals, collect, recordings)):
        if any(part.state == ATTENTION for part in parts):
            state = ATTENTION
        elif page == next_page:
            state = NEXT
        elif parts and all(part.state == DONE for part in parts):
            state = DONE
        else:
            state = PENDING
        result.append((state, parts))
    return result


class OperatorWindow(QMainWindow):
    def __init__(self, project_path: Path | None = None, *, restore_settings: bool = True) -> None:
        super().__init__()
        self.setObjectName("operator-window")
        self.setWindowTitle("Telemetry Operator")
        self.resize(1360, 880)
        self.setMinimumSize(1024, 700)
        self._settings = QSettings("toyota-telemetry-tools", "TelemetryOperator")
        self._restore_settings = restore_settings
        self._closing_after_job = False

        self.context = PageContext(self, self.navigate)
        self.context.settings = self._settings if restore_settings else None
        self.context.message = lambda text: self.statusBar().showMessage(text, 8000)

        self.navigation = StepBar([TITLES[page] for page in PAGES])
        self._status = None
        self.next_action = "Open or create a project"
        self.context.register("status", self.navigation, "Progress; click a step to open it")
        self.choose_result_folder = lambda start: QFileDialog.getExistingDirectory(self, "Export folder", start)
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
        content_layout.setSpacing(0)
        content_layout.addWidget(self.navigation)
        content_layout.addWidget(self.pages, 1)
        self.setCentralWidget(content)
        self.navigation.current_changed.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)

        self._build_menu()
        self._build_status_bar()
        self.signals_page.dirty_changed.connect(self._update_title)
        self.signals_page.dirty_changed.connect(lambda _dirty: self._show_steps())
        self.signals_page.dirty_changed.connect(self.collect_page.set_unsaved)
        self.context.project_changed.connect(self.refresh_status)
        self.context.data_changed.connect(self.refresh_status)
        self.context.busy_changed.connect(self._busy_changed)
        self.context.project_changed.connect(self._update_actions)
        self.context.busy_changed.connect(lambda _busy: self._update_actions())
        self._update_actions()

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

    def _build_menu(self) -> None:
        project_menu = self.menuBar().addMenu("&Project")
        new_action = QAction("New project", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_project)
        self.context.register("init", new_action, "Create a workspace in an empty folder")
        project_menu.addAction(new_action)
        open_action = QAction("Open project", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.choose_project)
        project_menu.addAction(open_action)
        project_menu.addSeparator()
        self.export_result_action = QAction("Export result", self)
        self.export_result_action.triggered.connect(self.export_result)
        self.context.register("export", self.export_result_action, "Write the signal list to a folder for publishing")
        project_menu.addAction(self.export_result_action)
        project_menu.setToolTipsVisible(True)
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
        self.work_status = BusyIndicator("Loading…")
        self.work_status.setToolTip("Reading project data")
        self.project_status = QLabel("No project open")
        self.connector_status = QLabel("No connector")
        self.adapter_status = QLabel("Adapter closed")
        self.draft_status = QLabel("Saved")
        for widget in (
            self.work_status, self.project_status, self.connector_status, self.adapter_status, self.draft_status,
        ):
            status.addPermanentWidget(widget)
        self.context.worker.busy_changed.connect(
            self.work_status.set_busy
        )

    # Project and status ----------------------------------------------------

    def choose_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Open project workspace")
        if selected:
            self.open_project(Path(selected))

    def new_project(self) -> None:
        selected = self.choose_new_folder()
        if not selected:
            return
        try:
            OperatorService.create(Path(selected))
        except (OSError, ValueError) as exc:
            self.context.error("Project not created", str(exc))
            return
        self.open_project(Path(selected))

    def choose_new_folder(self) -> str:
        return QFileDialog.getExistingDirectory(self, "Empty folder for the new project")

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
            self.next_action = "Open or create a project"
            self.project_status.setText("No project open")
            self.connector_status.setText("No connector")
            self._status = None
            self._show_steps()
            return
        self.project_status.setText(service.root.name)
        self.context.run("status", lambda service: service.status(), self._show_status, lambda _detail: None)

    def _show_status(self, status) -> None:
        name = Path(status.project_root).name
        self.project_status.setText(name if status.valid else f"{name}: invalid")
        self.connector_status.setText(status.connector or "No connector")
        self.next_action = status.next_action.text
        self._status = status
        self._show_steps()

    def _show_steps(self) -> None:
        dirty = self.signals_page.dirty
        for index, (page, (state, parts)) in enumerate(zip(PAGES, step_states(self._status, dirty))):
            tip = f"{DESCRIPTIONS[page]} (Alt+{index + 1})"
            if state == NEXT:
                tip += f"\nNext: {self.next_action}"
            self.navigation.set_step(index, state, parts, tip)

    def navigate(self, page: str) -> None:
        if page in PAGES:
            self.navigation.setCurrentRow(PAGES.index(page))

    def _update_actions(self) -> None:
        enabled = self.context.service is not None and not self.context.live_job_running
        self.export_result_action.setEnabled(enabled)

    def export_result(self) -> None:
        service = self.context.service
        if service is None:
            return
        key = f"export/{service.root.as_posix()}"
        folder = self.choose_result_folder(self.context.recall(key) or str(service.root.parent))
        if not folder:
            return
        destination = Path(folder)

        def done(manifest) -> None:
            self.context.remember(key, destination.as_posix())
            self.context.message(
                f"Exported the result ({manifest['row_counts']['signals']} signals) to {destination.name}."
            )

        self.statusBar().showMessage("Exporting the result…")
        self.context.run(
            "export", lambda service: service.export_result(destination), done, title="Result not exported",
        )

    def _busy_changed(self, running: bool) -> None:
        self.adapter_status.setText("LIVE - adapter open" if running else "Adapter closed")
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
            "Diagnostic session: the mode an ECU is switched to before it is read. Most ECUs answer more "
            "requests in the extended session (request 10 03) than in the default one. The choices are the "
            "session steps reviewed in the project's transport settings.\n\n"
            "Verify vs Confirm endpoint: Verify transmits read requests to see which DIDs answer. Confirm "
            "endpoint transmits nothing; it records your review that an endpoint which answered is the ECU "
            "you think it is, with where the evidence is written up. Collection needs both.\n\n"
            "Refusal codes: when an ECU rejects a request it returns a code, e.g. 0x31 (request out of range) "
            "or 0x22 (conditions not correct); the app names common ones.\n\n"
            "Run: one recording session, saved as one folder. Delete moves a run's folder to the system "
            "trash; restore it from there to bring the run back.\n\n"
            "Interrupted run: a recording that stopped without finishing. Recover keeps everything saved "
            "up to the interruption.\n\n"
            "Only the buttons with a warning icon (Verify and Start) transmit to the vehicle, and "
            "each shows its exact scope first.\n\n"
            "Hover over any control for a short explanation.\n\n"
            "The steps across the top are the pages in workflow order. A tick means done, a dot marks "
            "the next step (hover it to see what to do), and ! needs attention.\n\n"
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
