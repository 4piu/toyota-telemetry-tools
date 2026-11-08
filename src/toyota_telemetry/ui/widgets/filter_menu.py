"""A Filter button whose menu holds grouped checkboxes.

Options in one group widen the result (any of them); groups narrow it (all of
them), as in spreadsheet and mail filters. Each option is a visible checkbox,
so several can be ticked; the menu stays open while ticking. A long group
(such as the ECU list) scrolls inside the menu.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QLabel,
    QMenu,
    QScrollArea,
    QStyle,
    QStyleOptionToolButton,
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from toyota_telemetry.i18n import _

from .icons import filter_icon

# Groups with more options than this scroll; the box shows about this many rows.
VISIBLE_ROWS = 10
# Room on the right for the drop-down chevron.
CHEVRON = 16


class FilterButton(QToolButton):
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setIcon(filter_icon())
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setAccessibleName(_("Filter"))
        self.setToolTip(_("Filter rows; tick any number of options"))
        self.menu_ = QMenu(self)
        self.setMenu(self.menu_)
        self.groups: dict[str, list[tuple[str, str, str]]] = {}
        self.actions_: dict[str, QCheckBox] = {}
        self._rebuild()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        option = self._option()
        hint = self.style().sizeFromContents(
            QStyle.ContentsType.CT_ToolButton, option, option.iconSize + QSize(self.fontMetrics().horizontalAdvance(
                self.text()) + 6, 0), self,
        )
        return QSize(hint.width() + CHEVRON, max(hint.height(), super().sizeHint().height()))

    def _option(self) -> QStyleOptionToolButton:
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        # Qt's own menu indicator is a small corner triangle; a chevron is drawn instead.
        option.features &= ~QStyleOptionToolButton.ToolButtonFeature.HasMenu
        return option

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        option = self._option()
        painter = QStylePainter(self)
        bevel = QStyleOptionToolButton(option)
        bevel.text = ""
        bevel.icon = QIcon()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, bevel)
        label = QStyleOptionToolButton(option)
        label.rect = option.rect.adjusted(4, 0, -CHEVRON, 0)
        painter.drawControl(QStyle.ControlElement.CE_ToolButtonLabel, label)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.palette().buttonText().color() if self.isEnabled() else self.palette().placeholderText().color()
        painter.setPen(QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        x = option.rect.right() - CHEVRON / 2 - 2
        y = option.rect.center().y() + 0.5
        painter.drawPolyline([QPointF(x - 3.5, y - 1.8), QPointF(x, y + 1.8), QPointF(x + 3.5, y - 1.8)])

    def set_group(self, title: str, options: list[tuple[str, str, str]]) -> None:
        """Add or replace a group of (key, label, tooltip) options; ticks on kept keys survive."""

        self.groups[title] = options
        self._rebuild()

    def active(self, title: str) -> set[str]:
        return {key for key, _label, _tip in self.groups.get(title, ()) if self.actions_[key].isChecked()}

    def clear(self) -> None:
        for box in self.actions_.values():
            box.blockSignals(True)
            box.setChecked(False)
            box.blockSignals(False)
        self._changed()

    def _rebuild(self) -> None:
        ticked = {key for key, box in self.actions_.items() if box.isChecked()}
        self.menu_.clear()  # deletes the previous checkboxes with their actions
        boxes: dict[str, QCheckBox] = {}
        for title, options in self.groups.items():
            if not options:
                continue
            header = QLabel(title)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            header.setContentsMargins(10, 6, 10, 2)
            self._add_widget(header)
            container = QWidget()
            layout = QVBoxLayout(container)
            layout.setContentsMargins(10, 0, 10, 4)
            layout.setSpacing(2)
            for key, label, tip in options:
                box = QCheckBox(label)
                box.setToolTip(tip)
                box.setChecked(key in ticked)
                box.toggled.connect(lambda _checked: self._changed())
                layout.addWidget(box)
                boxes[key] = box
            if len(options) > VISIBLE_ROWS:
                scroll = QScrollArea()
                scroll.setFrameShape(QFrame.Shape.NoFrame)
                scroll.setWidgetResizable(True)
                scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                scroll.setWidget(container)
                # Blend into the menu rather than the window's grey.
                container.setAutoFillBackground(False)
                scroll.viewport().setAutoFillBackground(False)
                row = max(box.sizeHint().height() for box in boxes.values()) + layout.spacing()
                scroll.setFixedHeight(row * VISIBLE_ROWS + layout.contentsMargins().bottom())
                bar = scroll.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
                scroll.setMinimumWidth(container.sizeHint().width() + bar)
                scroll.setAccessibleName(_("{title} options").format(title=title))
                self._add_widget(scroll)
            else:
                self._add_widget(container)
        self.menu_.addSeparator()
        clear = self.menu_.addAction(_("Clear filters"))
        clear.triggered.connect(self.clear)
        self.actions_ = boxes
        self._update_text()

    def _add_widget(self, widget: QWidget) -> None:
        action = QWidgetAction(self.menu_)
        action.setDefaultWidget(widget)
        self.menu_.addAction(action)

    def count(self) -> int:
        return sum(1 for box in self.actions_.values() if box.isChecked())

    def _update_text(self) -> None:
        count = self.count()
        self.setText(_("Filter ({count})").format(count=count) if count else _("Filter"))

    def _changed(self) -> None:
        self._update_text()
        self.changed.emit()
