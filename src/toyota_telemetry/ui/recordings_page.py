"""Recordings: runs, their plots, recovery, deletion, and sample export.

Runs are named by their local start time. Run and endpoint IDs stay out of
sight; the CLI uses them.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.recordings import NEEDS_RECOVERY, UNREADABLE, RunSummary

from .context import PageContext
from .dialogs import ExportDialog
from .names import count, endpoint_label
from .widgets import LoadingOverlay, button_row, page_title, search_icon, set_enabled
from .widgets.metric_plot import MetricPlot, RunSpan


RUN_COLUMNS = ("Started", "Length", "ECU", "Samples", "Status", "Note")
STATUS = {NEEDS_RECOVERY: "! Interrupted", UNREADABLE: "✕ Unreadable"}
KEY = Qt.ItemDataRole.UserRole
SORT = Qt.ItemDataRole.UserRole + 1
PREFIX = Qt.ItemDataRole.UserRole + 2


class SortItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine, theirs = self.data(SORT), other.data(SORT)
        if mine is not None and theirs is not None:
            return mine < theirs
        return super().__lt__(other)


class PrefixDelegate(QStyledItemDelegate):
    """Draws an item's PREFIX, greyed, before its text."""

    def paint(self, painter, option, index) -> None:
        prefix = index.data(PREFIX)
        if not prefix:
            super().paint(painter, option, index)
            return
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        text, option.text = option.text, ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, option.widget)
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, option.widget) + 1
        rect = rect.adjusted(margin, 0, -margin, 0)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        main = option.palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        dim = option.palette.color(QPalette.ColorRole.PlaceholderText)
        if selected:
            dim = main
            dim.setAlpha(160)
        lead = f"{prefix} · "
        metrics = option.fontMetrics
        align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        painter.save()
        painter.setFont(option.font)
        painter.setPen(dim)
        painter.drawText(rect, align, metrics.elidedText(lead, Qt.TextElideMode.ElideRight, rect.width()))
        rest = rect.adjusted(metrics.horizontalAdvance(lead), 0, 0, 0)
        if rest.width() > 0:
            painter.setPen(main)
            painter.drawText(rest, align, metrics.elidedText(text, Qt.TextElideMode.ElideRight, rest.width()))
        painter.restore()


class RecordingsPage(QWidget):
    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self.choose_export_path = lambda: QFileDialog.getSaveFileName(
            self, "Export samples", "samples.csv", "CSV files (*.csv)"
        )[0]
        self.run_dialog = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            "Recordings",
            "Select runs to see and plot their signals",
        ))

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._runs_panel())
        splitter.addWidget(self._plot_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 400])
        layout.addWidget(splitter, 1)

        self.export_button = QPushButton("Export")
        self.export_button.clicked.connect(self.export)
        self.context.register("recordings.export", self.export_button, "Export recorded samples")
        layout.addLayout(button_row(self.export_button))

        self._runs: list[RunSummary] = []
        self._labels: dict[str, str] = {}
        self._names: dict[str, tuple[str, str]] = {}
        self._places: dict[str, tuple[str, str]] = {}
        self._series: dict[str, list[dict]] = {}
        self._all_signals: dict[str, dict] = {}
        self._shown: str | None = None
        self._stale = True
        context.project_changed.connect(self._project_changed)
        context.data_changed.connect(self._mark_stale)
        context.busy_changed.connect(lambda _busy: self._update_buttons())
        self._update_buttons()

    # Layout -----------------------------------------------------------------

    def _runs_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.runs = self._table(RUN_COLUMNS, "Recorded runs")
        self.runs.setMinimumHeight(180)
        self.runs.itemSelectionChanged.connect(self._selection_changed)
        # Clicking below the last row, or Esc, goes back to all runs.
        self.runs.viewport().installEventFilter(self)
        self.context.register("recordings.list", self.runs, "Select runs to narrow the signals")
        self.runs_loading = LoadingOverlay(self.runs)
        layout.addWidget(self.runs, 1)
        self.run_detail = QLabel()
        self.run_detail.setWordWrap(True)
        self.context.register("recordings.show", self.run_detail, "The selected runs")
        self.all_button = QPushButton("Show all")
        self.all_button.setToolTip("Clear the selection (Esc)")
        self.all_button.clicked.connect(self.runs.clearSelection)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.runs, self.runs.clearSelection)
        self.recover_button = QPushButton("Recover")
        self.trash_button = QPushButton("Delete")
        self.context.register("recordings.recover", self.recover_button, "Keeps what was saved")
        self.context.register("recordings.trash", self.trash_button, "Moves its files to the system trash")
        self.recover_button.clicked.connect(self.recover)
        self.trash_button.clicked.connect(self.trash)
        actions = button_row(self.all_button, self.recover_button, self.trash_button)
        actions.insertWidget(0, self.run_detail, 1)
        layout.addLayout(actions)
        return panel

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if (
            watched is self.runs.viewport()
            and event.type() == QEvent.Type.MouseButtonPress
            and not self.runs.indexAt(event.position().toPoint()).isValid()
        ):
            self.runs.clearSelection()
        return super().eventFilter(watched, event)

    def _plot_panel(self) -> QWidget:
        panel = QWidget()
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        self.signal_search = QLineEdit()
        self.signal_search.setPlaceholderText("Search signals")
        self.signal_search.setClearButtonEnabled(True)
        self.signal_search.addAction(search_icon(), QLineEdit.ActionPosition.LeadingPosition)
        self.signal_search.textChanged.connect(self._filter_signals)
        self.signals = QListWidget()
        self.signals.setAccessibleName("Recorded signals")
        self.signals.setToolTip("Select one to plot it")
        self.signals.setItemDelegate(PrefixDelegate(self.signals))
        # Per-item scrolling counts rows hidden by the search, so a click
        # in filtered results jumped to the end of the list.
        self.signals.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.signals.currentItemChanged.connect(lambda *_: self.plot_current())
        self.signals_loading = LoadingOverlay(self.signals)
        left.addWidget(self.signal_search)
        left.addWidget(self.signals, 1)
        layout.addLayout(left, 1)
        right = QVBoxLayout()
        self.plot = MetricPlot()
        self.plot_loading = LoadingOverlay(self.plot)
        self.cursor = QLabel()
        self.pinned = QLabel()
        self.plot.cursor_changed.connect(self.cursor.setText)
        self.plot.pinned_changed.connect(self.pinned.setText)
        self.plot_summary = QLabel("Select a signal to plot it.")
        self.plot_summary.setWordWrap(True)
        right.addWidget(self.plot, 1)
        readings = QHBoxLayout()
        readings.addWidget(self.cursor, 1)
        readings.addWidget(self.pinned, 1)
        right.addLayout(readings)
        right.addWidget(self.plot_summary)
        layout.addLayout(right, 3)
        return panel

    @staticmethod
    def _table(columns, name: str) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(list(columns))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setStyleSheet("QTableView::item { padding: 3px 8px; }")
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((170, 80, 200, 90, 120)[: len(columns) - 1]):
            header.resizeSection(column, width)
        header.setStretchLastSection(True)
        table.setSortingEnabled(True)
        table.setAccessibleName(name)
        return table

    # Loading ------------------------------------------------------------------

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
            return {
                "runs": service.runs(),
                "summary": service.observation_summary(),
                "names": {signal.signal_ref: (signal.name, signal.unit) for _, _, signal in tree.signals()},
                "places": {
                    signal.signal_ref: (signal.ecu_name or f"ECU {signal.ecu_key}", did.data_id)
                    for _, did, signal in tree.signals()
                },
                "labels": {
                    endpoint.endpoint_id: endpoint_label(endpoint).split("  ")[0]
                    for endpoint in tree.endpoints if endpoint.endpoint_id
                },
            }

        def failed(detail: str) -> None:
            self.runs_loading.stop()
            self.signals_loading.stop()
            self.run_detail.setText(f"Cannot read recordings: {detail.split(chr(10) * 2, 1)[0]}")

        self.runs_loading.start("Loading runs…")
        self.signals_loading.start("Loading signals…")
        self.context.run("recordings.list", work, self._show_listing, failed)

    def _show_listing(self, listing: dict | None) -> None:
        self.runs_loading.stop()
        selected = set(self._selected_keys())
        current = self.signals.currentItem()
        current_signal = current.data(KEY) if current is not None else None
        self._runs = listing["runs"] if listing else []
        self._labels = listing["labels"] if listing else {}
        self._names = listing["names"] if listing else {}
        self._places = listing["places"] if listing else {}
        self._fill()
        self.runs.blockSignals(True)
        self.select_runs(list(selected))
        self.runs.blockSignals(False)
        self._all_signals = listing["summary"] if listing else {}
        self._show_selection()
        self._load_signals(current_signal)

    def _load_signals(self, keep: str | None = None) -> None:
        """List the signals recorded in the selected runs (all runs when none is selected)."""

        run_ids = [run.run_id for run in self.selected_runs()]
        if not run_ids or self.context.service is None:
            self._show_signals(self._all_signals, keep)
            return

        def done(summary: dict) -> None:
            if [run.run_id for run in self.selected_runs()] == run_ids:
                self._show_signals(summary, keep)

        self.signals_loading.start("Loading signals…")
        self.context.run(
            "recordings.signals", lambda service: service.observation_summary(run_ids), done,
        )

    def _show_signals(self, summary: dict, keep: str | None) -> None:
        self.signals_loading.stop()
        self.signals.blockSignals(True)
        self.signals.clear()
        for signal_ref, prefix, text, tip in signal_entries(summary, self._names, self._places):
            item = QListWidgetItem(text)
            item.setData(KEY, signal_ref)
            item.setData(PREFIX, prefix)
            item.setToolTip(tip)
            if prefix:
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{prefix} · {text}")
            self.signals.addItem(item)
        self.signals.blockSignals(False)
        self._filter_signals(self.signal_search.text())
        if keep is not None:
            try:
                self.select_signal(keep)
            except KeyError:
                pass
        self._update_buttons()

    def _fill(self) -> None:
        table = self.runs
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for run in self._runs:
            row = table.rowCount()
            table.insertRow(row)
            start = _utc(run.created_utc)
            values = [
                (_local(run), start or 0.0),
                (_length(run.duration_seconds), run.duration_seconds or 0.0),
                (self._labels.get(run.endpoint_id, "Unknown ECU") if run.endpoint_id else "-", None),
                (f"{run.observations:,}", run.observations),
                (STATUS.get(run.state, ""), int(run.state in STATUS)),
                (run.note or "", None),
            ]
            for column, (text, sort) in enumerate(values):
                item = SortItem(text)
                item.setData(KEY, run.run_id)
                item.setData(SORT, sort)
                if column == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, column, item)
            if run.state in STATUS:
                table.item(row, 4).setToolTip(
                    "Stopped without finishing" if run.state == NEEDS_RECOVERY else "Cannot be read"
                )
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.DescendingOrder)

    # Selection ----------------------------------------------------------------

    def _selected_keys(self) -> list[str]:
        return list(dict.fromkeys(index.data(KEY) for index in self.runs.selectionModel().selectedRows(0)))

    def selected_runs(self) -> list[RunSummary]:
        keys = set(self._selected_keys())
        return [run for run in self._runs if run.run_id in keys]

    def select_runs(self, run_ids: list[str]) -> None:
        """Select runs by ID (automation and tests)."""

        model = self.runs.selectionModel()
        model.clearSelection()
        for row in range(self.runs.rowCount()):
            if self.runs.item(row, 0).data(KEY) in run_ids:
                model.select(self.runs.model().index(row, 0), model.SelectionFlag.Select | model.SelectionFlag.Rows)

    def _selection_changed(self) -> None:
        self._show_selection()
        current = self.signals.currentItem()
        self._load_signals(current.data(KEY) if current is not None else None)
        self._redraw(reset_view=True)

    def _show_selection(self) -> None:
        runs = self.selected_runs()
        if not self._runs:
            self.run_detail.setText("No recordings." if self.context.service else "")
        elif not runs:
            total = sum(run.observations for run in self._runs)
            self.run_detail.setText(f"All {count(len(self._runs), 'run')}, {total:,} samples.")
        else:
            seconds = sum(run.duration_seconds or 0 for run in runs)
            samples = sum(run.observations for run in runs)
            failed = sum(run.failed for run in runs)
            text = f"{count(len(runs), 'run')} selected: {_length(seconds)}, {samples:,} samples"
            if failed:
                text += f", {failed:,} failed reads"
            self.run_detail.setText(text + ".")
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.context.service is not None
        live = self.context.live_job_running
        blocked = "Open a project first." if not has else "Wait for the live job to finish." if live else ""
        runs = self.selected_runs()
        set_enabled(self.recover_button, has and not live and any(run.state == NEEDS_RECOVERY for run in runs),
                    blocked or "Select an interrupted run.")
        set_enabled(self.trash_button, has and not live and bool(runs), blocked or "Select runs first.")
        self.all_button.setVisible(bool(runs))
        set_enabled(self.export_button, has and self.signals.count() > 0, blocked or "Nothing recorded yet.")

    def _filter_signals(self, text: str) -> None:
        needle = text.lower()
        for index in range(self.signals.count()):
            item = self.signals.item(index)
            item.setHidden(bool(needle) and needle not in f"{item.data(PREFIX) or ''} {item.text()}".lower())

    def select_signal(self, signal_ref: str) -> None:
        for index in range(self.signals.count()):
            item = self.signals.item(index)
            if item.data(KEY) == signal_ref:
                self.signals.setCurrentItem(item)
                return
        raise KeyError(signal_ref)

    # Plot -----------------------------------------------------------------

    def plot_current(self) -> None:
        """Show the selected signal; samples load once per signal in the background."""

        item = self.signals.currentItem()
        if item is None or self.context.service is None:
            return
        signal_ref = item.data(KEY)
        if signal_ref in self._series:
            self._shown = signal_ref
            self._redraw(reset_view=True)
            return
        self.plot_loading.start(f"Loading {item.text().rsplit('  (', 1)[0]}…")

        def done(rows: list[dict]) -> None:
            self.plot_loading.stop()
            self._series[signal_ref] = rows
            current = self.signals.currentItem()
            if current is not None and current.data(KEY) == signal_ref:
                self._shown = signal_ref
                self._redraw(reset_view=True)

        def failed(detail: str) -> None:
            self.plot_loading.stop()
            self.context.error("Cannot plot", detail.split("\n\n", 1)[0])

        self.context.run("recordings.series", lambda service: service.series(signal_ref), done, failed)

    def _redraw(self, *, reset_view: bool) -> None:
        rows = self._series.get(self._shown) if self._shown else None
        if rows is None:
            return
        runs = self.selected_runs() or self._runs
        data = series_data(rows, runs)
        self.plot.set_data(data["x"], data["y"], data["spans"], reset_view=reset_view)
        name, unit = self._names.get(self._shown, (self._shown, ""))
        self.plot.set_measurement(name, unit)
        finite = data["finite"]
        where = count(data["runs"], "run")
        if not data["samples"]:
            self.plot_summary.setText("No samples of this signal in the selected runs.")
        elif finite:
            minimum, maximum, last = finite
            self.plot_summary.setText(
                f"{data['samples']:,} samples in {where}. "
                + (f"Constant {last:g}." if minimum == maximum else f"Min {minimum:g}, max {maximum:g}, last {last:g}.")
            )
        else:
            self.plot_summary.setText(f"{data['samples']:,} samples in {where}; no decoded values.")

    # Actions ----------------------------------------------------------------

    def recover(self) -> None:
        run_ids = [run.run_id for run in self.selected_runs() if run.state == NEEDS_RECOVERY]
        self._each(run_ids, "recover", "Not recovered", lambda n: f"Recovered {count(n, 'run')}.")

    def trash(self) -> None:
        run_ids = [run.run_id for run in self.selected_runs()]
        self._each(run_ids, "trash", "Not deleted", lambda n: f"Moved {count(n, 'run')} to the system trash.")

    def _each(self, run_ids: list[str], method: str, title: str, message) -> None:
        if self.context.service is None or not run_ids:
            return

        def work(service):
            if method == "trash":
                service.trash_runs(run_ids)
            else:
                for run_id in run_ids:
                    service.recover(run_id)
            return len(run_ids)

        def done(number: int) -> None:
            self.runs_loading.stop()
            self.context.message(message(number))
            self.context.changed()

        def failed(detail: str) -> None:
            self.runs_loading.stop()
            self._update_buttons()
            self.context.error(title, detail.split("\n\n", 1)[0])
            self.context.changed()

        for button in (self.recover_button, self.trash_button):
            button.setEnabled(False)
        self.runs_loading.start("Recovering…" if method == "recover" else "Deleting…")
        self.context.run(f"recordings.{method}", work, done, failed)

    def export(self) -> None:
        service = self.context.service
        if service is None:
            return
        item = self.signals.currentItem()
        selected = self.selected_runs()
        dialog = ExportDialog(
            self._names.get(item.data(KEY), ("",))[0] if item is not None else None,
            self.signals.count(), len(selected), len(self._runs), self,
        )
        if not self.run_dialog(dialog):
            return
        choice = dialog.choice()
        refs = [item.data(KEY)] if choice["signals"] == "selected" and item is not None else [
            self.signals.item(index).data(KEY) for index in range(self.signals.count())
        ]
        run_ids = [run.run_id for run in selected] if choice["runs"] == "selected" else []
        path = self.choose_export_path()
        if not path:
            return

        def work(service):
            with Path(path).open("w", encoding="utf-8", newline="") as stream:
                return service.export(refs, stream, run_ids=run_ids)

        def done(number: int) -> None:
            self.export_button.setEnabled(True)
            self.context.message(f"Exported {number:,} samples to {Path(path).name}.")

        def failed(detail: str) -> None:
            self.export_button.setEnabled(True)
            self.context.error("Export failed", detail.split("\n\n", 1)[0])

        self.export_button.setEnabled(False)
        self.context.run("recordings.export", work, done, failed)


def signal_entries(
    summary: dict, names: dict[str, tuple[str, str]], places: dict[str, tuple[str, str]],
) -> list[tuple[str, str, str, str]]:
    """(signal_ref, prefix, text, tooltip) per recorded signal, by name then ECU.

    The ECU prefix appears only where several recorded signals share a name,
    with the DID added where they also share the ECU; every tooltip names both.
    """

    def name(ref: str) -> str:
        return names.get(ref, (ref, ""))[0]

    def place(ref: str) -> tuple[str, str]:
        return places.get(ref, ("Unknown ECU", ref))

    by_name = Counter(name(ref).lower() for ref in summary)
    by_ecu = Counter((name(ref).lower(), place(ref)[0]) for ref in summary)
    entries = []
    for ref in sorted(summary, key=lambda ref: (name(ref).lower(), place(ref)[0].lower(), ref)):
        ecu, did = place(ref)
        prefix = ""
        if by_name[name(ref).lower()] > 1:
            prefix = f"{ecu} {did}" if by_ecu[(name(ref).lower(), ecu)] > 1 else ecu
        entries.append((ref, prefix, f"{name(ref)}  ({summary[ref]['observations']:,})", f"{ecu}\nDID {did}"))
    return entries


def series_data(rows: list[dict], runs: list[RunSummary]) -> dict:
    """Lay the chosen runs end to end on their own clocks, with a gap so lines never join."""

    chosen = {run.run_id: run for run in runs}
    xs: list[float] = []
    ys: list[float] = []
    spans: list[RunSpan] = []
    offset = 0.0
    origin = 0.0
    current: RunSummary | None = None
    last = 0.0
    samples = 0
    for row in rows:
        run = chosen.get(row["run_id"])
        if run is None:
            continue
        run_start = _utc(run.created_utc)
        when = _utc(row.get("response_utc"))
        if when is None:
            # Without wall-clock times, a run is placed on its own elapsed clock.
            when = (run_start or 0.0) + row["response_elapsed_ms"] / 1000
        if run is not current:
            if current is not None:
                spans.append(RunSpan(current.run_id, offset, last, _span_label(current)))
                offset = last + 1.0
                xs.append(offset)
                ys.append(math.nan)
            current = run
            origin = run_start if run_start is not None else when
        last = offset + max(0.0, when - origin)
        xs.append(last)
        ys.append(math.nan if row["decoded_value"] is None else float(row["decoded_value"]))
        samples += 1
    if current is not None:
        spans.append(RunSpan(current.run_id, offset, last, _span_label(current)))
    finite = [value for value in ys if math.isfinite(value)]
    return {
        "x": xs, "y": ys, "spans": spans, "samples": samples, "runs": len(spans),
        "finite": (min(finite), max(finite), finite[-1]) if finite else None,
    }


def _utc(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _span_label(run: RunSummary) -> str:
    return f"the run of {_local(run)}" if run.created_utc else "a run without a start time"


def _local(run: RunSummary) -> str:
    start = _utc(run.created_utc)
    return datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S") if start is not None else "Unknown time"


def _length(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds // 60:.0f} min"
    return f"{seconds // 3600:.0f} h {seconds % 3600 // 60:.0f} min"
