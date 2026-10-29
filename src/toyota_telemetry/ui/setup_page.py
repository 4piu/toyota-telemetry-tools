"""Setup: project validity, connector settings, candidate sources, cache."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.connector import CONNECTOR_FIELDS, CONNECTOR_LABELS
from toyota_telemetry.operator.inventory import ImportPreview
from toyota_telemetry.operator.sources import NormalizedFileSource, TisDatabaseSource

from .context import PageContext
from .dialogs import parse_keys
from .widgets import button_row, notice, page_title, path_field, set_enabled, show_notice


class SetupPage(QWidget):
    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self._preview: ImportPreview | None = None
        self._load_connector_pending = False
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            "Setup",
            "Check the project, describe this computer's CAN connector, and import a candidate "
            "signal list. Nothing on this page transmits to the vehicle.",
        ))
        layout.addWidget(self._project_group())
        layout.addWidget(self._connector_group())
        layout.addWidget(self._source_group())
        layout.addStretch(1)
        context.project_changed.connect(self.refresh)
        context.data_changed.connect(self.refresh_project_summary)
        self.refresh()

    # Project ----------------------------------------------------------------

    def _project_group(self) -> QGroupBox:
        group = QGroupBox("Project")
        layout = QVBoxLayout(group)
        self.project_summary = QLabel("No project open.")
        self.project_summary.setWordWrap(True)
        layout.addWidget(self.project_summary)
        self.check_button = QPushButton("Check")
        self.rebuild_button = QPushButton("Rebuild cache")
        self.context.register("check", self.check_button, "Validate the tracked project files and the candidate list.")
        self.context.register(
            "cache.rebuild", self.rebuild_button,
            "Rebuild the disposable cache from project files, candidates, and recordings.",
        )
        self.check_button.clicked.connect(self.check_project)
        self.rebuild_button.clicked.connect(self.rebuild_cache)
        layout.addLayout(button_row(self.check_button, self.rebuild_button))
        self.check_result = notice()
        layout.addWidget(self.check_result)
        return group

    def check_project(self) -> None:
        def done(result) -> None:
            self.check_button.setEnabled(True)
            lines = [f"OK: {note}" for note in result.notes] + [f"Problem: {text}" for text in result.problems]
            show_notice(self.check_result, "\n".join(lines))

        self._start(self.check_button, "setup.check", lambda service: service.check(), done, "Check failed")

    def rebuild_cache(self) -> None:
        def done(_result) -> None:
            self.rebuild_button.setEnabled(True)
            self.context.message("Cache rebuilt.")
            self.context.changed()

        self._start(
            self.rebuild_button, "setup.rebuild", lambda service: service.rebuild_cache(), done,
            "Cache rebuild failed",
        )

    def _start(self, button, key, work, done, title) -> None:
        """Run a button's work in the background, disabling it until done."""

        if self.context.service is None:
            return
        button.setEnabled(False)

        def failed(detail: str) -> None:
            button.setEnabled(True)
            self.context.error(title, _first(detail))

        self.context.run(key, work, done, failed)

    def refresh_project_summary(self) -> None:
        if self.context.service is None:
            self.project_summary.setText("No project open.")
            return

        def work(service):
            repository = service.repository()
            inventory = service.inventory(repository)
            return {
                "root": str(service.root),
                "project_id": repository.project_id,
                "profile_id": repository.profile_id,
                "endpoints": len(repository.transport["endpoints"]),
                "sources": [source["label"] for source in inventory["sources"]],
                "rows": len(inventory["rows"]),
                "connector": repository.local_settings,
            }

        def done(summary) -> None:
            sources = ", ".join(summary["sources"]) or "none yet"
            self.project_summary.setText(
                f"{summary['root']}\nProject {summary['project_id']}, profile {summary['profile_id']}. "
                f"{summary['endpoints']} confirmed endpoint(s). Candidate sources: {sources} "
                f"({summary['rows']} rows)."
            )
            if self._load_connector_pending:
                self._load_connector_pending = False
                self.load_connector(summary["connector"])

        def failed(detail: str) -> None:
            self.project_summary.setText(f"Problem: {_first(detail)}")

        self.project_summary.setText("Loading…")
        self.context.run("setup.summary", work, done, failed)

    # Connector --------------------------------------------------------------

    def _connector_group(self) -> QGroupBox:
        group = QGroupBox("Connector")
        group.setToolTip(
            "The CAN adapter and driver on this computer. Settings are machine-local and not "
            "tracked. CAN bitrate comes from the reviewed transport configuration."
        )
        layout = QVBoxLayout(group)
        form = QFormLayout()
        self.connector_kind = QComboBox()
        for kind, label in CONNECTOR_LABELS.items():
            self.connector_kind.addItem(label, kind)
        form.addRow("Connector", self.connector_kind)
        self.connector_fields: dict[str, dict[str, QLineEdit]] = {}
        self.connector_stack = QStackedWidget()
        for kind, fields in CONNECTOR_FIELDS.items():
            page = QWidget()
            page_form = QFormLayout(page)
            page_form.setContentsMargins(0, 0, 0, 0)
            editors = {}
            for name, label, _required in fields:
                editor = QLineEdit()
                editor.setAccessibleName(label)
                editors[name] = editor
                page_form.addRow(label, path_field(editor, label) if name == "dll" else editor)
            self.connector_fields[kind] = editors
            self.connector_stack.addWidget(page)
        form.addRow(self.connector_stack)
        self.executable = QLineEdit()
        self.executable.setPlaceholderText("default")
        self.executable.setToolTip("Worker program; empty uses scratch/bin/telemetry-KIND.exe.")
        form.addRow("Worker executable", self.executable)
        layout.addLayout(form)
        self.context.register("connector.show", self.connector_kind, "Adapter type")
        self.connector_kind.currentIndexChanged.connect(self.connector_stack.setCurrentIndex)
        self.connector_check_button = QPushButton("Check")
        self.connector_save_button = QPushButton("Save")
        self.context.register(
            "connector.check", self.connector_check_button,
            "Validate the settings and run the worker self-test. The adapter is not opened.",
        )
        self.context.register("connector.set", self.connector_save_button, "Save the connector settings on this computer.")
        self.connector_check_button.clicked.connect(self.check_connector)
        self.connector_save_button.clicked.connect(self.save_connector)
        layout.addLayout(button_row(self.connector_check_button, self.connector_save_button))
        self.connector_result = notice()
        layout.addWidget(self.connector_result)
        return group

    def _connector_document(self):
        kind = self.connector_kind.currentData()
        settings = {name: editor.text() for name, editor in self.connector_fields[kind].items()}
        return self.context.service.connector_document(kind, settings, self.executable.text().strip() or None)

    def check_connector(self) -> None:
        if self.context.service is None:
            return
        try:
            document = self._connector_document()
        except ValueError as exc:
            show_notice(self.connector_result, f"Problem: {exc}")
            return

        def done(check) -> None:
            self.connector_check_button.setEnabled(True)
            lines = [f"OK: {note}" for note in check.notes] + [f"Problem: {text}" for text in check.problems]
            if check.ok:
                lines.append("Settings are valid.")
            show_notice(self.connector_result, "\n".join(lines))

        show_notice(self.connector_result, "Checking…")
        self._start(
            self.connector_check_button, "setup.connector.check",
            lambda service: service.check_connector(document), done, "Connector check failed",
        )

    def save_connector(self) -> None:
        if self.context.service is None:
            return
        try:
            document = self._connector_document()
            self.context.service.save_connector(document)
        except (OSError, ValueError) as exc:
            show_notice(self.connector_result, f"Not saved: {exc}")
            return
        show_notice(self.connector_result, "Saved.")
        self.context.changed()

    def load_connector(self, document) -> None:
        for editors in self.connector_fields.values():
            for editor in editors.values():
                editor.clear()
        self.executable.clear()
        if document is None:
            return
        backend = document["backend"]
        self.connector_kind.setCurrentIndex(self.connector_kind.findData(backend["kind"]))
        for name, value in backend["settings"].items():
            self.connector_fields[backend["kind"]][name].setText(value)
        self.executable.setText(backend["executable"])

    # Sources ----------------------------------------------------------------

    def _source_group(self) -> QGroupBox:
        group = QGroupBox("Candidate signal source")
        layout = QVBoxLayout(group)
        choices = QHBoxLayout()
        self.source_file = QRadioButton("JSON file")
        self.source_file.setToolTip("A normalized JSON file of candidate rows.")
        self.source_tis = QRadioButton("TIS database")
        self.source_tis.setToolTip("Installed Techstream (TIS) databases. Windows only.")
        self.source_file.setChecked(True)
        self._source_buttons = QButtonGroup(self)
        for index, button in enumerate((self.source_file, self.source_tis)):
            self._source_buttons.addButton(button, index)
            choices.addWidget(button)
        choices.addStretch(1)
        layout.addLayout(choices)
        self.source_stack = QStackedWidget()
        file_page = QWidget()
        file_form = QFormLayout(file_page)
        self.file_path = QLineEdit()
        file_form.addRow("Row file", path_field(self.file_path, "Normalized row file"))
        self.source_stack.addWidget(file_page)
        tis_page = QWidget()
        tis_form = QFormLayout(tis_page)
        self.tis_directory = QLineEdit()
        self.tis_phases = QLineEdit("5")
        self.tis_ecus = QLineEdit()
        self.tis_ecus.setPlaceholderText("all installed ECUs")
        self.tis_code_page = QSpinBox()
        self.tis_code_page.setRange(0, 65535)
        self.tis_code_page.setValue(932)
        self.tis_directory.setToolTip("Techstream region folder that contains DB")
        self.tis_phases.setToolTip("Database phases to read, e.g. 5")
        self.tis_ecus.setToolTip("Limit to these ECU keys; empty reads every installed ECU")
        self.tis_code_page.setToolTip("Code page of the database text (932 for the NA set)")
        tis_form.addRow("Database folder", path_field(self.tis_directory, "TIS database folder", directory=True))
        tis_form.addRow("Phases", self.tis_phases)
        tis_form.addRow("ECU keys", self.tis_ecus)
        tis_form.addRow("Code page", self.tis_code_page)
        self.source_stack.addWidget(tis_page)
        self._source_buttons.idToggled.connect(
            lambda index, checked: checked and self.source_stack.setCurrentIndex(index)
        )
        layout.addWidget(self.source_stack)
        self.preview_button = QPushButton("Preview")
        self.apply_button = QPushButton("Apply")
        self.context.register(
            "source.preview", self.preview_button,
            "Read the source and show what would change. Nothing is written.",
        )
        self.context.register(
            "source.apply", self.apply_button,
            "Use the previewed rows as this source's candidates and update the catalog manifest and cache.",
        )
        set_enabled(self.apply_button, False, "Preview a source first.")
        self.preview_button.clicked.connect(self.preview_source)
        self.apply_button.clicked.connect(self.apply_source)
        layout.addLayout(button_row(self.preview_button, self.apply_button))
        self.preview_result = notice()
        layout.addWidget(self.preview_result)
        return group

    def _source(self):
        if self.source_file.isChecked():
            if not self.file_path.text().strip():
                raise ValueError("choose a row file first")
            return NormalizedFileSource(Path(self.file_path.text().strip()))
        if not self.tis_directory.text().strip():
            raise ValueError("choose the TIS database folder first")
        phases = parse_keys(self.tis_phases.text()) or [5]
        return TisDatabaseSource(
            Path(self.tis_directory.text().strip()),
            phases=phases,
            ecu_keys=parse_keys(self.tis_ecus.text()),
            text_code_page=self.tis_code_page.value(),
        )

    def preview_source(self) -> None:
        if self.context.service is None:
            return
        self._preview = None
        set_enabled(self.apply_button, False, "Preview a source first.")
        try:
            source = self._source()
        except ValueError as exc:
            show_notice(self.preview_result, str(exc))
            return

        def done(preview) -> None:
            self.preview_button.setEnabled(True)
            counts = preview.counts
            lines = [
                f"{preview.label}: {counts.get('rows', 0)} rows, {counts.get('signals', 0)} signals, "
                f"{counts.get('dids', 0)} DIDs, {counts.get('ecus', 0)} ECUs, "
                f"{counts.get('endpoint_candidates', 0)} endpoint candidates.",
                f"Compared with the current list: {counts.get('added', 0)} added, "
                f"{counts.get('removed', 0)} removed, {counts.get('unchanged', 0)} unchanged; "
                f"{counts.get('conflicts', 0)} conflicting signal(s); {counts.get('rejected', 0)} rejected.",
            ]
            lines += [f"Rejected {where}: {reason}" for where, reason in preview.rejections[:5]]
            if preview.blocking_problem:
                lines.append(f"Cannot use this list: {preview.blocking_problem}")
                set_enabled(self.apply_button, False, preview.blocking_problem)
            else:
                self._preview = preview
                set_enabled(self.apply_button, True)
            show_notice(self.preview_result, "\n".join(lines))

        def failed(detail: str) -> None:
            self.preview_button.setEnabled(True)
            show_notice(self.preview_result, f"Could not read the source: {_first(detail)}")

        show_notice(self.preview_result, "Reading the source…")
        self.preview_button.setEnabled(False)
        self.context.run("setup.preview", lambda service: service.preview_source(source.read()), done, failed)

    def apply_source(self) -> None:
        preview = self._preview
        if self.context.service is None or preview is None:
            return

        def done(_result) -> None:
            show_notice(self.preview_result, f"Applied {preview.label}.")
            self._preview = None
            set_enabled(self.apply_button, False, "Preview a source first.")
            self.context.changed()

        def failed(detail: str) -> None:
            set_enabled(self.apply_button, True)
            show_notice(self.preview_result, "")
            self.context.error("Candidate list not used", _first(detail))

        show_notice(self.preview_result, f"Applying {preview.label}…")
        set_enabled(self.apply_button, False, "Applying…")
        self.context.run("setup.apply", lambda service: service.apply_source(preview), done, failed)

    # Refresh ----------------------------------------------------------------

    def refresh(self) -> None:
        has_project = self.context.service is not None
        for widget in (
            self.check_button, self.rebuild_button, self.connector_check_button,
            self.connector_save_button, self.preview_button,
        ):
            set_enabled(widget, has_project, "Open a project first.")
        problem = TisDatabaseSource(Path(self.tis_directory.text() or ".")).availability()
        platform_problem = problem is not None and "DB folder" not in problem
        set_enabled(self.source_tis, not platform_problem, problem or "")
        self._load_connector_pending = has_project
        if not has_project:
            self.load_connector(None)
        self.refresh_project_summary()
        show_notice(self.check_result, "")


def _first(detail: str) -> str:
    return detail.split("\n\n", 1)[0]
