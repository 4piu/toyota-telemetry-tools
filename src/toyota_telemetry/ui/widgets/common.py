"""Small native-Qt building blocks shared by the operator preview pages."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
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
    detail.setProperty("muted", True)
    layout.addWidget(label)
    layout.addWidget(detail)
    return container


class ReadinessCard(QFrame):
    """Compact status/evidence/action card for the Overview dashboard."""

    def __init__(self, title: str, status: str, evidence: str, action: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(210)
        self.setMinimumHeight(135)
        self.setMaximumHeight(180)
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        name = QLabel(title)
        font = name.font()
        font.setBold(True)
        name.setFont(font)
        state = QLabel(status)
        state.setWordWrap(True)
        state.setAlignment(Qt.AlignmentFlag.AlignRight)
        heading.addWidget(name, 1)
        heading.addWidget(state)
        layout.addLayout(heading)
        self.detail = QLabel(evidence)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
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


def disabled_action(button: QPushButton, detail: str, *, live: bool = False) -> QWidget:
    """Return a disabled preview action with a visible, accessible reason."""

    disable_preview(button, detail)
    if live:
        font = button.font()
        font.setBold(True)
        button.setFont(font)
        button.setText(f"LIVE - {button.text()}")
        button.setIcon(button.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning))
        button.setProperty("liveAction", True)
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignRight)
    reason = QLabel(detail)
    reason.setWordWrap(True)
    reason.setMaximumWidth(520)
    reason.setProperty("muted", True)
    reason.setAccessibleName(f"Why {button.text()} is unavailable")
    layout.addWidget(reason, alignment=Qt.AlignmentFlag.AlignRight)
    return container
