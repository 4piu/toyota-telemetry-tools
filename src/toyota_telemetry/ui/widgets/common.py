"""Small native-Qt building blocks shared by the operator pages."""

from __future__ import annotations

from PySide6.QtCore import Qt
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


def page_title(title: str, subtitle: str) -> QWidget:
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(4)
    label = QLabel(title)
    font = label.font()
    font.setPointSize(font.pointSize() + 6)
    font.setBold(True)
    label.setFont(font)
    detail = QLabel(subtitle)
    detail.setWordWrap(True)
    layout.addWidget(label)
    layout.addWidget(detail)
    return container


def mark_live(button: QPushButton) -> QPushButton:
    """Give a vehicle-transmitting action its warning icon, weight, and LIVE text."""

    font = button.font()
    font.setBold(True)
    button.setFont(font)
    if not button.text().startswith("LIVE"):
        button.setText(f"LIVE - {button.text()}")
    button.setIcon(button.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning))
    button.setProperty("liveAction", True)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button


def notice(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setFrameShape(QFrame.Shape.StyledPanel)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setContentsMargins(8, 6, 8, 6)
    return label


def path_field(field: QLineEdit, caption: str, *, directory: bool = False) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(field, 1)
    browse = QPushButton("Browse…")
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
