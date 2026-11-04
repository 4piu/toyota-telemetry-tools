"""A Filter button whose menu holds grouped checkboxes.

Options in one group widen the result (any of them); groups narrow it (all of
them), as in spreadsheet and mail filters. The menu stays open while ticking.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QMenu, QToolButton, QWidgetAction

from .icons import filter_icon


class StayOpenMenu(QMenu):
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        action = self.activeAction()
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


class FilterButton(QToolButton):
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setIcon(filter_icon())
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setAccessibleName("Filter")
        self.setToolTip("Show only some rows. Ticks in one group add rows; groups combine to narrow them.")
        self.menu_ = StayOpenMenu(self)
        self.setMenu(self.menu_)
        self.groups: dict[str, list[tuple[str, str, str]]] = {}
        self.actions_: dict[str, QAction] = {}
        self._group_of: dict[str, str] = {}
        self._rebuild()

    def set_group(self, title: str, options: list[tuple[str, str, str]]) -> None:
        """Add or replace a group of (key, label, tooltip) options; ticks on kept keys survive."""

        self.groups[title] = options
        self._rebuild()

    def active(self, title: str) -> set[str]:
        return {key for key, _label, _tip in self.groups.get(title, ()) if self.actions_[key].isChecked()}

    def clear(self) -> None:
        for action in self.actions_.values():
            action.blockSignals(True)
            action.setChecked(False)
            action.blockSignals(False)
        self._changed()

    def _rebuild(self) -> None:
        self.menu_.clear()
        kept: dict[str, QAction] = {}
        for title, options in self.groups.items():
            if not options:
                continue
            header = QLabel(title)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            header.setContentsMargins(10, 6, 10, 2)
            widget_action = QWidgetAction(self.menu_)
            widget_action.setDefaultWidget(header)
            self.menu_.addAction(widget_action)
            for key, label, tip in options:
                action = self.actions_.get(key)
                if action is None:
                    action = QAction(label, self)
                    action.setCheckable(True)
                    action.toggled.connect(lambda _checked: self._changed())
                action.setText(label)
                action.setToolTip(tip)
                kept[key] = action
                self.menu_.addAction(action)
        self.menu_.setToolTipsVisible(True)
        self.menu_.addSeparator()
        clear = self.menu_.addAction("Clear filters")
        clear.triggered.connect(self.clear)
        self.actions_ = kept
        self._update_text()

    def count(self) -> int:
        return sum(1 for action in self.actions_.values() if action.isChecked())

    def _update_text(self) -> None:
        count = self.count()
        self.setText(f"Filter ({count})" if count else "Filter")

    def _changed(self) -> None:
        self._update_text()
        self.changed.emit()
