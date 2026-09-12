"""Foundation pages proving the desktop interaction and rendering boundaries."""

from __future__ import annotations

from pathlib import Path
import threading

import numpy as np
from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator import BoundedSeries, bounded_minmax

from .models import SyntheticSignalModel
from .tasks import LatestTaskController
from .widgets import MetricPlot


def _title(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPointSize(font.pointSize() + 6)
    font.setBold(True)
    label.setFont(font)
    return label


class HomePage(QWidget):
    choose_project = Signal()

    def __init__(self, project_path: Path | None = None) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)
        layout.addWidget(_title("Telemetry workspace"))

        self.project_label = QLabel()
        self.project_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.set_project(project_path)
        layout.addWidget(self.project_label)

        rail = QFrame()
        rail.setFrameShape(QFrame.Shape.StyledPanel)
        rail_layout = QHBoxLayout(rail)
        for phase in ("Prepare", "Curate", "Plan", "Collect", "Evidence"):
            phase_label = QLabel(phase)
            phase_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            phase_label.setAccessibleDescription(f"Workflow phase: {phase}")
            rail_layout.addWidget(phase_label)
        layout.addWidget(rail)

        banner = QFrame()
        banner.setFrameShape(QFrame.Shape.StyledPanel)
        banner_layout = QVBoxLayout(banner)
        banner_layout.addWidget(QLabel("Foundation prototype"))
        banner_layout.addWidget(
            QLabel(
                "Explore the virtual signal table and bounded evidence plot. "
                "Project readiness and editing arrive in the next slices."
            )
        )
        button = QPushButton("Open existing project")
        button.clicked.connect(self.choose_project)
        banner_layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(banner)
        layout.addStretch(1)

    def set_project(self, path: Path | None) -> None:
        if path is None:
            self.project_label.setText(
                "No project open. Project creation remains a separate template/bootstrap activity."
            )
        else:
            self.project_label.setText(f"Selected project: {path}")


class SignalPrototypePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_title("Curate signals — scale prototype"))
        description = QLabel(
            "10,000 generated rows exercise Qt's virtual model/view path; no vehicle data is loaded."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter synthetic signals, endpoints, or DIDs")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Filter synthetic signals")
        layout.addWidget(self.search)

        self.model = SyntheticSignalModel()
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setDynamicSortFilter(True)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAccessibleName("Synthetic signal table")
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 230)
        layout.addWidget(self.table, 1)


class EvidencePrototypePage(QWidget):
    SOURCE_POINTS = 1_000_000

    def __init__(self) -> None:
        super().__init__()
        self._controller = LatestTaskController(parent=self)
        self._controller.result.connect(self._set_result)
        self._controller.failed.connect(self._set_failure)
        self._controller.busy_changed.connect(self._set_busy)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self.refresh)

        self._x = np.arange(self.SOURCE_POINTS, dtype=np.float64) / 100.0
        self._y = (
            np.sin(self._x * 0.017) * 18.0
            + np.sin(self._x * 0.29) * 2.5
            + np.cos(self._x * 0.003) * 6.0
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_title("Evidence — bounded rendering prototype"))
        self.status = QLabel("Waiting to build synthetic evidence view…")
        self.status.setAccessibleName("Evidence query status")
        layout.addWidget(self.status)
        self.plot = MetricPlot()
        self.plot.viewport_width_changed.connect(self._schedule_refresh)
        layout.addWidget(self.plot, 1)
        refresh = QPushButton("Rebuild visible range")
        refresh.clicked.connect(self.refresh)
        layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignRight)
        QTimer.singleShot(0, self.refresh)

    def _schedule_refresh(self, _width: int) -> None:
        self._debounce.start()

    def refresh(self) -> None:
        width = max(100, int(self.plot.getPlotItem().getViewBox().width()))
        x = self._x
        y = self._y

        def work(cancel_event: threading.Event) -> BoundedSeries:
            return bounded_minmax(x, y, width, cancelled=cancel_event.is_set)

        self._controller.start(work)

    def cancel(self) -> None:
        self._debounce.stop()
        self._controller.cancel()

    def _set_result(self, value: object) -> None:
        if not isinstance(value, BoundedSeries):
            self._set_failure("Evidence worker returned an unexpected result")
            return
        self.plot.set_series(value)
        self.status.setText(
            f"Rendered {value.rendered_points:,} representative points from "
            f"{value.source_points:,} source points."
        )

    def _set_failure(self, detail: str) -> None:
        self.status.setText(f"Could not build evidence preview: {detail.splitlines()[-1]}")

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.status.setText("Building the newest bounded viewport…")


class PlaceholderPage(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.addWidget(_title(title))
        text = QLabel("This workflow page is intentionally outside the Foundation prototype.")
        text.setWordWrap(True)
        layout.addWidget(text)
        layout.addStretch(1)
