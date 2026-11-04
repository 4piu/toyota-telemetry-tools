"""Recordings: drives, their plots, recovery, trash, and sample export.

People work with drives (runs grouped wherever recording paused), named by
their local start time. Run and endpoint IDs stay out of sight; the CLI uses
them.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.operator.recordings import Drive

from .context import PageContext
from .dialogs import ExportDialog
from .names import count, endpoint_label
from .widgets import Disclosure, button_row, page_title, search_icon, set_enabled
from .widgets.metric_plot import MetricPlot, RunSpan


DRIVE_COLUMNS = ("Started", "Length", "ECUs", "Samples", "Status", "Note")
KEY = Qt.ItemDataRole.UserRole
SORT = Qt.ItemDataRole.UserRole + 1


class SortItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine, theirs = self.data(SORT), other.data(SORT)
        if mine is not None and theirs is not None:
            return mine < theirs
        return super().__lt__(other)


class RecordingsPage(QWidget):
    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self.choose_export_path = lambda: QFileDialog.getSaveFileName(
            self, "Export samples", "samples.csv", "CSV files (*.csv)"
        )[0]
        self.run_dialog = lambda dialog: dialog.exec() == QDialog.DialogCode.Accepted
        self.confirm = lambda title, text: QMessageBox.question(
            self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) == QMessageBox.StandardButton.Yes
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            "Recordings",
            "A pause over a minute starts a new drive",
        ))

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._drives_panel())
        splitter.addWidget(self._plot_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 400])
        layout.addWidget(splitter, 1)

        self.export_button = QPushButton("Export")
        self.export_button.clicked.connect(self.export)
        self.context.register("recordings.export", self.export_button, "Export recorded samples")
        layout.addLayout(button_row(self.export_button))

        self._drives: list[Drive] = []
        self._trash: list[Drive] = []
        self._labels: dict[str, str] = {}
        self._names: dict[str, tuple[str, str]] = {}
        self._series: dict[str, list[dict]] = {}
        self._all_signals: dict[str, dict] = {}
        self._shown: str | None = None
        self._stale = True
        context.project_changed.connect(self._project_changed)
        context.data_changed.connect(self._mark_stale)
        context.busy_changed.connect(lambda _busy: self._update_buttons())
        self._update_buttons()

    # Layout -----------------------------------------------------------------

    def _drives_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.drives = self._table(DRIVE_COLUMNS, "Recorded drives")
        self.drives.setMinimumHeight(180)
        self.drives.itemSelectionChanged.connect(self._selection_changed)
        self.context.register("recordings.list", self.drives, "Recorded drives")
        layout.addWidget(self.drives, 1)
        self.drive_detail = QLabel()
        self.drive_detail.setWordWrap(True)
        self.context.register("recordings.show", self.drive_detail, "The selected drives")
        self.recover_button = QPushButton("Recover")
        self.trash_button = QPushButton("Delete")
        self.context.register("recordings.recover", self.recover_button, "Keeps what was saved")
        self.context.register("recordings.trash", self.trash_button, "Moves to the trash")
        self.recover_button.clicked.connect(self.recover)
        self.trash_button.clicked.connect(self.trash)
        actions = button_row(self.recover_button, self.trash_button)
        actions.insertWidget(0, self.drive_detail, 1)
        layout.addLayout(actions)

        trash = QWidget()
        trash_layout = QVBoxLayout(trash)
        trash_layout.setContentsMargins(16, 0, 0, 0)
        self.trash_table = self._table(("Started", "Length", "ECUs", "Samples"), "Drives in the trash")
        self.trash_table.setMaximumHeight(150)
        self.trash_table.itemSelectionChanged.connect(self._update_buttons)
        self.restore_button = QPushButton("Restore")
        self.empty_button = QPushButton("Empty trash")
        self.context.register("recordings.restore", self.restore_button, "Back to the recordings")
        self.context.register("recordings.empty-trash", self.empty_button, "Delete permanently")
        self.restore_button.clicked.connect(self.restore)
        self.empty_button.clicked.connect(self.empty_trash)
        trash_layout.addWidget(self.trash_table)
        trash_layout.addLayout(button_row(self.restore_button, self.empty_button))
        self.trash_toggle = Disclosure("Trash", trash)
        layout.addWidget(self.trash_toggle)
        layout.addWidget(trash)
        return panel

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
        self.signals.currentItemChanged.connect(lambda *_: self.plot_current())
        left.addWidget(self.signal_search)
        left.addWidget(self.signals, 1)
        layout.addLayout(left, 1)
        right = QVBoxLayout()
        self.plot = MetricPlot()
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
        for column, width in enumerate((150, 80, 220, 90, 110)[: len(columns) - 1]):
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
        """Load drives, the trash, and recorded signals in the background."""

        self._stale = False
        if self.context.service is None:
            self._show_listing(None)
            return

        def work(service):
            tree = service.tree()
            return {
                "drives": service.drives(),
                "trash": service.trashed(),
                "summary": service.observation_summary(),
                "names": {signal.signal_ref: (signal.name, signal.unit) for _, _, signal in tree.signals()},
                "labels": {
                    endpoint.endpoint_id: endpoint_label(endpoint).split("  ")[0]
                    for endpoint in tree.endpoints if endpoint.endpoint_id
                },
            }

        def failed(detail: str) -> None:
            self.drive_detail.setText(f"Cannot read recordings: {detail.split(chr(10) * 2, 1)[0]}")

        if not self._drives:
            self.drive_detail.setText("Loading…")
        self.context.run("recordings.list", work, self._show_listing, failed)

    def _show_listing(self, listing: dict | None) -> None:
        selected = set(self._selected_keys(self.drives))
        current = self.signals.currentItem()
        current_signal = current.data(KEY) if current is not None else None
        self._drives = listing["drives"] if listing else []
        self._trash = listing["trash"] if listing else []
        self._labels = listing["labels"] if listing else {}
        self._names = listing["names"] if listing else {}
        self._fill(self.drives, self._drives, full=True)
        self._fill(self.trash_table, self._trash, full=False)
        self.trash_toggle.setText(f"Trash ({len(self._trash)})" if self._trash else "Trash")
        self.drives.blockSignals(True)
        self.select_drives(list(selected))
        self.drives.blockSignals(False)
        self._all_signals = listing["summary"] if listing else {}
        self._show_selection()
        self._load_signals(current_signal)

    def _load_signals(self, keep: str | None = None) -> None:
        """List the signals recorded in the selected drives (all drives when none is selected)."""

        drives = self.selected_drives()
        if not drives or self.context.service is None:
            self._show_signals(self._all_signals, keep)
            return
        run_ids = [run_id for drive in drives for run_id in drive.run_ids]
        keys = [drive.key for drive in drives]

        def done(summary: dict) -> None:
            if [drive.key for drive in self.selected_drives()] == keys:
                self._show_signals(summary, keep)

        self.context.run(
            "recordings.signals", lambda service: service.observation_summary(run_ids), done,
        )

    def _show_signals(self, summary: dict, keep: str | None) -> None:
        self.signals.blockSignals(True)
        self.signals.clear()
        for signal_ref in sorted(summary, key=lambda ref: self._names.get(ref, (ref, ""))[0].lower()):
            name = self._names.get(signal_ref, (signal_ref, ""))[0]
            item = QListWidgetItem(f"{name}  ({summary[signal_ref]['observations']:,})")
            item.setData(KEY, signal_ref)
            self.signals.addItem(item)
        self.signals.blockSignals(False)
        self._filter_signals(self.signal_search.text())
        if keep is not None:
            try:
                self.select_signal(keep)
            except KeyError:
                pass
        self._update_buttons()

    def _fill(self, table: QTableWidget, drives: list[Drive], *, full: bool) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for drive in drives:
            row = table.rowCount()
            table.insertRow(row)
            ecus = ", ".join(self._labels.get(endpoint, "Unknown ECU") for endpoint in drive.endpoint_ids)
            values = [
                (_local(drive), drive.start.timestamp() if drive.start else 0.0),
                (_length(drive.duration_seconds), drive.duration_seconds or 0.0),
                (ecus or "-", None),
                (f"{drive.total('observations'):,}", drive.total("observations")),
            ]
            if full:
                values += [
                    ("! Interrupted" if drive.interrupted else "", int(bool(drive.interrupted))),
                    ("; ".join(drive.notes), None),
                ]
            for column, (text, sort) in enumerate(values):
                item = SortItem(text)
                item.setData(KEY, drive.key)
                item.setData(SORT, sort)
                if column == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, column, item)
            if full and drive.interrupted:
                table.item(row, 4).setToolTip("Stopped without finishing")
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.DescendingOrder)

    # Selection ----------------------------------------------------------------

    @staticmethod
    def _selected_keys(table: QTableWidget) -> list[str]:
        return list(dict.fromkeys(index.data(KEY) for index in table.selectionModel().selectedRows(0)))

    def selected_drives(self) -> list[Drive]:
        keys = set(self._selected_keys(self.drives))
        return [drive for drive in self._drives if drive.key in keys]

    def select_drives(self, keys: list[str]) -> None:
        """Select drives by key (automation and tests)."""

        model = self.drives.selectionModel()
        model.clearSelection()
        for row in range(self.drives.rowCount()):
            if self.drives.item(row, 0).data(KEY) in keys:
                model.select(self.drives.model().index(row, 0), model.SelectionFlag.Select | model.SelectionFlag.Rows)

    def _selection_changed(self) -> None:
        self._show_selection()
        current = self.signals.currentItem()
        self._load_signals(current.data(KEY) if current is not None else None)
        self._redraw(reset_view=True)

    def _show_selection(self) -> None:
        drives = self.selected_drives()
        if not self._drives:
            self.drive_detail.setText("No recordings." if self.context.service else "")
        elif not drives:
            total = sum(drive.total("observations") for drive in self._drives)
            self.drive_detail.setText(f"{count(len(self._drives), 'drive')}, {total:,} samples. Plot and export use all.")
        else:
            seconds = sum(drive.duration_seconds or 0 for drive in drives)
            samples = sum(drive.total("observations") for drive in drives)
            failed = sum(drive.total("failed") for drive in drives)
            text = f"{count(len(drives), 'drive')} selected: {_length(seconds)}, {samples:,} samples"
            if failed:
                text += f", {failed:,} failed reads"
            self.drive_detail.setText(text + ".")
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.context.service is not None
        live = self.context.live_job_running
        blocked = "Open a project first." if not has else "Wait for the live job to finish." if live else ""
        drives = self.selected_drives()
        set_enabled(self.recover_button, has and not live and any(drive.interrupted for drive in drives),
                    blocked or "Select an interrupted drive.")
        set_enabled(self.trash_button, has and not live and bool(drives), blocked or "Select drives first.")
        set_enabled(self.restore_button, has and not live and bool(self._selected_keys(self.trash_table)),
                    blocked or "Select drives in the trash.")
        set_enabled(self.empty_button, has and not live and bool(self._trash), blocked or "The trash is empty.")
        set_enabled(self.export_button, has and self.signals.count() > 0, blocked or "Nothing recorded yet.")

    def _filter_signals(self, text: str) -> None:
        needle = text.lower()
        for index in range(self.signals.count()):
            item = self.signals.item(index)
            item.setHidden(bool(needle) and needle not in item.text().lower())

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
        self.plot_summary.setText("Loading…")

        def done(rows: list[dict]) -> None:
            self._series[signal_ref] = rows
            current = self.signals.currentItem()
            if current is not None and current.data(KEY) == signal_ref:
                self._shown = signal_ref
                self._redraw(reset_view=True)

        self.context.run("recordings.series", lambda service: service.series(signal_ref), done)

    def _redraw(self, *, reset_view: bool) -> None:
        rows = self._series.get(self._shown) if self._shown else None
        if rows is None:
            return
        drives = self.selected_drives() or self._drives
        data = series_data(rows, drives)
        self.plot.set_data(data["x"], data["y"], data["spans"], reset_view=reset_view)
        name, unit = self._names.get(self._shown, (self._shown, ""))
        self.plot.set_measurement(name, unit)
        finite = data["finite"]
        where = count(data["drives"], "drive")
        if not data["samples"]:
            self.plot_summary.setText("No samples of this signal in the selected drives.")
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
        keys = [drive.key for drive in self.selected_drives() if drive.interrupted]
        self._each(keys, "recover_drive", "Not recovered", lambda n: f"Recovered {count(n, 'drive')}.")

    def trash(self) -> None:
        keys = [drive.key for drive in self.selected_drives()]
        self._each(keys, "trash", "Not moved to the trash",
                   lambda n: f"Moved {count(n, 'drive')} to the trash. Restore brings them back.")

    def restore(self) -> None:
        keys = self._selected_keys(self.trash_table)
        self._each(keys, "restore", "Not restored", lambda n: f"Restored {count(n, 'drive')}.")

    def empty_trash(self) -> None:
        if not self._trash or not self.confirm(
            "Empty trash", f"Permanently delete {count(len(self._trash), 'drive')}? This cannot be undone.",
        ):
            return
        self._each([None], "empty_trash", "Trash not emptied", lambda _n: "Emptied the trash.")

    def _each(self, keys: list, method: str, title: str, message) -> None:
        if self.context.service is None or not keys:
            return

        def work(service):
            for key in keys:
                getattr(service, method)(*([] if key is None else [key]))
            return len(keys)

        def done(number: int) -> None:
            self.context.message(message(number))
            self.context.changed()

        def failed(detail: str) -> None:
            self._update_buttons()
            self.context.error(title, detail.split("\n\n", 1)[0])
            self.context.changed()

        for button in (self.recover_button, self.trash_button, self.restore_button, self.empty_button):
            button.setEnabled(False)
        self.context.run(f"recordings.{method}", work, done, failed)

    def export(self) -> None:
        service = self.context.service
        if service is None:
            return
        item = self.signals.currentItem()
        selected = self.selected_drives()
        dialog = ExportDialog(
            self._names.get(item.data(KEY), ("",))[0] if item is not None else None,
            self.signals.count(), len(selected), len(self._drives), self,
        )
        if not self.run_dialog(dialog):
            return
        choice = dialog.choice()
        refs = [item.data(KEY)] if choice["signals"] == "selected" and item is not None else [
            self.signals.item(index).data(KEY) for index in range(self.signals.count())
        ]
        run_ids = [run_id for drive in selected for run_id in drive.run_ids] if choice["drives"] == "selected" else []
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


def series_data(rows: list[dict], drives: list[Drive]) -> dict:
    """Lay the chosen drives end to end on their own clocks, with a gap so lines never join."""

    starts = {run.run_id: (drive, _utc(run.created_utc)) for drive in drives for run in drive.runs}
    xs: list[float] = []
    ys: list[float] = []
    spans: list[RunSpan] = []
    offset = 0.0
    origin = 0.0
    current: Drive | None = None
    last = 0.0
    samples = 0
    for row in rows:
        found = starts.get(row["run_id"])
        if found is None:
            continue
        drive, run_start = found
        when = _utc(row.get("response_utc"))
        if when is None:
            # Without wall-clock times, a recording runs on its own elapsed clock.
            when = (run_start or 0.0) + row["response_elapsed_ms"] / 1000
        if drive is not current:
            if current is not None:
                spans.append(RunSpan(current.key, offset, last, _span_label(current)))
                offset = last + 1.0
                xs.append(offset)
                ys.append(math.nan)
            current = drive
            origin = drive.start.timestamp() if drive.start else when
        last = offset + max(0.0, when - origin)
        xs.append(last)
        ys.append(math.nan if row["decoded_value"] is None else float(row["decoded_value"]))
        samples += 1
    if current is not None:
        spans.append(RunSpan(current.key, offset, last, _span_label(current)))
    finite = [value for value in ys if math.isfinite(value)]
    return {
        "x": xs, "y": ys, "spans": spans, "samples": samples, "drives": len(spans),
        "finite": (min(finite), max(finite), finite[-1]) if finite else None,
    }


def _utc(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _span_label(drive: Drive) -> str:
    return f"the drive of {_local(drive)}" if drive.start else "a recording without a start time"


def _local(drive: Drive) -> str:
    return drive.start.astimezone().strftime("%Y-%m-%d %H:%M") if drive.start else "Unknown time"


def _length(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds // 60:.0f} min"
    return f"{seconds // 3600:.0f} h {seconds % 3600 // 60:.0f} min"
