"""Recordings: recovery, runs, signal plots across all valid runs, export, result."""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator import bounded_minmax

from .context import PageContext
from .widgets import button_row, notice, page_title, set_enabled, show_notice
from .widgets.metric_plot import MetricPlot


RUN_COLUMNS = ("Started (UTC)", "State", "Duration", "Reads", "Failed", "Endpoint", "Note", "Run")


class RecordingsPage(QWidget):
    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self.choose_export_path = lambda: QFileDialog.getSaveFileName(
            self, "Export signal data", "signal-data.csv", "CSV files (*.csv)"
        )[0]
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            "Recordings",
            "Every valid run is included. Interrupted runs are listed for recovery; recovery and "
            "export never open the connector.",
        ))
        self.recovery = QGroupBox("Interrupted runs")
        recovery_layout = QVBoxLayout(self.recovery)
        self.recovery_text = QLabel()
        self.recovery_text.setWordWrap(True)
        self.recover_button = QPushButton("Recover")
        self.quarantine_button = QPushButton("Quarantine")
        self.context.register(
            "recordings.recover", self.recover_button,
            "Keep the run's valid prefix as a closed run. The connector is not opened.",
        )
        self.context.register(
            "recordings.quarantine", self.quarantine_button,
            "Move the run out of the capture tree without deleting it.",
        )
        self.recover_button.clicked.connect(self.recover)
        self.quarantine_button.clicked.connect(self.quarantine)
        recovery_layout.addWidget(self.recovery_text)
        recovery_layout.addLayout(button_row(self.recover_button, self.quarantine_button))
        layout.addWidget(self.recovery)

        splitter = QSplitter(Qt.Orientation.Vertical)
        runs_panel = QWidget()
        runs_layout = QVBoxLayout(runs_panel)
        runs_layout.setContentsMargins(0, 0, 0, 0)
        self.runs = QTableWidget(0, len(RUN_COLUMNS))
        self.runs.setHorizontalHeaderLabels(RUN_COLUMNS)
        self.runs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.runs.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.runs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.runs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.runs.horizontalHeader().setStretchLastSection(True)
        self.runs.setAccessibleName("Recorded runs")
        self.context.register("recordings.list", self.runs, "Recorded runs; select one for details")
        self.runs.itemSelectionChanged.connect(self.show_run)
        self.run_detail = QPlainTextEdit()
        self.run_detail.setReadOnly(True)
        self.run_detail.setMaximumHeight(130)
        self.run_detail.setAccessibleName("Selected run details")
        self.context.register("recordings.show", self.run_detail, "Details of the selected run")
        runs_layout.addWidget(self.runs, 1)
        runs_layout.addWidget(self.run_detail)
        splitter.addWidget(runs_panel)

        analysis = QWidget()
        analysis_layout = QHBoxLayout(analysis)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        self.signal_search = QLineEdit()
        self.signal_search.setPlaceholderText("Search")
        self.signal_search.setToolTip("Filter recorded signals by name or reference")
        self.signal_search.textChanged.connect(self._filter_signals)
        self.signals = QListWidget()
        self.signals.setAccessibleName("Recorded signals")
        self.signals.currentItemChanged.connect(lambda *_: self.plot_current())
        left.addWidget(self.signal_search)
        left.addWidget(self.signals, 1)
        analysis_layout.addLayout(left, 1)
        right = QVBoxLayout()
        self.plot = MetricPlot()
        self.plot.viewport_width_changed.connect(lambda _width: self.plot_current())
        self.signals.setToolTip("Signals with recorded samples; select one to plot it across every valid run")
        self.cursor = QLabel()
        self.cursor.setWordWrap(True)
        self.plot.cursor_changed.connect(self.cursor.setText)
        self.plot_summary = QLabel()
        self.plot_summary.setWordWrap(True)
        right.addWidget(self.plot, 1)
        right.addWidget(self.cursor)
        right.addWidget(self.plot_summary)
        analysis_layout.addLayout(right, 3)
        splitter.addWidget(analysis)
        layout.addWidget(splitter, 1)

        self.export_button = QPushButton("Export CSV…")
        self.result_button = QPushButton("Generate result")
        self.context.register(
            "recordings.export", self.export_button,
            "Export the selected signal's samples (or all signals when none is selected) as CSV.",
        )
        self.context.register(
            "result.generate", self.result_button,
            "Write the tracked, privacy-minimized project result under result/.",
        )
        self.export_button.clicked.connect(self.export)
        self.result_button.clicked.connect(self.generate_result)
        layout.addLayout(button_row(self.export_button, self.result_button))
        self.result_text = notice()
        layout.addWidget(self.result_text)
        context.project_changed.connect(self.refresh)
        context.data_changed.connect(self.refresh)
        context.busy_changed.connect(lambda _busy: self._update_buttons())
        self._interrupted: list[str] = []
        self.refresh()

    def refresh(self) -> None:
        service = self.context.service
        self.runs.setRowCount(0)
        self.signals.clear()
        self._interrupted = []
        if service is None:
            self.recovery.setVisible(False)
            self._update_buttons()
            return
        try:
            runs = service.runs()
            tree = service.tree()
            summary = service.observation_summary()
        except (OSError, ValueError) as exc:
            self.run_detail.setPlainText(f"Cannot read recordings: {exc}")
            self._update_buttons()
            return
        for run in runs:
            row = self.runs.rowCount()
            self.runs.insertRow(row)
            duration = f"{run.duration_seconds:.0f} s" if run.duration_seconds is not None else "-"
            values = (
                run.created_utc or "unknown", run.state, duration, str(run.exchanges), str(run.failed),
                run.endpoint_id or "", run.note or "", run.run_id,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, run.run_id)
                self.runs.setItem(row, column, item)
            if run.state == "needs recovery":
                self._interrupted.append(run.run_id)
        live = self.context.live_job_running
        self.recovery.setVisible(bool(self._interrupted) and not live)
        if self._interrupted:
            self.recovery_text.setText(f"{len(self._interrupted)} interrupted run(s), next: {self._interrupted[0]}")
        names = {signal.signal_ref: signal for _, _, signal in tree.signals()}
        for signal_ref in sorted(summary):
            signal = names.get(signal_ref)
            label = f"{signal.name if signal else signal_ref}  ({summary[signal_ref]['observations']} samples)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, signal_ref)
            item.setToolTip(signal_ref)
            self.signals.addItem(item)
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.context.service is not None
        live = self.context.live_job_running
        blocked = "Open a project first." if not has else "Wait for the live job to finish." if live else ""
        set_enabled(self.recover_button, has and bool(self._interrupted) and not live, blocked or "No interrupted run.")
        set_enabled(self.quarantine_button, has and bool(self._interrupted) and not live, blocked or "No interrupted run.")
        set_enabled(self.export_button, has and self.signals.count() > 0, blocked or "No recorded signals.")
        set_enabled(self.result_button, has and not live, blocked)

    def _filter_signals(self, text: str) -> None:
        needle = text.lower()
        for index in range(self.signals.count()):
            item = self.signals.item(index)
            item.setHidden(bool(needle) and needle not in item.text().lower() and needle not in item.toolTip().lower())

    def show_run(self) -> None:
        items = self.runs.selectedItems()
        if not items or self.context.service is None:
            return
        run = self.context.service.run(items[0].data(Qt.ItemDataRole.UserRole))
        lines = [
            f"Run {run.run_id}: {run.state}" + (f" ({run.end_reason})" if run.end_reason else ""),
            f"Started {run.created_utc or 'unknown'} on {run.endpoint_id or 'unknown endpoint'}; note {run.note or '-'}",
        ]
        lines += [
            f"Segment {row['segment']} {row['state']}: {row['exchanges']} reads, {row['failed']} failed, "
            f"{row['observations']} observations"
            for row in run.segment_rows
        ]
        if run.detail:
            lines.append(run.detail)
        self.run_detail.setPlainText("\n".join(lines))

    def select_signal(self, signal_ref: str) -> None:
        for index in range(self.signals.count()):
            item = self.signals.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == signal_ref:
                self.signals.setCurrentItem(item)
                return
        raise KeyError(signal_ref)

    def plot_current(self) -> None:
        item = self.signals.currentItem()
        service = self.context.service
        if item is None or service is None:
            return
        signal_ref = item.data(Qt.ItemDataRole.UserRole)
        rows = service.series(signal_ref)
        xs: list[float] = []
        ys: list[float] = []
        offset = 0.0
        current_run = None
        run_start = 0.0
        last = 0.0
        runs = 0
        for row in rows:
            seconds = row["response_elapsed_ms"] / 1000
            if row["run_id"] != current_run:
                if current_run is not None:
                    offset = last + 1.0
                    xs.append(offset)
                    ys.append(math.nan)
                current_run = row["run_id"]
                run_start = seconds
                runs += 1
            last = offset + (seconds - run_start)
            xs.append(last)
            ys.append(math.nan if row["decoded_value"] is None else float(row["decoded_value"]))
        width = max(1, int(self.plot.getPlotItem().getViewBox().width()) or 600)
        series = bounded_minmax(xs, ys, width)
        self.plot.set_series(series)
        found = service.tree().find_signal(signal_ref)
        name, unit = (found[2].name, found[2].unit) if found else (signal_ref, "")
        self.plot.set_measurement(name, unit)
        finite = [value for value in ys if math.isfinite(value)]
        if finite:
            constant = min(finite) == max(finite)
            self.plot_summary.setText(
                f"{len(rows)} samples, {runs} run(s). "
                + (f"Constant value {finite[-1]:g}." if constant else
                   f"Min {min(finite):g}, max {max(finite):g}, last {finite[-1]:g}.")
            )
            self.plot_summary.setToolTip("Runs are drawn end to end with gaps; they are never joined.")
        else:
            self.plot_summary.setText(f"{len(rows)} samples, {runs} run(s); no decoded values.")

    def export(self) -> None:
        service = self.context.service
        item = self.signals.currentItem()
        if service is None:
            return
        refs = [item.data(Qt.ItemDataRole.UserRole)] if item is not None else [
            self.signals.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.signals.count())
        ]
        path = self.choose_export_path()
        if not path:
            return
        with Path(path).open("w", encoding="utf-8", newline="") as stream:
            count = service.export(refs, stream)
        show_notice(self.result_text, f"Exported {count} sample(s) to {path}.")

    def recover(self) -> None:
        if self.context.service is None or not self._interrupted:
            return
        try:
            path = self.context.service.recover(self._interrupted[0])
        except (OSError, ValueError) as exc:
            self.context.error("Run not recovered", exc)
            return
        self.context.message(f"Recovered valid prefix into {Path(path).name}.")
        self.context.changed()

    def quarantine(self) -> None:
        if self.context.service is None or not self._interrupted:
            return
        try:
            path = self.context.service.quarantine(self._interrupted[0])
        except (OSError, ValueError) as exc:
            self.context.error("Run not quarantined", exc)
            return
        self.context.message(f"Moved the run to {path}.")
        self.context.changed()

    def generate_result(self) -> None:
        if self.context.service is None:
            return
        try:
            manifest = self.context.service.generate_result()
        except (OSError, ValueError) as exc:
            self.context.error("Result not generated", exc)
            return
        show_notice(
            self.result_text,
            f"Generated result {manifest['generation_id']} ({manifest['row_counts']['signals']} signals).",
        )
