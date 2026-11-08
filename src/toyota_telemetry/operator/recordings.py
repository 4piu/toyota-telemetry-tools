"""Runs: listing, recovery, deletion, and bounded observation export.

A run is one capture folder. The scanner tolerates damaged or interrupted runs so they can be listed and
recovered; the strict capture reader still decides what the cache ingests.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO

from .. import system_trash
from ..capture_writer import recover_incomplete_segment
from ..i18n import _, N_
from ..identity import SignalRef, canonical_run_id
from ..repository import ProjectRepository
from ..states import raw_number, state_label
from .inventory import refresh_cache, signal_states
from .live import LiveBusy, LiveLock


# Run states: compared in code; marked so the interface can translate them.
COMPLETE = N_("complete")
NEEDS_RECOVERY = N_("needs recovery")
UNREADABLE = N_("unreadable")
END_REASONS = {
    "plan_complete": N_("targets complete"),
    "slice_end": N_("handed over to the next ECU"),
    "operator_stop": N_("stopped"),
    "transport_lost": N_("connection lost"),
    "size_limit": N_("storage limit reached"),
    "recovered_prefix": N_("recovered after interruption"),
}
CSV_COLUMNS = (
    "signal_ref", "run_id", "sequence", "response_utc", "elapsed_ms",
    "raw_hex", "decoded_value", "state", "condition",
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
        if not self.end_reason:
            return None
        return _(END_REASONS[self.end_reason]) if self.end_reason in END_REASONS else self.end_reason

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


def trash_runs(repository: ProjectRepository, run_ids: Iterable[str]) -> list[RunSummary]:
    """Move run folders to the system trash in one step; restoring them there brings them back."""

    runs = [run_detail(repository, run_id) for run_id in run_ids]
    try:
        with LiveLock(repository):
            system_trash.send_to_trash([_run_directory(repository, run.run_id) for run in runs])
    except LiveBusy as exc:
        raise ValueError(_("a live job is running; delete after it stops")) from exc
    except system_trash.TrashError as exc:
        raise ValueError(_("not moved to the trash: {error}").format(error=exc)) from exc
    return runs


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
        raise ValueError(_("run {run} is already complete").format(run=run_id))
    try:
        with LiveLock(repository):
            for partial in sorted(directory.glob("*.segment.partial.jsonl")):
                os.replace(partial, partial.with_name(partial.name.replace(".partial.", ".incomplete.")))
            incomplete = sorted(directory.glob("*.segment.incomplete.jsonl"))
            if not incomplete:
                raise ValueError(_("run {run} has no interrupted segment to recover").format(run=run_id))
            return recover_incomplete_segment(incomplete[-1])
    except LiveBusy as exc:
        raise ValueError(_("a live job is running; recover after it stops")) from exc


def export_observations(
    repository: ProjectRepository,
    inventory: Mapping[str, Any],
    signal_refs: Iterable[str],
    stream: TextIO,
    *,
    run_ids: Iterable[str] = (),
) -> int:
    refresh_cache(repository, inventory)
    states = signal_states(repository, inventory)
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
                state_label(states.get(signal_ref, {}), raw_number(row["raw_hex"])) or "",
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
        raise ValueError(_("no recorded run {run}").format(run=canonical))
    return directory


def _summarize(directory: Path) -> RunSummary:
    try:
        run_id = canonical_run_id(directory.name)
    except ValueError:
        return RunSummary(directory.name, UNREADABLE, detail=_("directory name is not a run id"))
    summary = RunSummary(run_id, COMPLETE)
    try:
        start = json.loads((directory / "run-start.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summary.state = UNREADABLE
        summary.detail = _("run-start is unreadable: {error}").format(error=exc)
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
            summary.detail = _("run-end is unreadable: {error}").format(error=exc)
    else:
        summary.state = NEEDS_RECOVERY
        summary.detail = _(
            "interrupted: recover it to keep the reads saved before the interruption, or move it to the trash"
        )
    return summary
