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
        self.plot.viewport_width_changed.connect(lambda _width: self._redraw(reset_view=False))
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
        self._interrupted: list[str] = []
        self._names: dict[str, tuple[str, str]] = {}
        self._series: dict[str, dict] = {}
        self._shown: str | None = None
        self._stale = True
        context.project_changed.connect(self._project_changed)
        context.data_changed.connect(self._mark_stale)
        context.busy_changed.connect(lambda _busy: self._update_buttons())
        self._update_buttons()

    def _project_changed(self) -> None:
        self._show_listing(None)
        self._mark_stale()

    def _mark_stale(self) -> None:
        self._stale = True
        self._series.clear()
        if self.isVisible():
            self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._stale:
            self.refresh()

    def refresh(self) -> None:
        """Load runs and recorded signals in the background."""

        self._stale = False
        if self.context.service is None:
            self._show_listing(None)
            return

        def work(service):
            tree = service.tree()
            names = {signal.signal_ref: (signal.name, signal.unit) for _, _, signal in tree.signals()}
            return {"runs": service.runs(), "summary": service.observation_summary(), "names": names}

        def failed(detail: str) -> None:
            self.run_detail.setPlainText(f"Cannot read recordings: {detail.split(chr(10) * 2, 1)[0]}")

        if self.runs.rowCount() == 0:
            self.run_detail.setPlainText("Loading…")
        self.context.run("recordings.list", work, self._show_listing, failed)

    def _show_listing(self, listing: dict | None) -> None:
        selected_run = self._selected_run()
        current = self.signals.currentItem()
        current_signal = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.runs.setRowCount(0)
        self.signals.blockSignals(True)
        self.signals.clear()
        self.signals.blockSignals(False)
        self._interrupted = []
        self._names = {}
        if listing is None:
            self.recovery.setVisible(False)
            self.run_detail.setPlainText("")
            self._shown = None
            self._update_buttons()
            return
        for run in listing["runs"]:
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
        if self.run_detail.toPlainText() == "Loading…":
            self.run_detail.setPlainText("")
        live = self.context.live_job_running
        self.recovery.setVisible(bool(self._interrupted) and not live)
        if self._interrupted:
            self.recovery_text.setText(f"{len(self._interrupted)} interrupted run(s), next: {self._interrupted[0]}")
        self._names = listing["names"]
        summary = listing["summary"]
        for signal_ref in sorted(summary):
            name = self._names.get(signal_ref, (signal_ref, ""))[0]
            item = QListWidgetItem(f"{name}  ({summary[signal_ref]['observations']} samples)")
            item.setData(Qt.ItemDataRole.UserRole, signal_ref)
            item.setToolTip(signal_ref)
            self.signals.addItem(item)
        self._filter_signals(self.signal_search.text())
        if selected_run is not None:
            for row in range(self.runs.rowCount()):
                if self.runs.item(row, 0).data(Qt.ItemDataRole.UserRole) == selected_run:
                    self.runs.selectRow(row)
                    break
        if current_signal is not None:
            try:
                self.select_signal(current_signal)
            except KeyError:
                pass
        self._update_buttons()

    def _selected_run(self) -> str | None:
        items = self.runs.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

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
        run_id = self._selected_run()
        if run_id is None or self.context.service is None:
            return

        def done(run) -> None:
            if self._selected_run() != run.run_id:
                return
            lines = [
                f"Run {run.run_id}: {run.state}" + (f" ({run.end_reason})" if run.end_reason else ""),
                f"Started {run.created_utc or 'unknown'} on {run.endpoint_id or 'unknown endpoint'}; "
                f"note {run.note or '-'}",
            ]
            lines += [
                f"Segment {row['segment']} {row['state']}: {row['exchanges']} reads, {row['failed']} failed, "
                f"{row['observations']} observations"
                for row in run.segment_rows
            ]
            if run.detail:
                lines.append(run.detail)
            self.run_detail.setPlainText("\n".join(lines))

        self.context.run("recordings.run", lambda service: service.run(run_id), done)

    def select_signal(self, signal_ref: str) -> None:
        for index in range(self.signals.count()):
            item = self.signals.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == signal_ref:
                self.signals.setCurrentItem(item)
                return
        raise KeyError(signal_ref)

    def plot_current(self) -> None:
        """Show the selected signal; samples load once per signal in the background."""

        item = self.signals.currentItem()
        if item is None or self.context.service is None:
            return
        signal_ref = item.data(Qt.ItemDataRole.UserRole)
        if signal_ref in self._series:
            self._shown = signal_ref
            self._redraw(reset_view=True)
            return
        self.plot_summary.setText("Loading…")

        def done(data: dict) -> None:
            self._series[signal_ref] = data
            current = self.signals.currentItem()
            if current is not None and current.data(Qt.ItemDataRole.UserRole) == signal_ref:
                self._shown = signal_ref
                self._redraw(reset_view=True)

        self.context.run("recordings.series", lambda service: _series_data(service.series(signal_ref)), done)

    def _redraw(self, *, reset_view: bool) -> None:
        data = self._series.get(self._shown) if self._shown else None
        if data is None:
            return
        width = max(1, int(self.plot.getPlotItem().getViewBox().width()) or 600)
        self.plot.set_series(bounded_minmax(data["x"], data["y"], width), reset_view=reset_view)
        if not reset_view:
            return
        name, unit = self._names.get(self._shown, (self._shown, ""))
        self.plot.set_measurement(name, unit)
        finite = data["finite"]
        if finite:
            minimum, maximum, last = finite
            self.plot_summary.setText(
                f"{data['samples']} samples, {data['runs']} run(s). "
                + (f"Constant value {last:g}." if minimum == maximum else
                   f"Min {minimum:g}, max {maximum:g}, last {last:g}.")
            )
            self.plot_summary.setToolTip("Runs are drawn end to end with gaps; they are never joined.")
        else:
            self.plot_summary.setText(f"{data['samples']} samples, {data['runs']} run(s); no decoded values.")

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

        def work(service):
            with Path(path).open("w", encoding="utf-8", newline="") as stream:
                return service.export(refs, stream)

        def done(count: int) -> None:
            self.export_button.setEnabled(True)
            show_notice(self.result_text, f"Exported {count} sample(s) to {path}.")

        def failed(detail: str) -> None:
            self.export_button.setEnabled(True)
            self.context.error("Export failed", detail.split("\n\n", 1)[0])

        self.export_button.setEnabled(False)
        show_notice(self.result_text, "Exporting…")
        self.context.run("recordings.export", work, done, failed)

    def recover(self) -> None:
        self._run_interrupted("recover", "Run not recovered", lambda path: f"Recovered into {Path(path).name}.")

    def quarantine(self) -> None:
        self._run_interrupted("quarantine", "Run not quarantined", lambda path: f"Moved the run to {path}.")

    def _run_interrupted(self, action: str, title: str, message) -> None:
        if self.context.service is None or not self._interrupted:
            return
        run_id = self._interrupted[0]

        def done(path) -> None:
            self.context.message(message(path))
            self.context.changed()

        def failed(detail: str) -> None:
            self._update_buttons()
            self.context.error(title, detail.split("\n\n", 1)[0])

        self.recover_button.setEnabled(False)
        self.quarantine_button.setEnabled(False)
        self.context.run(
            f"recordings.{action}", lambda service: getattr(service, action)(run_id), done, failed
        )

    def generate_result(self) -> None:
        if self.context.service is None:
            return

        def done(manifest) -> None:
            self.result_button.setEnabled(True)
            show_notice(
                self.result_text,
                f"Generated result {manifest['generation_id']} ({manifest['row_counts']['signals']} signals).",
            )

        def failed(detail: str) -> None:
            self.result_button.setEnabled(True)
            show_notice(self.result_text, "")
            self.context.error("Result not generated", detail.split("\n\n", 1)[0])

        self.result_button.setEnabled(False)
        show_notice(self.result_text, "Generating…")
        self.context.run("recordings.result", lambda service: service.generate_result(), done, failed)


def _series_data(rows: list[dict]) -> dict:
    """Lay runs end to end with a NaN gap between them so lines never join."""

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
    finite = [value for value in ys if math.isfinite(value)]
    return {
        "x": xs,
        "y": ys,
        "samples": len(rows),
        "runs": runs,
        "finite": (min(finite), max(finite), finite[-1]) if finite else None,
    }
