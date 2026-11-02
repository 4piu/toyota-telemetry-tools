"""The sole PyQtGraph import boundary for metric evidence rendering."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtGui import QPalette

from toyota_telemetry.operator import BoundedSeries


class ElapsedTimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):  # noqa: N802 - Qt API
        labels = []
        for value in values:
            seconds = max(0, int(round(value * scale)))
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            labels.append(f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}")
        return labels


class MetricPlot(pg.PlotWidget):
    viewport_width_changed = Signal(int)
    cursor_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent=parent, axisItems={"bottom": ElapsedTimeAxis(orientation="bottom")})
        self.setAccessibleName("Metric evidence plot")
        self.setMinimumHeight(280)
        self.showGrid(x=True, y=True, alpha=0.18)
        self.setLabel("bottom", "Run time, runs end to end (h:mm:ss)")
        self.setLabel("left", "Value")
        self.getPlotItem().setMenuEnabled(False)
        self._curve = self.plot([], [], connect="finite")
        # A sensible empty view, so the axes read 0:00 to 1:00 instead of repeated labels.
        self.getPlotItem().getViewBox().setRange(xRange=(0, 60), yRange=(0, 1), padding=0)
        self._cursor = pg.InfiniteLine(angle=90, movable=False)
        self._cursor.setVisible(False)
        self.addItem(self._cursor, ignoreBounds=True)
        self.scene().sigMouseMoved.connect(self._mouse_moved)
        self._unit = ""
        self._measurement = "Value"
        self._last_series: BoundedSeries | None = None
        self._rendered_points = 0
        self._source_points = 0

    @property
    def rendered_points(self) -> int:
        return self._rendered_points

    @property
    def source_points(self) -> int:
        return self._source_points

    def set_series(self, series: BoundedSeries, *, reset_view: bool = True) -> None:
        """Show a reduced series; ``reset_view`` fits both axes to it.

        Redraws of the same signal after a resize keep the user's zoom.
        """

        accent = self.palette().color(QPalette.ColorRole.Highlight)
        self._curve.setPen(pg.mkPen(accent, width=1.4))
        self._curve.setData(series.x, series.y, connect="finite")
        if reset_view:
            self.getPlotItem().enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
            self.getPlotItem().getViewBox().updateAutoRange()
        self._last_series = series
        self._source_points = series.source_points
        self._rendered_points = series.rendered_points
        self.setAccessibleDescription(
            f"Signal plot with {series.rendered_points} rendered "
            f"points reduced from {series.source_points} recorded samples."
        )

    def set_measurement(self, name: str, unit: str) -> None:
        self._measurement = name
        self._unit = "" if unit in ("state", "raw bytes", "text") else unit
        label = name if not self._unit else f"{name} ({self._unit})"
        self.setLabel("left", label)

    def _mouse_moved(self, scene_position) -> None:
        if not self.sceneBoundingRect().contains(scene_position):
            self._cursor.setVisible(False)
            return
        point = self.getPlotItem().getViewBox().mapSceneToView(scene_position)
        self._cursor.setPos(point.x())
        self._cursor.setVisible(True)
        seconds = max(0, int(point.x()))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        elapsed = f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"
        suffix = f" {self._unit}" if self._unit else ""
        self.cursor_changed.emit(
            f"Cursor: {elapsed} on the run timeline | {self._measurement}: {point.y():.2f}{suffix}"
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        plot_item = getattr(self, "plotItem", None)
        if plot_item is not None:
            width = plot_item.getViewBox().width()
            self.viewport_width_changed.emit(max(1, int(width)))
