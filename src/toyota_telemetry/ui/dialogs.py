"""Dialogs for live confirmation, endpoint review, and batch target edits."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from PySide6.QtCore import Signal
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

from toyota_telemetry.i18n import N_, _, ngettext
from toyota_telemetry.operator.editing import (
    REQUIREMENT_LABELS,
    format_condition,
    format_duration,
    parse_condition,
    parse_duration,
)


from .names import count, requirement_label
from .widgets import Disclosure, mark_live


EXPORT_FORMATS = {"csv": N_("CSV (spreadsheet)")}
# A short example per target field; the label already says what it is.
TARGET_EXAMPLES = {
    "minimum_samples": N_("e.g. 500: at least 500 readings"),
    "minimum_runs": N_("e.g. 3: seen in at least 3 recordings"),
    "minimum_span_seconds": N_("e.g. 30m: first and last reading 30 min apart"),
    "required_states": N_("e.g. 0,1,2: each of these raw values seen"),
    "minimum_transitions": N_("e.g. 10: the raw value changed 10 times"),
    "minimum_changes": N_("e.g. 20: moved by the minimum difference 20 times"),
    "minimum_applicable_seconds": N_("e.g. 10m: 10 min recorded while Collect when holds"),
    "minimum_difference": N_("e.g. 0.5: smaller steps don't count as changes"),
    "condition": N_("e.g. 372/0x1002/0..15 > 10; empty means always"),
    "included": N_("Paused keeps the target but leaves it out of plans"),
}


# Up to this many states are offered as check boxes; more stay a typed list.
STATE_PICKER_LIMIT = 16


class StatePicker(QWidget):
    """Required states as check boxes named by their text.

    Reads and writes the same "0,1" raw list as the plain field it replaces.
    """

    textChanged = Signal(str)  # noqa: N815 - matches QLineEdit

    def __init__(self, states: dict[str, str], selected: Iterable[int] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        chosen = {int(value) for value in selected}
        self.boxes: dict[int, QCheckBox] = {}
        for index, raw in enumerate(sorted(states, key=int)):
            box = QCheckBox(states[raw])
            box.setToolTip(_("Raw value {raw}").format(raw=raw))
            box.setChecked(int(raw) in chosen)
            box.toggled.connect(lambda _checked: self.textChanged.emit(self.text()))
            grid.addWidget(box, index // 4, index % 4)
            self.boxes[int(raw)] = box

    def text(self) -> str:
        return ",".join(str(raw) for raw, box in sorted(self.boxes.items()) if box.isChecked())

    def setText(self, text: str) -> None:  # noqa: N802 - matches QLineEdit
        chosen = {int(value) for value in str(text).split(",") if value.strip()}
        for raw, box in self.boxes.items():
            box.setChecked(raw in chosen)


class ExportDialog(QDialog):
    """Choose which recorded samples to export and in which format."""

    def __init__(
        self,
        signal_name: str | None,
        signal_count: int,
        selected_runs: int,
        run_count: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Export samples"))
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.signals_selected = QRadioButton(
            _("Selected: {signal}").format(signal=signal_name) if signal_name else _("Selected signal")
        )
        self.signals_all = QRadioButton(
            ngettext("All {count} signal", "All {count} signals", signal_count).format(count=signal_count)
        )
        self.signals_selected.setEnabled(signal_name is not None)
        (self.signals_selected if signal_name else self.signals_all).setChecked(True)
        form.addRow(_("Signals"), _stack(self.signals_selected, self.signals_all))
        self.runs_selected = QRadioButton(
            ngettext("Selected {count} run", "Selected {count} runs", selected_runs).format(count=f"{selected_runs:,}")
            if selected_runs else _("Selected runs")
        )
        self.runs_all = QRadioButton(
            ngettext("All {count} run", "All {count} runs", run_count).format(count=f"{run_count:,}")
        )
        self.runs_selected.setEnabled(selected_runs > 0)
        (self.runs_selected if selected_runs else self.runs_all).setChecked(True)
        form.addRow(_("Runs"), _stack(self.runs_selected, self.runs_all))
        self.format = QComboBox()
        for key, label in EXPORT_FORMATS.items():
            self.format.addItem(_(label), key)
        form.addRow(_("Format"), self.format)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.export_button = buttons.addButton(_("Export"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choice(self) -> dict[str, str]:
        return {
            "signals": "selected" if self.signals_selected.isChecked() else "all",
            "runs": "selected" if self.runs_selected.isChecked() else "all",
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
        text.setAccessibleName(_("Exact live scope"))
        layout.addWidget(text, 1)
        technical = list(technical)
        if technical:
            raw = QPlainTextEdit("\n".join(technical))
            raw.setReadOnly(True)
            raw.setAccessibleName(_("Technical scope details"))
            toggle = Disclosure(_("Technical details"), raw)
            toggle.setToolTip(_("Request bytes and session steps"))
            layout.addWidget(toggle)
            layout.addWidget(raw)
        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.start_button = mark_live(QPushButton(_("Start")))
        self.start_button.setToolTip(
            _("Read-only requests. Never operate while driving.")
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


class EndpointConfirmDialog(_ValidatedDialog):
    def __init__(self, name: str, ecu_keys: list[int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Confirm {name}").format(name=name))
        form = QFormLayout(self)
        form.addRow(QLabel(_("Nothing is sent. This records that the endpoint\n"
                             "which answered is this ECU.")))
        self.ecus = QLineEdit(", ".join(str(key) for key in ecu_keys))
        self.ecus.setToolTip(_("Which ECUs answer at this endpoint"))
        self.note = QLineEdit()
        self.note.setPlaceholderText(_("optional"))
        self.note.setToolTip(_("How it was confirmed, for your own records"))
        form.addRow(_("ECU keys"), self.ecus)
        form.addRow(_("Note"), self.note)
        form.addRow(self._error_label())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def problem(self) -> str | None:
        if not parse_keys(self.ecus.text()):
            return _("Enter at least one ECU key.")
        return None

    def values(self) -> tuple[list[int], list[str]]:
        note = self.note.text().strip()
        return parse_keys(self.ecus.text()), [note] if note else []


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
        *,
        states: dict[str, str] | None = None,
    ) -> None:
        """``states`` are the selected signals' shared state text, if any."""

        super().__init__(parent)
        self.setWindowTitle(_("Batch edit collection targets") if signal_count > 1 else _("Edit collection target"))
        self.setMinimumWidth(620)
        self._preview = preview
        layout = QVBoxLayout(self)
        intro = QLabel(count(signal_count, "signal"))
        intro.setToolTip(_("0 or empty removes a requirement"))
        layout.addWidget(intro)
        grid = QGridLayout()
        self.enabled: dict[str, QCheckBox] = {}
        self.editors: dict[str, QWidget] = {}
        requirements = (current or {}).get("requirements", {})
        row = 0
        for name in REQUIREMENT_LABELS:
            label = requirement_label(name)
            if name in self.INTEGER_FIELDS:
                editor = QSpinBox()
                editor.setRange(0, 10_000_000)
                editor.setSpecialValueText(_("none"))
                editor.setValue(int(requirements.get(name, 0)))
            elif name in self.DURATION_FIELDS:
                editor = QLineEdit(format_duration(requirements[name]) if name in requirements else "")
                editor.setPlaceholderText(_("e.g. 30m or 2h; empty removes"))
            else:
                current_states = requirements.get(name, [])
                if (states and len(states) <= STATE_PICKER_LIMIT
                        and all(str(value) in states for value in current_states)):
                    editor = StatePicker(states, current_states)
                else:
                    editor = QLineEdit(",".join(str(value) for value in current_states))
                    editor.setPlaceholderText(_("raw states, e.g. 0,1,2; empty removes"))
            self._add_row(grid, row, name, label, editor)
            if isinstance(editor, StatePicker):
                self.enabled[name].setToolTip(_("Each checked state must be seen"))
            row += 1
        difference = QDoubleSpinBox()
        difference.setDecimals(4)
        difference.setRange(0, 1e9)
        difference.setSpecialValueText(_("none"))
        difference.setValue(float((current or {}).get("minimum_difference") or 0))
        self._add_row(grid, row, "minimum_difference", _("Minimum difference"), difference)
        row += 1
        condition = QLineEdit(format_condition((current or {}).get("condition")))
        condition.setPlaceholderText(_("372/0x1002/0..15 > 10 and ...; empty means always"))
        self._add_row(grid, row, "condition", _("Collect when"), condition)
        row += 1
        status = QComboBox()
        status.addItem(_("Active"), True)
        status.addItem(_("Paused"), False)
        status.setCurrentIndex(0 if (current or {}).get("included", True) else 1)
        self._add_row(grid, row, "included", _("Status"), status)
        layout.addLayout(grid)
        self.impact = QLabel()
        self.impact.setWordWrap(True)
        self.impact.setAccessibleName(_("Batch impact preview"))
        layout.addWidget(self.impact)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(_("Apply"))
        self.ok_button.setToolTip(_("Saved with Save on Signals"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_preview()

    def _add_row(self, grid: QGridLayout, row: int, name: str, label: str, editor: QWidget) -> None:
        change = QCheckBox(label)
        change.setAccessibleName(_("Change {field}").format(field=label))
        change.setToolTip(_(TARGET_EXAMPLES[name]))
        editor.setToolTip(_(TARGET_EXAMPLES[name]))
        editor.setEnabled(False)
        change.toggled.connect(editor.setEnabled)
        change.toggled.connect(lambda _checked: self.refresh_preview())
        for signal_name in ("valueChanged", "textChanged", "currentIndexChanged"):
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
        elif isinstance(editor, QComboBox):
            editor.setCurrentIndex(editor.findData(bool(value)))
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
                fields[name] = editor.currentData()
        fields["requirements"] = requirements
        return fields

    def refresh_preview(self) -> None:
        try:
            fields = self.fields()
            if not fields["requirements"] and len(fields) == 1:
                self.impact.setText(_("Check at least one field to change."))
                self.ok_button.setEnabled(False)
                return
            self.impact.setText(self._preview(fields))
            self.ok_button.setEnabled(True)
        except ValueError as exc:
            self.impact.setText(_("Cannot apply: {error}").format(error=exc))
            self.ok_button.setEnabled(False)


def parse_keys(text: str) -> list[int]:
    keys = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part:
            if not part.isdigit():
                raise ValueError(_("ECU key must be a number: {value}").format(value=repr(part)))
            keys.append(int(part))
    return keys
