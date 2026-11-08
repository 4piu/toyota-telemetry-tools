"""Import candidates: preview a source against the candidate list, then apply exactly that."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.inventory import ImportPreview, describe_import
from toyota_telemetry.operator.sources import NormalizedFileSource, TisDatabaseSource, default_tis_database

from .context import PageContext
from .dialogs import parse_keys
from .widgets import button_row, notice, path_field, set_enabled, show_notice


class ImportDialog(QDialog):
    """Kept for the window's lifetime so a long preview survives closing it."""

    def __init__(self, context: PageContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._preview: ImportPreview | None = None
        self.setWindowTitle("Import candidates")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        choices = QHBoxLayout()
        self.source_file = QRadioButton("JSON file")
        self.source_file.setToolTip("Candidate rows as JSON")
        self.source_tis = QRadioButton("TIS database")
        self.source_tis.setToolTip("Installed Techstream databases (Windows)")
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
        file_form.addRow("Row file", path_field(self.file_path, "Candidate row file"))
        self.source_stack.addWidget(file_page)
        tis_page = QWidget()
        tis_form = QFormLayout(tis_page)
        self.tis_directory = QLineEdit()
        installed = default_tis_database()
        if installed is not None:
            self.tis_directory.setText(str(installed))
        self.tis_ecus = QLineEdit()
        self.tis_ecus.setPlaceholderText("all installed ECUs")
        self.tis_code_page = QSpinBox()
        self.tis_code_page.setRange(0, 65535)
        self.tis_code_page.setValue(932)
        self.tis_directory.setToolTip("Region folder containing DB")
        self.tis_ecus.setToolTip("Empty reads every ECU")
        self.tis_code_page.setToolTip("932 for the NA set")
        tis_form.addRow("Database folder", path_field(self.tis_directory, "TIS database folder", directory=True))
        tis_form.addRow("ECU keys", self.tis_ecus)
        tis_form.addRow("Code page", self.tis_code_page)
        self.source_stack.addWidget(tis_page)
        self._source_buttons.idToggled.connect(
            lambda index, checked: checked and self.source_stack.setCurrentIndex(index)
        )
        layout.addWidget(self.source_stack)
        self.result = notice()
        layout.addWidget(self.result)
        self.preview_button = QPushButton("Preview")
        self.apply_button = QPushButton("Apply")
        self.close_button = QPushButton("Close")
        context.register("source.preview", self.preview_button, "Show what will change; nothing is written")
        context.register("source.apply", self.apply_button, "Make exactly the previewed changes")
        set_enabled(self.apply_button, False, "Preview a source first.")
        self.preview_button.clicked.connect(self.preview_source)
        self.apply_button.clicked.connect(self.apply_source)
        self.close_button.clicked.connect(self.reject)
        row = button_row(self.preview_button, self.apply_button)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        problem = TisDatabaseSource(Path(self.tis_directory.text() or ".")).availability()
        platform_problem = problem is not None and "DB folder" not in problem
        set_enabled(self.source_tis, not platform_problem, problem or "")

    def _source(self):
        if self.source_file.isChecked():
            if not self.file_path.text().strip():
                raise ValueError("choose a row file first")
            return NormalizedFileSource(Path(self.file_path.text().strip()))
        if not self.tis_directory.text().strip():
            raise ValueError("choose the TIS database folder first")
        return TisDatabaseSource(
            Path(self.tis_directory.text().strip()),
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
            show_notice(self.result, str(exc))
            return

        def done(preview) -> None:
            self.preview_button.setEnabled(True)
            lines = describe_import(preview)
            lines += [f"Rejected {where}: {reason}" for where, reason in preview.rejections[:5]]
            if preview.blocking_problem:
                lines.append(f"Cannot use this list: {preview.blocking_problem}")
                set_enabled(self.apply_button, False, preview.blocking_problem)
            else:
                self._preview = preview
                set_enabled(self.apply_button, True)
            show_notice(self.result, "\n".join(lines))

        def failed(detail: str) -> None:
            self.preview_button.setEnabled(True)
            show_notice(self.result, f"Could not read the source: {_first(detail)}")

        show_notice(self.result, "Reading the source…")
        self.preview_button.setEnabled(False)
        self.context.run("import.preview", lambda service: service.preview_source(source.read()), done, failed)

    def apply_source(self) -> None:
        preview = self._preview
        if self.context.service is None or preview is None:
            return

        def done(_result) -> None:
            show_notice(self.result, f"Applied {preview.label}.")
            self._preview = None
            set_enabled(self.apply_button, False, "Preview a source first.")
            self.context.changed()

        def failed(detail: str) -> None:
            set_enabled(self.apply_button, True)
            show_notice(self.result, "")
            self.context.error("Candidates not imported", _first(detail))

        show_notice(self.result, f"Applying {preview.label}…")
        set_enabled(self.apply_button, False, "Applying…")
        self.context.run("import.apply", lambda service: service.apply_source(preview), done, failed)


def _first(detail: str) -> str:
    return detail.split("\n\n", 1)[0]
