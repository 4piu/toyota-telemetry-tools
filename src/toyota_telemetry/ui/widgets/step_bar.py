"""The step bar: page navigation that also shows how far the project has come."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget


DONE = "done"
NEXT = "next"
ATTENTION = "attention"
PENDING = "pending"
MARKS = {DONE: "✓", NEXT: "●", ATTENTION: "!", PENDING: "○"}
STATE_WORDS = {DONE: "done", NEXT: "next step", ATTENTION: "needs attention", PENDING: "not started"}


@dataclass(frozen=True)
class Part:
    """One line under a step, e.g. ("Connector", DONE, "pcan")."""

    label: str
    state: str
    value: str = ""

    def text(self) -> str:
        value = f" {self.value}" if self.value else ""
        return f"{MARKS[self.state]} {self.label}{value}"


class StepButton(QPushButton):
    """A checkable button laid out as a title and a few status lines."""

    def __init__(self, number: int, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("step")
        self.setCheckable(True)
        self.setAutoDefault(False)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Fill the bar's height, so steps with fewer status lines line up with
        # the tallest instead of being centred at their own height.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.number = number
        self.title = title
        self.setAccessibleName(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 8)
        layout.setSpacing(2)
        self.heading = QLabel()
        font = self.heading.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.1)
        self.heading.setFont(font)
        self.lines = QLabel()
        self.lines.setTextFormat(Qt.TextFormat.PlainText)
        for label in (self.heading, self.lines):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label)
        layout.addStretch(1)
        self.set_state(PENDING, [])

    def set_state(self, state: str, parts: list[Part]) -> None:
        self.state = state
        self.parts = list(parts)
        self.heading.setText(f"{self.number}  {self.title}  {MARKS[state]}")
        self.lines.setText("\n".join(part.text() for part in parts))
        self.setProperty("stepState", state)
        summary = "; ".join(f"{part.label}{' ' + part.value if part.value else ''}: {STATE_WORDS[part.state]}"
                            for part in parts)
        self.setAccessibleDescription(f"{STATE_WORDS[state]}. {summary}")
        self.style().unpolish(self)
        self.style().polish(self)

    def minimumSizeHint(self):  # noqa: N802 - Qt API
        return self.layout().minimumSize()

    def sizeHint(self):  # noqa: N802 - Qt API
        return self.layout().sizeHint()


class StepBar(QWidget):
    """Numbered steps across the top; the current page is highlighted."""

    current_changed = Signal(int)

    def __init__(self, titles: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Steps")
        self.setObjectName("step-bar")
        # As tall as the tallest step; every step fills that height.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QPushButton#step { text-align: left; border: none; border-bottom: 3px solid transparent;"
            " border-radius: 0; padding: 0; }"
            "QPushButton#step:hover { background: palette(midlight); }"
            "QPushButton#step:checked { border-bottom: 3px solid palette(highlight);"
            " background: palette(alternate-base); }"
            "QPushButton#step:focus { border-bottom: 3px solid palette(dark); }"
            "QPushButton#step:checked:focus { border-bottom: 3px solid palette(highlight); }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.steps: list[StepButton] = []
        for index, title in enumerate(titles):
            button = StepButton(index + 1, title, self)
            button.clicked.connect(lambda _checked=False, row=index: self.setCurrentRow(row))
            layout.addWidget(button, 1)
            self.steps.append(button)
        self._current = -1
        self.setCurrentRow(0)

    def currentRow(self) -> int:  # noqa: N802 - mirrors QListWidget
        return self._current

    def setCurrentRow(self, row: int) -> None:  # noqa: N802 - mirrors QListWidget
        for index, button in enumerate(self.steps):
            button.setChecked(index == row)
        if row != self._current:
            self._current = row
            self.current_changed.emit(row)

    def set_step(self, row: int, state: str, parts: list[Part], tip: str = "") -> None:
        button = self.steps[row]
        button.set_state(state, parts)
        button.setToolTip(tip)
