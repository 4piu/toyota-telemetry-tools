"""Visible "loading" cues: a spinner and an overlay that dims a busy area.

The overlay waits a moment before showing, so quick loads do not flash.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


SHOW_DELAY_MS = 150


class Spinner(QWidget):
    """A rotating arc in the palette's highlight colour."""

    def __init__(self, size: int = 18, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._step)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)

    def _step(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = max(2.0, self.width() / 8)
        rect = QRectF(width, width, self.width() - 2 * width, self.height() - 2 * width)
        track = QColor(self.palette().color(QPalette.ColorRole.Mid))
        painter.setPen(QPen(track, width))
        painter.drawEllipse(rect)
        pen = QPen(self.palette().color(QPalette.ColorRole.Highlight), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, -self._angle * 16, 100 * 16)


class BusyIndicator(QWidget):
    """Spinner and text for a status bar; shown while ``set_busy(True)``."""

    def __init__(self, text: str = "Loading…", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 6, 0)
        self.spinner = Spinner(14)
        self.label = QLabel(text)
        row.addWidget(self.spinner)
        row.addWidget(self.label)
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(SHOW_DELAY_MS)
        self._delay.timeout.connect(self.show)
        self.busy = False
        self.hide()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self._delay.start()
        else:
            self._delay.stop()
            self.hide()


class LoadingOverlay(QWidget):
    """Covers ``target`` with a translucent layer, a spinner, and a short text."""

    def __init__(self, target: QWidget) -> None:
        super().__init__(target)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        column = QVBoxLayout(self)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spinner = Spinner(28)
        self.label = QLabel("Loading…")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self.label, 0, Qt.AlignmentFlag.AlignHCenter)
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(SHOW_DELAY_MS)
        self._delay.timeout.connect(self._appear)
        self.active = False
        target.installEventFilter(self)
        self.hide()

    def start(self, text: str = "Loading…") -> None:
        self.label.setText(text)
        self.active = True
        if not self.isVisible():
            self._delay.start()

    def stop(self) -> None:
        self.active = False
        self._delay.stop()
        self.hide()

    def _appear(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return False

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        color = QColor(self.palette().color(QPalette.ColorRole.Base))
        color.setAlpha(190)
        painter.fillRect(self.rect(), color)
