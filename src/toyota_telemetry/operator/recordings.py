"""Drives and runs: listing, recovery, deletion, and bounded observation export.

A run is one capture folder. Imported recordings arrive as many short runs,
so people work with drives instead: runs grouped wherever recording paused
for more than ``DRIVE_GAP_SECONDS``. A drive is named by its start minute,
which is unique because drives are at least that gap apart.

The scanner tolerates damaged or interrupted runs so they can be listed and
recovered; the strict capture reader still decides what the cache ingests.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO

from .. import system_trash
from ..capture_writer import recover_incomplete_segment
from ..identity import SignalRef, canonical_run_id
from ..repository import ProjectRepository
from .inventory import refresh_cache
from .live import LiveBusy, LiveLock


COMPLETE = "complete"
NEEDS_RECOVERY = "needs recovery"
UNREADABLE = "unreadable"
END_REASONS = {
    "plan_complete": "targets complete",
    "operator_stop": "stopped",
    "transport_lost": "connection lost",
    "size_limit": "storage limit reached",
    "recovered_prefix": "recovered after interruption",
}
DRIVE_GAP_SECONDS = 60
CSV_COLUMNS = (
    "signal_ref", "run_id", "sequence", "response_utc", "elapsed_ms",
    "raw_hex", "decoded_value", "condition",
)


@dataclass
class RunSummary:
    run_id: str
    state: str
    origin: str | None = None
    created_utc: str | None = None
    endpoint_id: str | None = None
    note: str | None = None
    end_reason: str | None = None
    segments: int = 0
    exchanges: int = 0
    positive: int = 0
    failed: int = 0
    observations: int = 0
    duration_seconds: float | None = None
    detail: str = ""
    segment_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def end_label(self) -> str | None:
        return END_REASONS.get(self.end_reason, self.end_reason) if self.end_reason else None

    def as_dict(self, *, segments: bool = False) -> dict[str, Any]:
        value = {
            "run_id": self.run_id,
            "state": self.state,
            "origin": self.origin,
            "created_utc": self.created_utc,
            "endpoint_id": self.endpoint_id,
            "note": self.note,
            "end_reason": self.end_reason,
            "segments": self.segments,
            "exchanges": self.exchanges,
            "positive": self.positive,
            "failed": self.failed,
            "observations": self.observations,
            "duration_seconds": self.duration_seconds,
            "detail": self.detail,
        }
        if segments:
            value["segment_rows"] = list(self.segment_rows)
        return value


@dataclass
class Drive:
    """Runs recorded without a pause longer than ``DRIVE_GAP_SECONDS``."""

    key: str
    runs: list[RunSummary]
    start: datetime | None = None
    end: datetime | None = None

    @property
    def duration_seconds(self) -> float | None:
        return (self.end - self.start).total_seconds() if self.start and self.end else None

    @property
    def run_ids(self) -> list[str]:
        return [run.run_id for run in self.runs]

    @property
    def interrupted(self) -> list[str]:
        return [run.run_id for run in self.runs if run.state == NEEDS_RECOVERY]

    @property
    def endpoint_ids(self) -> list[str]:
        return sorted({run.endpoint_id for run in self.runs if run.endpoint_id})

    @property
    def notes(self) -> list[str]:
        return list(dict.fromkeys(run.note for run in self.runs if run.note))

    def total(self, name: str) -> int:
        return sum(getattr(run, name) for run in self.runs)

    def as_dict(self, *, runs: bool = False) -> dict[str, Any]:
        value = {
            "drive": self.key,
            "start_utc": _iso(self.start),
            "end_utc": _iso(self.end),
            "duration_seconds": self.duration_seconds,
            "runs": len(self.runs),
            "reads": self.total("exchanges"),
            "failed": self.total("failed"),
            "samples": self.total("observations"),
            "endpoint_ids": self.endpoint_ids,
            "notes": self.notes,
            "interrupted_runs": self.interrupted,
        }
        if runs:
            value["run_list"] = [run.as_dict() for run in self.runs]
        return value


def group_drives(runs: Iterable[RunSummary], gap_seconds: float = DRIVE_GAP_SECONDS) -> list[Drive]:
    """Group runs into drives, oldest first; a run without a start time is its own drive."""

    drives: list[Drive] = []
    timed = []
    for run in runs:
        start = _parse_utc(run.created_utc)
        if start is None:
            drives.append(Drive(run.run_id, [run]))
        else:
            timed.append((start, start + timedelta(seconds=run.duration_seconds or 0), run))
    current: Drive | None = None
    for start, end, run in sorted(timed, key=lambda row: (row[0], row[2].run_id)):
        if current is None or (start - current.end).total_seconds() > gap_seconds:
            current = Drive(start.strftime("%Y-%m-%dT%H:%M"), [], start, end)
            drives.append(current)
        current.runs.append(run)
        current.end = max(current.end, end)
    return sorted(drives, key=lambda drive: (drive.start is None, drive.start or datetime.min, drive.key))


def list_drives(repository: ProjectRepository) -> list[Drive]:
    return group_drives(list_runs(repository))


def find_drive(drives: Iterable[Drive], key: str) -> Drive:
    """A drive by its start minute ("2026-09-01T08:15" or "2026-09-01 08:15") or by one of its runs."""

    wanted = key.strip().replace(" ", "T")
    drives = list(drives)
    for drive in drives:
        if drive.key == wanted:
            return drive
    try:
        run_id = canonical_run_id(key)
    except ValueError:
        run_id = None
    for drive in drives:
        if run_id in drive.run_ids:
            return drive
    raise ValueError(f"no drive {key}; drives are named by their start, e.g. 2026-09-01T08:15")


def trash_drive(repository: ProjectRepository, key: str) -> Drive:
    """Move a drive's run folders to the system trash; restoring them there brings them back."""

    drive = find_drive(list_drives(repository), key)
    try:
        with LiveLock(repository):
            system_trash.send_to_trash([repository.paths.captures / run_id for run_id in drive.run_ids])
    except LiveBusy as exc:
        raise ValueError("a live job is running; delete after it stops") from exc
    except system_trash.TrashError as exc:
        raise ValueError(f"not moved to the trash: {exc}") from exc
    return drive


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


_FINISHED: dict[str, tuple[tuple[int, int], RunSummary]] = {}


def list_runs(repository: ProjectRepository) -> list[RunSummary]:
    root = repository.paths.captures
    if not root.is_dir():
        return []
    runs = [_cached_summary(directory) for directory in sorted(path for path in root.iterdir() if path.is_dir())]
    return sorted(runs, key=lambda run: (run.created_utc or "", run.run_id))


def interrupted_run_ids(repository: ProjectRepository) -> list[str]:
    """Runs without run-end metadata, found without reading their segments."""

    root = repository.paths.captures
    if not root.is_dir():
        return []
    return sorted(
        directory.name for directory in root.iterdir()
        if directory.is_dir() and not (directory / "run-end.json").is_file()
    )


def _cached_summary(directory: Path) -> RunSummary:
    """Finished runs are immutable, so their summaries are reused by run-end identity."""

    end = directory / "run-end.json"
    try:
        stat = end.stat()
    except OSError:
        return _summarize(directory)
    key = (stat.st_size, stat.st_mtime_ns)
    cached = _FINISHED.get(str(directory))
    if cached is not None and cached[0] == key:
        return cached[1]
    summary = _summarize(directory)
    _FINISHED[str(directory)] = (key, summary)
    return summary


def run_detail(repository: ProjectRepository, run_id: str) -> RunSummary:
    directory = _run_directory(repository, run_id)
    return _summarize(directory)


def recover_run(repository: ProjectRepository, run_id: str) -> Path:
    """Close an interrupted run at its valid prefix; never opens a connector."""

    directory = _run_directory(repository, run_id)
    if (directory / "run-end.json").is_file():
        raise ValueError(f"run {run_id} is already complete")
    try:
        with LiveLock(repository):
            for partial in sorted(directory.glob("*.segment.partial.jsonl")):
                os.replace(partial, partial.with_name(partial.name.replace(".partial.", ".incomplete.")))
            incomplete = sorted(directory.glob("*.segment.incomplete.jsonl"))
            if not incomplete:
                raise ValueError(f"run {run_id} has no interrupted segment to recover")
            return recover_incomplete_segment(incomplete[-1])
    except LiveBusy as exc:
        raise ValueError("a live job is running; recover after it stops") from exc


def export_observations(
    repository: ProjectRepository,
    inventory: Mapping[str, Any],
    signal_refs: Iterable[str],
    stream: TextIO,
    *,
    run_ids: Iterable[str] = (),
) -> int:
    refresh_cache(repository, inventory)
    wanted_runs = {canonical_run_id(value) for value in run_ids}
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    count = 0
    for signal_ref in sorted({str(SignalRef.parse(ref)) for ref in signal_refs}, key=SignalRef.parse):
        for row in repository.signal_observations(signal_ref):
            if wanted_runs and row["run_id"] not in wanted_runs:
                continue
            writer.writerow((
                signal_ref, row["run_id"], row["sequence"], row["response_utc"] or "",
                row["response_elapsed_ms"], row["raw_hex"],
                "" if row["decoded_value"] is None else row["decoded_value"],
                row["recorded_applicability"],
            ))
            count += 1
    return count


def signal_series(
    repository: ProjectRepository, inventory: Mapping[str, Any], signal_ref: str
) -> list[dict[str, Any]]:
    refresh_cache(repository, inventory)
    return repository.signal_observations(signal_ref)


def _run_directory(repository: ProjectRepository, run_id: str) -> Path:
    canonical = canonical_run_id(run_id)
    directory = repository.paths.captures / canonical
    if not directory.is_dir():
        raise ValueError(f"no recorded run {canonical}")
    return directory


def _summarize(directory: Path) -> RunSummary:
    try:
        run_id = canonical_run_id(directory.name)
    except ValueError:
        return RunSummary(directory.name, UNREADABLE, detail="directory name is not a run id")
    summary = RunSummary(run_id, COMPLETE)
    try:
        start = json.loads((directory / "run-start.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summary.state = UNREADABLE
        summary.detail = f"run-start is unreadable: {exc}"
        return summary
    summary.origin = start.get("origin")
    summary.created_utc = start.get("created_utc")
    summary.endpoint_id = start.get("endpoint_id")
    summary.note = start.get("run_note")
    first_ms: int | None = None
    last_ms: int | None = None
    # One file per segment: a recovered complete copy supersedes the
    # interrupted source it was made from, which stays on disk untouched.
    rank = {"complete": 0, "incomplete": 1, "partial": 2}
    chosen: dict[str, Path] = {}
    for path in sorted(directory.glob("*.segment.*.jsonl")):
        index, state = path.name.split(".")[0], path.name.split(".")[2]
        current = chosen.get(index)
        if current is None or rank.get(state, 3) < rank.get(current.name.split(".")[2], 3):
            chosen[index] = path
    for index in sorted(chosen):
        path = chosen[index]
        state = path.name.split(".")[2]
        counts = {"exchanges": 0, "positive": 0, "failed": 0, "observations": 0}
        try:
            lines = path.read_bytes().splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                break
            if not isinstance(record, dict) or record.get("record") != "exchange":
                continue
            counts["exchanges"] += 1
            if record.get("outcome") == "positive":
                counts["positive"] += 1
            else:
                counts["failed"] += 1
            counts["observations"] += len(record.get("observations") or [])
            elapsed = record.get("response_elapsed_ms")
            if isinstance(elapsed, int):
                first_ms = elapsed if first_ms is None else min(first_ms, elapsed)
                last_ms = elapsed if last_ms is None else max(last_ms, elapsed)
        summary.segment_rows.append({"segment": index, "state": state, **counts})
        summary.segments += 1
        for name in ("exchanges", "positive", "failed", "observations"):
            setattr(summary, name, getattr(summary, name) + counts[name])
    if first_ms is not None and last_ms is not None:
        summary.duration_seconds = (last_ms - first_ms) / 1000
    end_path = directory / "run-end.json"
    if end_path.is_file():
        try:
            summary.end_reason = json.loads(end_path.read_text(encoding="utf-8")).get("reason")
        except (OSError, json.JSONDecodeError) as exc:
            summary.state = UNREADABLE
            summary.detail = f"run-end is unreadable: {exc}"
    else:
        summary.state = NEEDS_RECOVERY
        summary.detail = "interrupted: recover it to keep the reads saved before the interruption, or move it to the trash"
    return summary
