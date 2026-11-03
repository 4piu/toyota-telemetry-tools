"""Small native-Qt building blocks shared by the operator pages.

Visible text stays short: labels and buttons name things and actions.
Explanations live in tooltips.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)


LIVE_TIP = "Transmits to the vehicle. You confirm the exact scope first."


def page_title(title: str, description: str) -> QWidget:
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 4)
    label = QLabel(title)
    font = label.font()
    font.setPointSize(font.pointSize() + 6)
    font.setBold(True)
    label.setFont(font)
    label.setToolTip(description)
    label.setAccessibleDescription(description)
    layout.addWidget(label)
    return container


def mark_live(button: QPushButton) -> QPushButton:
    """Give a vehicle-transmitting action its warning icon and weight."""

    font = button.font()
    font.setBold(True)
    button.setFont(font)
    button.setIcon(button.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning))
    # Text-sized, so the button keeps the height of its unmarked neighbours.
    side = button.fontMetrics().height()
    button.setIconSize(QSize(side, side))
    button.setProperty("liveAction", True)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button


def set_enabled(widget: QWidget, enabled: bool, reason: str = "") -> None:
    """Enable a control; when disabled, its tooltip says why."""

    widget.setEnabled(enabled)
    base = widget.property("baseToolTip") or widget.toolTip()
    if widget.property("baseToolTip") is None:
        widget.setProperty("baseToolTip", base)
    widget.setToolTip(base if enabled or not reason else f"{base}\n\nUnavailable: {reason}")


def notice(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setFrameShape(QFrame.Shape.StyledPanel)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setContentsMargins(8, 6, 8, 6)
    label.setVisible(bool(text))
    return label


def show_notice(label: QLabel, text: str) -> None:
    label.setText(text)
    label.setVisible(bool(text))


def path_field(field: QLineEdit, caption: str, *, directory: bool = False) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(field, 1)
    browse = QPushButton("Browse")
    browse.setToolTip(caption)
    browse.setAccessibleName(f"Browse for {caption}")

    def choose() -> None:
        if directory:
            selected = QFileDialog.getExistingDirectory(container, caption, field.text())
        else:
            selected, _ = QFileDialog.getOpenFileName(container, caption, field.text())
        if selected:
            field.setText(selected)

    browse.clicked.connect(choose)
    layout.addWidget(browse)
    return container


def button_row(*widgets: QWidget, stretch_first: bool = False) -> QHBoxLayout:
    row = QHBoxLayout()
    if stretch_first:
        row.addStretch(1)
    for widget in widgets:
        row.addWidget(widget)
    if not stretch_first:
        row.addStretch(1)
    return row
