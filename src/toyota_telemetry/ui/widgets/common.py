"""Small native-Qt building blocks shared by the operator preview pages."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
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
    detail.setProperty("muted", True)
    layout.addWidget(label)
    layout.addWidget(detail)
    return container


class ReadinessCard(QFrame):
    """Compact status/evidence/action card for the Home dashboard."""

    def __init__(self, title: str, status: str, evidence: str, action: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(235)
        self.setMinimumHeight(135)
        self.setMaximumHeight(180)
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        name = QLabel(title)
        font = name.font()
        font.setBold(True)
        name.setFont(font)
        state = QLabel(status)
        state.setAlignment(Qt.AlignmentFlag.AlignRight)
        heading.addWidget(name, 1)
        heading.addWidget(state)
        layout.addLayout(heading)
        detail = QLabel(evidence)
        detail.setWordWrap(True)
        layout.addWidget(detail)
        layout.addStretch(1)
        self.action = QPushButton(action)
        layout.addWidget(self.action, alignment=Qt.AlignmentFlag.AlignLeft)


def preview_notice(text: str) -> QFrame:
    notice = QFrame()
    notice.setFrameShape(QFrame.Shape.StyledPanel)
    layout = QHBoxLayout(notice)
    label = QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(label)
    return notice


def disable_preview(button: QPushButton, detail: str) -> None:
    button.setEnabled(False)
    button.setToolTip(detail)
    button.setAccessibleDescription(detail)
