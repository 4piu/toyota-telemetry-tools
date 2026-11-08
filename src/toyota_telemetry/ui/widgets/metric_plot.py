"""The sole PyQtGraph import boundary for metric evidence rendering.

Interaction follows monitoring dashboards: hover shows the nearest sample,
click pins it, drag selects a time range to zoom, double-click shows
everything, sideways scroll pans, and pinch or Ctrl+scroll zooms time. The
value axis always fits the samples in view.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QGraphicsRectItem

from toyota_telemetry.i18n import _


MIN_TIME_SPAN = 2.0  # seconds shown at most zoom, and around a lone sample


@dataclass(frozen=True)
class RunSpan:
    """Where one run lies on the end-to-end timeline."""

    run_id: str
    start: float
    end: float
    label: str = ""


def clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def _short(text: str, width: int = 14) -> str:
    """A state name short enough for an axis tick; the readout shows it whole."""

    return text if len(text) <= width else text[: width - 1] + "…"


class ElapsedTimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):  # noqa: N802 - Qt API
        return [clock(value * scale) for value in values]


class TimeViewBox(pg.ViewBox):
    """Time-only zoom and pan; gestures map to dashboard conventions."""

    double_clicked = Signal()
    clicked = Signal(QPointF)

    def __init__(self) -> None:
        super().__init__(enableMenu=False)
        self.setMouseEnabled(x=True, y=False)
        self._band: QGraphicsRectItem | None = None

    def wheelEvent(self, ev, axis=None):  # noqa: N802 - Qt API
        delta = ev.delta()
        horizontal = ev.orientation() == Qt.Orientation.Horizontal
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+scroll, and a pinch on most touchpads, zoom time around the cursor.
            self.zoom_time(0.999 ** delta, self.mapToView(ev.pos()).x())
            ev.accept()
        elif horizontal or ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.pan_time(-delta / 1200)
            ev.accept()
        else:
            ev.ignore()

    def zoom_time(self, factor: float, center: float) -> None:
        (low, high), _values = self.viewRange()
        self.setXRange(center - (center - low) * factor, center + (high - center) * factor, padding=0)

    def pan_time(self, fraction: float) -> None:
        (low, high), _values = self.viewRange()
        shift = (high - low) * fraction
        self.setXRange(low + shift, high + shift, padding=0)

    def mouseDragEvent(self, ev, axis=None):  # noqa: N802 - Qt API
        if ev.button() != Qt.MouseButton.LeftButton or ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # Shift+drag (or another button) pans along time.
            super().mouseDragEvent(ev, axis)
            return
        ev.accept()
        start = self.mapToView(ev.buttonDownPos()).x()
        current = self.mapToView(ev.pos()).x()
        if self._band is None:
            self._band = QGraphicsRectItem()
            color = self.palette().color(QPalette.ColorRole.Highlight)
            color.setAlpha(60)
            self._band.setBrush(color)
            self._band.setPen(pg.mkPen(None))
            self._band.setZValue(1e9)
            self.addItem(self._band, ignoreBounds=True)
        _x_range, (bottom, top) = self.viewRange()
        self._band.setRect(QRectF(min(start, current), bottom, abs(current - start), top - bottom))
        if ev.isFinish():
            self.removeItem(self._band)
            self._band = None
            if abs(ev.pos().x() - ev.buttonDownPos().x()) > 4:
                self.setXRange(min(start, current), max(start, current), padding=0)

    def mouseClickEvent(self, ev):  # noqa: N802 - Qt API
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        ev.accept()
        if ev.double():
            self.double_clicked.emit()
        else:
            self.clicked.emit(self.mapToView(ev.pos()))


class MetricPlot(pg.PlotWidget):
    cursor_changed = Signal(str)
    pinned_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        self._view = TimeViewBox()
        super().__init__(
            parent=parent, viewBox=self._view, axisItems={"bottom": ElapsedTimeAxis(orientation="bottom")}
        )
        self.setAccessibleName(_("Metric evidence plot"))
        self.setToolTip(
            _("Click pins a reading; drag zooms; double-click shows all")
        )
        self.setMinimumHeight(280)
        self.showGrid(x=True, y=True, alpha=0.18)
        self.setLabel("bottom", _("Run time, runs end to end (h:mm:ss)"))
        self.setLabel("left", _("Value"))
        item = self.getPlotItem()
        item.setMenuEnabled(False)
        item.hideButtons()
        self._curve = self.plot([], [], connect="finite")
        # Draw only what is in view, reduced to peaks per pixel, so long histories stay fast.
        self._curve.setClipToView(True)
        self._curve.setDownsampling(auto=True, method="peak")
        self._view.setAutoVisible(y=True)
        self._view.setRange(xRange=(0, 60), yRange=(0, 1), padding=0)
        self._view.double_clicked.connect(self.show_all)
        self._view.clicked.connect(self._pin)
        self._crosshair = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(style=Qt.PenStyle.DashLine))
        self._marker = pg.ScatterPlotItem(size=9)
        self._label = pg.TextItem(anchor=(0, 1))
        self._pin_marker = pg.ScatterPlotItem(size=11, symbol="d")
        self._pin_label = pg.TextItem(anchor=(0, 1))
        self._region = pg.LinearRegionItem(movable=False)
        self._region.setZValue(-10)
        for overlay in (self._crosshair, self._marker, self._label, self._pin_marker, self._pin_label, self._region):
            overlay.setVisible(False)
            self.addItem(overlay, ignoreBounds=True)
        self._boundaries: list[pg.InfiniteLine] = []
        self.scene().sigMouseMoved.connect(self._hover)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        self.viewport().grabGesture(Qt.GestureType.PinchGesture)
        self._unit = ""
        self._measurement = _("Value")
        self._x = np.empty(0)
        self._y = np.empty(0)
        self._raw: list[int | None] = []
        self._states: dict[str, str] = {}
        self.state_mode = False
        self._runs: list[RunSpan] = []

    @property
    def source_points(self) -> int:
        return len(self._x)

    def set_data(
        self,
        x: Sequence[float],
        y: Sequence[float],
        runs: Sequence[RunSpan] = (),
        *,
        raw: Sequence[int | None] | None = None,
        states: Mapping[str, str] | None = None,
        reset_view: bool = True,
    ) -> None:
        """Show every sample; ``reset_view`` fits the time axis to the data.

        With ``states`` (raw value text) and the samples' ``raw`` values, a
        signal whose every reading is a labelled state plots as steps on an
        axis named by its states; otherwise labels only appear in readouts.
        """

        self._x = np.asarray(x, dtype=float)
        self._y = np.asarray(y, dtype=float)
        self._raw = list(raw) if raw is not None else [None] * len(self._x)
        self._states = dict(states or {})
        self._runs = list(runs)
        finite = [(value, code) for value, code in zip(self._y, self._raw) if math.isfinite(value)]
        self.state_mode = bool(self._states) and bool(finite) and all(
            code is not None and str(code) in self._states and value == code for value, code in finite
        )
        accent = self.palette().color(QPalette.ColorRole.Highlight)
        self._curve.setPen(pg.mkPen(accent, width=1.4))
        if self.state_mode:
            # A state holds until the next reading: draw steps, not slopes.
            step_x = np.repeat(self._x, 2)[1:]
            step_y = np.repeat(self._y, 2)[:-1]
            step_y[1::2][np.isnan(self._y[1:])] = math.nan
            self._curve.setData(step_x, step_y, connect="finite")
            ticks = [(float(code), _short(text)) for code, text in sorted(
                ((int(code), text) for code, text in self._states.items()), key=lambda item: item[0])]
            self.getAxis("left").setTicks([ticks, []])
        else:
            self._curve.setData(self._x, self._y, connect="finite")
            self.getAxis("left").setTicks(None)
        for line in self._boundaries:
            self.removeItem(line)
        boundary_pen = pg.mkPen(self.palette().color(QPalette.ColorRole.Mid), style=Qt.PenStyle.DotLine)
        self._boundaries = [pg.InfiniteLine(run.start, angle=90, movable=False, pen=boundary_pen) for run in self._runs[1:]]
        for line in self._boundaries:
            self.addItem(line, ignoreBounds=True)
        self._clear_pin()
        self._region.setVisible(False)
        if len(self._x):
            # The limits must stay wider than the smallest zoom, or the view keeps
            # re-fitting itself (one sample, or all samples at the same time).
            margin = max(float(self._x[-1] - self._x[0]) * 0.05, MIN_TIME_SPAN)
            self._view.setLimits(xMin=float(self._x[0]) - margin, xMax=float(self._x[-1]) + margin,
                                 minXRange=MIN_TIME_SPAN / 2)
        if reset_view:
            self.show_all()
        self.setAccessibleDescription(
            _("Signal plot of {samples} recorded samples across {runs} run(s).").format(
                samples=len(self._x), runs=len(self._runs),
            )
        )

    def show_all(self) -> None:
        if len(self._x):
            low, high = float(self._x[0]), float(self._x[-1])
            if high - low < MIN_TIME_SPAN:
                middle = (low + high) / 2
                self._view.setXRange(middle - MIN_TIME_SPAN / 2, middle + MIN_TIME_SPAN / 2, padding=0)
            else:
                self._view.setXRange(low, high, padding=0.02)
        if self.state_mode:
            # Show every state, seen or not, so unreached ones are visible too.
            codes = [int(code) for code in self._states] + [float(value) for value in self._y if math.isfinite(value)]
            self._view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
            self._view.setYRange(min(codes) - 0.5, max(codes) + 0.5, padding=0)
        else:
            self._view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

    def highlight_run(self, run_id: str | None) -> bool:
        """Shade one run's stretch of the timeline; False when it has no samples here."""

        span = next((run for run in self._runs if run.run_id == run_id), None)
        self._region.setVisible(span is not None)
        if span is not None:
            self._region.setRegion((span.start, span.end))
        return span is not None

    def set_measurement(self, name: str, unit: str) -> None:
        self._measurement = name
        self._unit = "" if unit in ("state", "raw bytes", "text") else unit
        label = name if not self._unit else f"{name} ({self._unit})"
        self.setLabel("left", label)

    # Reading samples ---------------------------------------------------------

    def nearest(self, x: float) -> int | None:
        """Index of the finite sample closest in time to ``x``."""

        if not len(self._x):
            return None
        index = int(np.searchsorted(self._x, x))
        best = None
        for candidate in range(max(0, index - 64), min(len(self._x), index + 64)):
            if math.isfinite(self._y[candidate]) and (
                best is None or abs(self._x[candidate] - x) < abs(self._x[best] - x)
            ):
                best = candidate
        return best

    def describe(self, index: int) -> str:
        x, y = float(self._x[index]), float(self._y[index])
        run = next((run for run in self._runs if run.start <= x <= run.end), None)
        suffix = f" {self._unit}" if self._unit else ""
        where = clock(x - run.start if run else x)
        if run and run.label:
            where = _("{time} into {run}").format(time=where, run=run.label)
        code = self._raw[index] if index < len(self._raw) else None
        label = self._states.get(str(code)) if code is not None else None
        value = _("{state} (raw {code})").format(state=label, code=code) if label else f"{y:g}{suffix}"
        return _("{measurement}: {value}\n{where}").format(measurement=self._measurement, value=value, where=where)

    def _hover(self, scene_position) -> None:
        inside = self._view.sceneBoundingRect().contains(scene_position)
        index = self.nearest(self._view.mapSceneToView(scene_position).x()) if inside else None
        visible = index is not None
        for overlay in (self._crosshair, self._marker, self._label):
            overlay.setVisible(visible)
        if not visible:
            self.cursor_changed.emit("")
            return
        x, y = float(self._x[index]), float(self._y[index])
        self._crosshair.setPos(x)
        self._marker.setData([x], [y], brush=self.palette().color(QPalette.ColorRole.Highlight))
        text = self.describe(index)
        self._label.setText(text)
        self._label.setPos(x, y)
        self.cursor_changed.emit(text.replace("\n", " | "))

    def _pin(self, point: QPointF) -> None:
        index = self.nearest(point.x())
        if index is None:
            self._clear_pin()
            return
        x, y = float(self._x[index]), float(self._y[index])
        self._pin_marker.setData([x], [y], brush=self.palette().color(QPalette.ColorRole.Text))
        text = self.describe(index)
        self._pin_label.setText(text)
        self._pin_label.setPos(x, y)
        self._pin_marker.setVisible(True)
        self._pin_label.setVisible(True)
        self.pinned_changed.emit(_("Pinned: {reading}").format(reading=text.replace("\n", " | ")))

    def _clear_pin(self) -> None:
        self._pin_marker.setVisible(False)
        self._pin_label.setVisible(False)
        self.pinned_changed.emit("")

    # Gestures ----------------------------------------------------------------

    def viewportEvent(self, event) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.NativeGesture and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            center = self._view.mapSceneToView(self.mapToScene(event.position().toPoint())).x()
            self._view.zoom_time(1 - event.value(), center)
            return True
        if event.type() == QEvent.Type.Gesture:
            pinch = event.gesture(Qt.GestureType.PinchGesture)
            if pinch is not None and pinch.scaleFactor() > 0:
                (low, high), _values = self._view.viewRange()
                self._view.zoom_time(1 / pinch.scaleFactor(), (low + high) / 2)
                return True
        return super().viewportEvent(event)
