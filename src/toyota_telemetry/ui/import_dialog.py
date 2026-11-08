"""Add candidates: from a JSON file, from the TIS database, or endpoints typed in by hand."""

from __future__ import annotations

from pathlib import Path
import re

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.inventory import ImportPreview, describe_import
from toyota_telemetry.operator.sources import NormalizedFileSource, TisDatabaseSource, default_tis_database

from .context import PageContext
from .dialogs import parse_keys
from .widgets import button_row, notice, path_field, set_enabled, show_notice

FILE_TAB, TIS_TAB, MANUAL_TAB = range(3)
MANUAL_COLUMNS = ("Request ID", "Response ID", "Address", "ECU keys")
MANUAL_TIPS = (
    "Request CAN ID, e.g. 0x7E0",
    "Empty means request + 8",
    "Only for an ECU behind a gateway, e.g. 0x2A",
    "ECU keys answering here, e.g. 372, 374",
)


class ImportDialog(QDialog):
    """Kept for the window's lifetime so a long preview survives closing it."""

    def __init__(self, context: PageContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._preview: ImportPreview | None = None
        self.setWindowTitle("Add candidates")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._file_tab(), "JSON file")
        self.tabs.addTab(self._tis_tab(), "TIS database")
        self.tabs.addTab(self._manual_tab(), "Manual")
        layout.addWidget(self.tabs, 1)
        self.preview_button = QPushButton("Preview")
        self.apply_button = QPushButton("Apply")
        context.register("source.preview", self.preview_button, "Show what will change; nothing is written")
        context.register("source.apply", self.apply_button, "Make exactly the previewed changes")
        set_enabled(self.apply_button, False, "Preview a source first.")
        self.preview_button.clicked.connect(self.preview_source)
        self.apply_button.clicked.connect(self.apply_source)
        self.source_buttons = QWidget()
        self.source_buttons.setLayout(button_row(self.preview_button, self.apply_button))
        self.source_buttons.layout().setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.source_buttons)
        self.result = notice()
        layout.addWidget(self.result)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        layout.addLayout(button_row(self.close_button, stretch_first=True))
        self.tabs.currentChanged.connect(self._tab_changed)
        self.refresh()

    # Tabs -------------------------------------------------------------------

    def _file_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.file_path = QLineEdit()
        form.addRow("Row file", path_field(self.file_path, "Candidate row file"))
        return page

    def _tis_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
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
        form.addRow("Database folder", path_field(self.tis_directory, "TIS database folder", directory=True))
        form.addRow("ECU keys", self.tis_ecus)
        form.addRow("Code page", self.tis_code_page)
        return page

    def _manual_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel("One endpoint per row. Nothing is sent; Verify finds the session.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.manual = QTableWidget(1, len(MANUAL_COLUMNS))
        self.manual.setHorizontalHeaderLabels(list(MANUAL_COLUMNS))
        for column, tip in enumerate(MANUAL_TIPS):
            self.manual.horizontalHeaderItem(column).setToolTip(tip)
        self.manual.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.manual.setAccessibleName("Endpoints to add")
        layout.addWidget(self.manual, 1)
        self.add_row_button = QPushButton("Add row")
        self.add_row_button.setToolTip("Another endpoint")
        self.remove_row_button = QPushButton("Remove row")
        self.remove_row_button.setToolTip("Remove the selected rows")
        self.add_endpoints_button = QPushButton("Add endpoints")
        self.context.register("endpoint.add", self.add_endpoints_button, "Add these endpoint candidates")
        self.add_row_button.clicked.connect(lambda: self.manual.insertRow(self.manual.rowCount()))
        self.remove_row_button.clicked.connect(self.remove_rows)
        self.add_endpoints_button.clicked.connect(self.add_endpoints)
        row = button_row(self.add_row_button, self.remove_row_button)
        row.addWidget(self.add_endpoints_button)
        layout.addLayout(row)
        return page

    def _tab_changed(self, index: int) -> None:
        self.source_buttons.setVisible(index != MANUAL_TAB)
        show_notice(self.result, "")

    def refresh(self) -> None:
        problem = TisDatabaseSource(Path(self.tis_directory.text() or ".")).availability()
        platform_problem = problem is not None and "DB folder" not in problem
        self.tabs.setTabEnabled(TIS_TAB, not platform_problem)
        self.tabs.setTabToolTip(TIS_TAB, problem if platform_problem else "Installed Techstream databases")

    # Sources ----------------------------------------------------------------

    def _source(self):
        if self.tabs.currentIndex() == FILE_TAB:
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

    # Manual endpoints -------------------------------------------------------

    def remove_rows(self) -> None:
        for row in sorted({index.row() for index in self.manual.selectedIndexes()}, reverse=True):
            self.manual.removeRow(row)
        if self.manual.rowCount() == 0:
            self.manual.insertRow(0)

    def set_manual_rows(self, rows: list[tuple[str, str, str, str]]) -> None:
        """Fill the table (automation and tests)."""

        self.manual.setRowCount(max(1, len(rows)))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.manual.setItem(row, column, QTableWidgetItem(value))

    def manual_rows(self) -> tuple[list[dict], list[int]]:
        """Filled rows as endpoints, with their table row numbers (1-based)."""

        endpoints, numbers = [], []
        for row in range(self.manual.rowCount()):
            cells = [
                (self.manual.item(row, column).text().strip() if self.manual.item(row, column) else "")
                for column in range(len(MANUAL_COLUMNS))
            ]
            if not any(cells):
                continue
            endpoints.append({
                "tester_can_id": cells[0], "response_can_id": cells[1] or None,
                "target_address": cells[2] or None, "ecu_keys": parse_keys(cells[3]),
            })
            numbers.append(row + 1)
        return endpoints, numbers

    def add_endpoints(self) -> None:
        service = self.context.service
        if service is None:
            return
        endpoints, numbers = self.manual_rows()
        try:
            service.add_endpoints(endpoints)
        except (OSError, ValueError) as exc:
            # The service numbers filled rows; name the table row instead.
            text = re.sub(r"^endpoint (\d+):", lambda m: f"Row {numbers[int(m.group(1)) - 1]}:", str(exc))
            show_notice(self.result, f"Not added. {text[0].upper()}{text[1:]}")
            return
        show_notice(self.result, f"Added {len(endpoints)} endpoint candidate(s). Select them and Verify.")
        self.set_manual_rows([])
        self.context.changed()


def _first(detail: str) -> str:
    return detail.split("\n\n", 1)[0]
