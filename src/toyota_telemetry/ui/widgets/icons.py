"""Small line icons drawn in the palette's text colour, so they follow the theme."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication


def _icon(draw) -> QIcon:
    color = QApplication.palette().windowText().color()
    pixmap = QPixmap(32, 32)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    draw(painter)
    painter.end()
    return QIcon(pixmap)


def filter_icon() -> QIcon:
    def draw(painter: QPainter) -> None:
        path = QPainterPath()
        path.addPolygon(QPolygonF([
            QPointF(2.5, 3), QPointF(13.5, 3), QPointF(9.2, 8.3), QPointF(9.2, 13),
            QPointF(6.8, 11.8), QPointF(6.8, 8.3), QPointF(2.5, 3),
        ]))
        painter.drawPath(path)

    return _icon(draw)


def search_icon() -> QIcon:
    def draw(painter: QPainter) -> None:
        painter.drawEllipse(QPointF(6.8, 6.8), 4.2, 4.2)
        painter.drawLine(QPointF(9.9, 9.9), QPointF(13.5, 13.5))

    return _icon(draw)
