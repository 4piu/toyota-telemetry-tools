"""Dialogs for live confirmation, endpoint review, and batch target edits."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.editing import (
    REQUIREMENT_LABELS,
    format_condition,
    format_duration,
    parse_condition,
    parse_duration,
)

from toyota_telemetry.operator.sources import canonical_can_id

from .widgets import Disclosure, mark_live


EXPORT_FORMATS = {"csv": "CSV (spreadsheet)"}


class ExportDialog(QDialog):
    """Choose which recorded samples to export and in which format."""

    def __init__(
        self,
        signal_name: str | None,
        signal_count: int,
        selected_drives: int,
        drive_count: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export samples")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.signals_selected = QRadioButton(f"Selected: {signal_name}" if signal_name else "Selected signal")
        self.signals_all = QRadioButton(f"All {signal_count} signals")
        self.signals_selected.setEnabled(signal_name is not None)
        (self.signals_selected if signal_name else self.signals_all).setChecked(True)
        form.addRow("Signals", _stack(self.signals_selected, self.signals_all))
        self.drives_selected = QRadioButton(
            f"Selected {selected_drives} drive" + ("s" if selected_drives != 1 else "") if selected_drives
            else "Selected drives"
        )
        self.drives_all = QRadioButton(f"All {drive_count} drives")
        self.drives_selected.setEnabled(selected_drives > 0)
        (self.drives_selected if selected_drives else self.drives_all).setChecked(True)
        form.addRow("Drives", _stack(self.drives_selected, self.drives_all))
        self.format = QComboBox()
        for key, label in EXPORT_FORMATS.items():
            self.format.addItem(label, key)
        form.addRow("Format", self.format)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.export_button = buttons.addButton("Export", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choice(self) -> dict[str, str]:
        return {
            "signals": "selected" if self.signals_selected.isChecked() else "all",
            "drives": "selected" if self.drives_selected.isChecked() else "all",
            "format": self.format.currentData(),
        }


def _stack(*widgets: QWidget) -> QWidget:
    container = QWidget()
    column = QVBoxLayout(container)
    column.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        column.addWidget(widget)
    return container


class LiveConfirmDialog(QDialog):
    """Lists the exact scope; Start is never the default button."""

    def __init__(
        self,
        title: str,
        summary: str,
        details: Iterable[str],
        parent: QWidget | None = None,
        *,
        technical: Iterable[str] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        heading = QLabel(summary)
        heading.setWordWrap(True)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        text = QPlainTextEdit("\n".join(details))
        text.setReadOnly(True)
        text.setAccessibleName("Exact live scope")
        layout.addWidget(text, 1)
        technical = list(technical)
        if technical:
            raw = QPlainTextEdit("\n".join(technical))
            raw.setReadOnly(True)
            raw.setAccessibleName("Technical scope details")
            toggle = Disclosure("Technical details", raw)
            toggle.setToolTip("Request bytes and session steps")
            layout.addWidget(toggle)
            layout.addWidget(raw)
        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.start_button = mark_live(QPushButton("Start"))
        self.start_button.setToolTip(
            "Read-only requests. Never operate while driving."
        )
        buttons.addButton(self.start_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class _ValidatedDialog(QDialog):
    """A dialog that checks its fields before closing and keeps them on error."""

    def _error_label(self) -> QLabel:
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: palette(bright-text); font-weight: bold;")
        self.error.setVisible(False)
        return self.error

    def problem(self) -> str | None:
        return None

    def accept(self) -> None:
        try:
            problem = self.problem()
        except ValueError as exc:
            problem = str(exc)
        if problem:
            self.error.setText(problem)
            self.error.setVisible(True)
            return
        super().accept()


class EndpointAddDialog(_ValidatedDialog):
    def __init__(self, recipes: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add endpoint candidate")
        form = QFormLayout(self)
        self.tester = QLineEdit()
        self.tester.setPlaceholderText("e.g. 0x7E0")
        self.response = QLineEdit()
        self.response.setPlaceholderText("e.g. 0x7E8")
        self.recipe = QComboBox()
        self.recipe.addItems(recipes)
        self.ecus = QLineEdit()
        self.ecus.setPlaceholderText("required, e.g. 372, 374")
        form.addRow("Tester CAN ID", self.tester)
        form.addRow("Response CAN ID", self.response)
        form.addRow("Session", self.recipe)
        form.addRow("ECU keys", self.ecus)
        self.setToolTip("Adds a candidate; nothing is transmitted")
        self.recipe.setToolTip("Diagnostic session to open")
        self.ecus.setToolTip("ECU keys that answer here")
        form.addRow(self._error_label())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def problem(self) -> str | None:
        tester = canonical_can_id(self.tester.text())
        response = canonical_can_id(self.response.text())
        if tester is None or response is None:
            return "Enter both CAN IDs as hexadecimal, e.g. 0x7E0 and 0x7E8."
        if tester == response:
            return "The tester and response CAN IDs must differ."
        if not parse_keys(self.ecus.text()):
            return "Enter at least one ECU key."
        return None

    def values(self) -> tuple[str, str, str, list[int]]:
        return self.tester.text(), self.response.text(), self.recipe.currentText(), parse_keys(self.ecus.text())


class EndpointConfirmDialog(_ValidatedDialog):
    def __init__(self, endpoint_id: str, ecu_keys: list[int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Confirm endpoint {endpoint_id}")
        
        form = QFormLayout(self)
        self.ecus = QLineEdit(", ".join(str(key) for key in ecu_keys))
        self.ecus.setToolTip("Which ECUs answer at this endpoint")
        self.source = QLineEdit()
        self.source.setPlaceholderText("required")
        self.source.setToolTip(
            "Path in the deliverable repository, e.g. docs/evidence/endpoints.md"
        )
        form.addRow("ECU keys", self.ecus)
        form.addRow("Evidence", self.source)
        form.addRow(self._error_label())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def problem(self) -> str | None:
        if not parse_keys(self.ecus.text()):
            return "Enter at least one ECU key."
        sources = self.values()[1]
        if not sources:
            return "Enter where the evidence for this endpoint is written up."
        for source in sources:
            path = PurePosixPath(source)
            if path.is_absolute() or ":" in source or ".." in path.parts or path.parts[:1] in (("captures",), ("state",)):
                return f"{source}: use a relative path in the deliverable repository."
        return None

    def values(self) -> tuple[list[int], list[str]]:
        sources = [value.strip().replace("\\", "/") for value in self.source.text().split(",") if value.strip()]
        return parse_keys(self.ecus.text()), sources


class TargetEditDialog(QDialog):
    """Batch edit: only fields whose Change box is checked are applied."""

    INTEGER_FIELDS = ("minimum_samples", "minimum_runs", "minimum_transitions", "minimum_changes")
    DURATION_FIELDS = ("minimum_span_seconds", "minimum_applicable_seconds")

    def __init__(
        self,
        signal_count: int,
        current: dict[str, Any] | None,
        preview: Callable[[dict[str, Any]], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Batch edit collection targets" if signal_count > 1 else "Edit collection target")
        self.setMinimumWidth(620)
        self._preview = preview
        layout = QVBoxLayout(self)
        intro = QLabel(f"{signal_count} signal(s)")
        intro.setToolTip("0 or empty removes a requirement")
        layout.addWidget(intro)
        grid = QGridLayout()
        self.enabled: dict[str, QCheckBox] = {}
        self.editors: dict[str, QWidget] = {}
        requirements = (current or {}).get("requirements", {})
        row = 0
        for name, label in REQUIREMENT_LABELS.items():
            if name in self.INTEGER_FIELDS:
                editor = QSpinBox()
                editor.setRange(0, 10_000_000)
                editor.setSpecialValueText("none")
                editor.setValue(int(requirements.get(name, 0)))
            elif name in self.DURATION_FIELDS:
                editor = QLineEdit(format_duration(requirements[name]) if name in requirements else "")
                editor.setPlaceholderText("e.g. 30m or 2h; empty removes")
            else:
                editor = QLineEdit(",".join(str(value) for value in requirements.get(name, [])))
                editor.setPlaceholderText("raw states, e.g. 0,1,2; empty removes")
            self._add_row(grid, row, name, label, editor)
            row += 1
        difference = QDoubleSpinBox()
        difference.setDecimals(4)
        difference.setRange(0, 1e9)
        difference.setSpecialValueText("none")
        difference.setValue(float((current or {}).get("minimum_difference") or 0))
        self._add_row(grid, row, "minimum_difference", "Minimum difference", difference)
        row += 1
        condition = QLineEdit(format_condition((current or {}).get("condition")))
        condition.setPlaceholderText("372/0x1002/0..15 > 10 and ...; empty means always")
        self._add_row(grid, row, "condition", "Collect when", condition)
        row += 1
        included = QCheckBox("Yes")
        included.setToolTip("Unchecked keeps it but skips collection")
        included.setChecked(bool((current or {}).get("included", True)))
        self._add_row(grid, row, "included", "Collect this signal", included)
        layout.addLayout(grid)
        difference.setToolTip("Smallest step that counts as a change")
        condition.setToolTip("Count samples only while this holds; empty means always")
        self.impact = QLabel()
        self.impact.setWordWrap(True)
        self.impact.setAccessibleName("Batch impact preview")
        layout.addWidget(self.impact)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Apply")
        self.ok_button.setToolTip("Saved with Save on Signals")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_preview()

    def _add_row(self, grid: QGridLayout, row: int, name: str, label: str, editor: QWidget) -> None:
        change = QCheckBox(label)
        change.setAccessibleName(f"Change {label}")
        change.setToolTip(f"Change {label.lower()}")
        editor.setEnabled(False)
        change.toggled.connect(editor.setEnabled)
        change.toggled.connect(lambda _checked: self.refresh_preview())
        for signal_name in ("valueChanged", "textChanged", "toggled"):
            signal = getattr(editor, signal_name, None)
            if signal is not None:
                signal.connect(lambda *_: self.refresh_preview())
                break
        grid.addWidget(change, row, 0)
        grid.addWidget(editor, row, 1)
        self.enabled[name] = change
        self.editors[name] = editor

    def set_field(self, name: str, value: Any) -> None:
        """Enable one field and set its value (used by tests and shortcuts)."""

        editor = self.editors[name]
        self.enabled[name].setChecked(True)
        if isinstance(editor, (QSpinBox, QDoubleSpinBox)):
            editor.setValue(value)
        elif isinstance(editor, QCheckBox):
            editor.setChecked(bool(value))
        else:
            editor.setText(str(value))

    def fields(self) -> dict[str, Any]:
        requirements: dict[str, Any] = {}
        fields: dict[str, Any] = {}
        for name, change in self.enabled.items():
            if not change.isChecked():
                continue
            editor = self.editors[name]
            if name in self.INTEGER_FIELDS:
                requirements[name] = editor.value()
            elif name in self.DURATION_FIELDS:
                text = editor.text().strip()
                requirements[name] = parse_duration(text) if text else 0
            elif name == "required_states":
                text = editor.text().strip()
                requirements[name] = [int(value) for value in text.split(",") if value.strip()] if text else 0
            elif name == "minimum_difference":
                fields[name] = editor.value() or None
            elif name == "condition":
                fields[name] = parse_condition(editor.text())
            elif name == "included":
                fields[name] = editor.isChecked()
        fields["requirements"] = requirements
        return fields

    def refresh_preview(self) -> None:
        try:
            fields = self.fields()
            if not fields["requirements"] and len(fields) == 1:
                self.impact.setText("Check at least one field to change.")
                self.ok_button.setEnabled(False)
                return
            self.impact.setText(self._preview(fields))
            self.ok_button.setEnabled(True)
        except ValueError as exc:
            self.impact.setText(f"Cannot apply: {exc}")
            self.ok_button.setEnabled(False)


def parse_keys(text: str) -> list[int]:
    keys = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part:
            if not part.isdigit():
                raise ValueError(f"ECU key must be a number: {part!r}")
            keys.append(int(part))
    return keys
