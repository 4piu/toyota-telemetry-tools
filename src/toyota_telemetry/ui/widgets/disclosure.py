"""A collapsible section header: ▸ when folded, ▾ when open."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QToolButton, QWidget


class Disclosure(QToolButton):
    def __init__(self, title: str, content: QWidget, *, expanded: bool = False) -> None:
        super().__init__()
        self.content = content
        self.setText(title)
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setStyleSheet("QToolButton { border: none; font-weight: bold; padding: 2px 0; }")
        self.toggled.connect(self._toggled)
        self.setChecked(expanded)
        self._toggled(expanded)

    def _toggled(self, expanded: bool) -> None:
        self.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)
