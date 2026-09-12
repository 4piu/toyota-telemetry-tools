"""The sole PyQtGraph import boundary for metric evidence rendering."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtGui import QPalette

from toyota_telemetry.operator import BoundedSeries


class MetricPlot(pg.PlotWidget):
    viewport_width_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent=parent)
        self.setAccessibleName("Metric evidence plot")
        self.setMinimumHeight(280)
        self.showGrid(x=True, y=True, alpha=0.18)
        self.setLabel("bottom", "Elapsed time", units="s")
        self.setLabel("left", "Synthetic value")
        self.getPlotItem().setMenuEnabled(False)
        self._curve = self.plot([], [], connect="finite")
        self._rendered_points = 0
        self._source_points = 0

    @property
    def rendered_points(self) -> int:
        return self._rendered_points

    @property
    def source_points(self) -> int:
        return self._source_points

    def set_series(self, series: BoundedSeries) -> None:
        accent = self.palette().color(QPalette.ColorRole.Highlight)
        self._curve.setPen(pg.mkPen(accent, width=1.4))
        self._curve.setData(series.x, series.y, connect="finite")
        self._source_points = series.source_points
        self._rendered_points = series.rendered_points
        self.setAccessibleDescription(
            f"Synthetic evidence preview with {series.rendered_points} rendered "
            f"points reduced from {series.source_points} source points."
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        plot_item = getattr(self, "plotItem", None)
        if plot_item is not None:
            width = plot_item.getViewBox().width()
            self.viewport_width_changed.emit(max(1, int(width)))
