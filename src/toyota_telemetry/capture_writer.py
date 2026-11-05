"""Create-new writer for native schema-1 run and segment captures."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping

from .capture_store import _validate_exchange, canonical_document_bytes
from .identity import canonical_run_id


class CaptureLimitReached(RuntimeError):
    """The current run finalized cleanly at a configured storage boundary."""


class NativeRunWriter:
    """Write one immutable run while rotating segments inside one live session."""

    def __init__(
        self,
        captures_root: Path,
        plan: Mapping[str, Any],
        plan_bytes: bytes,
        *,
        run_id: str,
        created_utc: str,
        backend_kind: str,
        endpoint_id: str | None = None,
        run_note: str | None = None,
        independence: str = "unknown",
        disk_free_bytes: Callable[[Path], int] | None = None,
        storage_unit_bytes: int = 1024 * 1024,
    ) -> None:
        self.plan = dict(plan)
        if canonical_document_bytes(self.plan) != plan_bytes:
            raise ValueError("admitted plan bytes do not match the supplied plan")
        if (
            self.plan.get("document_type") != "telemetry.admitted-plan"
            or self.plan.get("schema_version") != 1
            or self.plan.get("admitted") is not True
            or self.plan.get("live_authorized") is not True
        ):
            raise ValueError("native capture requires a live-authorized admitted plan")
        sessions = self.plan.get("endpoint_sessions")
        if not isinstance(sessions, list) or not sessions:
            raise ValueError("native capture requires at least one endpoint session")
        available_endpoints = [str(row["endpoint_id"]) for row in sessions]
        if endpoint_id is None:
            if len(available_endpoints) != 1:
                raise ValueError("endpoint_id is required for a multi-endpoint admitted plan")
            endpoint_id = available_endpoints[0]
        if endpoint_id not in available_endpoints:
            raise ValueError(f"endpoint is absent from admitted plan: {endpoint_id}")
        self.endpoint_id = endpoint_id
        if independence not in {"unknown", "reviewed_independent"}:
            raise ValueError("invalid run independence")
        if not isinstance(created_utc, str) or not created_utc:
            raise ValueError("created_utc must be a non-empty string")
        if not isinstance(backend_kind, str) or not backend_kind:
            raise ValueError("backend_kind must be a non-empty string")
        if run_note is not None and not isinstance(run_note, str):
            raise ValueError("run_note must be a string or null")
        if storage_unit_bytes < 1:
            raise ValueError("storage unit must be positive")

        self.run_id = canonical_run_id(run_id)
        self.captures_root = captures_root.resolve()
        self.run_directory = self.captures_root / self.run_id
        storage = self.plan["storage"]
        self.segment_duration_ms = int(storage["segment_seconds"]) * 1000
        self.segment_limit_bytes = int(storage["segment_max_mib"]) * storage_unit_bytes
        self.tree_limit_bytes = int(storage["capture_tree_max_mib"]) * storage_unit_bytes
        self.disk_reserve_bytes = int(storage["disk_reserve_mib"]) * storage_unit_bytes
        self._disk_free_bytes = disk_free_bytes or (lambda path: shutil.disk_usage(path).free)
        self.plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
        self.segment_index = 0
        self.run_sequence = 0
        self.segment_sequence = 0
        self.segment_started_ms = 0
        self.segment_hashes: list[str] = []
        self._stream: Any = None
        self._partial_path: Path | None = None
        self._closed = False

        self.captures_root.mkdir(parents=True, exist_ok=True)
        self._assert_next_segment_budget()
        self.run_directory.mkdir(parents=False, exist_ok=False)
        _write_new(self.run_directory / "admitted-plan.json", plan_bytes)
        run_start = {
            "document_type": "telemetry.run-start",
            "schema_version": 1,
            "run_id": self.run_id,
            "project_id": self.plan["project_id"],
            "profile_id": self.plan["profile_id"],
            "origin": "native",
            "independence": independence,
            "created_utc": created_utc,
            "backend_kind": backend_kind,
            "endpoint_id": self.endpoint_id,
            "run_note": run_note,
            "source_hashes": self.plan["source_hashes"],
            "admitted_plan_sha256": self.plan_sha256,
            "privacy": "VIN, location, adapter serial, and machine paths are not included",
        }
        _write_new(self.run_directory / "run-start.json", canonical_document_bytes(run_start))
        self._open_segment(0)

    @property
    def partial_path(self) -> Path | None:
        return self._partial_path

    def needs_rotation(self, now_ms: int) -> bool:
        return now_ms - self.segment_started_ms >= self.segment_duration_ms

    def append_exchange(self, exchange: Mapping[str, Any]) -> dict[str, Any]:
        self._require_open()
        self.segment_sequence += 1
        self.run_sequence += 1
        record = {
            "record": "exchange",
            "schema_version": 1,
            "run_id": self.run_id,
            "segment_index": self.segment_index,
            "segment_sequence": self.segment_sequence,
            "run_sequence": self.run_sequence,
            **dict(exchange),
        }
        assert self._partial_path is not None
        _validate_exchange(
            record,
            self._partial_path,
            self.run_id,
            self.segment_index,
            self.segment_sequence,
            "native",
        )
        encoded = _jsonl_record(record)
        end_reserve = len(_jsonl_record(self._segment_end("size_limit")))
        if self._stream.tell() + len(encoded) + end_reserve > self.segment_limit_bytes:
            self.segment_sequence -= 1
            self.run_sequence -= 1
            self._finish_segment("size_limit")
            self._finish_run("size_limit")
            raise CaptureLimitReached("segment size limit reached; run finalized cleanly")
        self._stream.write(encoded)
        self._stream.flush()
        os.fsync(self._stream.fileno())
        return record

    def rotate(self, now_ms: int) -> Path:
        self._require_open()
        if not self.needs_rotation(now_ms):
            raise ValueError("segment rotation requested before its duration elapsed")
        try:
            self._assert_next_segment_budget()
        except CaptureLimitReached:
            self._finish_segment("size_limit")
            self._finish_run("size_limit")
            raise
        complete = self._finish_segment("rotation")
        self._open_segment(now_ms)
        return complete

    def finish(self, reason: str) -> Path:
        self._require_open()
        if reason not in {"operator_stop", "transport_lost", "plan_complete", "slice_end"}:
            raise ValueError(f"invalid native run completion reason: {reason}")
        complete = self._finish_segment(reason)
        self._finish_run(reason)
        return complete

    def crash(self) -> Path | None:
        if self._closed:
            return None
        incomplete = None
        if self._stream is not None and self._partial_path is not None:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            incomplete = self._partial_path.with_name(
                self._partial_path.name.replace(".partial.jsonl", ".incomplete.jsonl")
            )
            os.replace(self._partial_path, incomplete)
            self._stream = None
            self._partial_path = None
        self._closed = True
        return incomplete

    def __enter__(self) -> "NativeRunWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self._closed:
            self.crash()

    def _open_segment(self, started_ms: int) -> None:
        self.segment_index += 1
        self.segment_sequence = 0
        self.segment_started_ms = started_ms
        self._partial_path = self.run_directory / f"{self.segment_index:06d}.segment.partial.jsonl"
        self._stream = self._partial_path.open("xb")
        start = {
            "record": "segment_start",
            "schema_version": 1,
            "run_id": self.run_id,
            "segment_index": self.segment_index,
            "origin": "native",
            "admitted_plan_sha256": self.plan_sha256,
        }
        self._stream.write(_jsonl_record(start))
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def _segment_end(self, reason: str) -> dict[str, Any]:
        return {
            "record": "segment_end",
            "schema_version": 1,
            "run_id": self.run_id,
            "segment_index": self.segment_index,
            "reason": reason,
            "exchange_count": self.segment_sequence,
        }

    def _finish_segment(self, reason: str) -> Path:
        assert self._stream is not None and self._partial_path is not None
        self._stream.write(_jsonl_record(self._segment_end(reason)))
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        complete = self._partial_path.with_name(
            self._partial_path.name.replace(".partial.jsonl", ".complete.jsonl")
        )
        os.replace(self._partial_path, complete)
        self.segment_hashes.append(hashlib.sha256(complete.read_bytes()).hexdigest())
        self._stream = None
        self._partial_path = None
        return complete

    def _finish_run(self, reason: str) -> None:
        run_end = {
            "document_type": "telemetry.run-end",
            "schema_version": 1,
            "run_id": self.run_id,
            "reason": reason,
            "segment_hashes": self.segment_hashes,
        }
        _write_new(self.run_directory / "run-end.json", canonical_document_bytes(run_end))
        self._closed = True

    def _assert_next_segment_budget(self) -> None:
        used = sum(
            path.stat().st_size
            for path in self.captures_root.rglob("*")
            if path.is_file()
        )
        if used + self.segment_limit_bytes > self.tree_limit_bytes:
            raise CaptureLimitReached("capture-tree limit cannot reserve the next segment")
        required_free = self.disk_reserve_bytes + self.segment_limit_bytes
        if self._disk_free_bytes(self.captures_root) < required_free:
            raise CaptureLimitReached("disk reserve cannot allocate the next segment")

    def _require_open(self) -> None:
        if self._closed or self._stream is None:
            raise RuntimeError("native capture run is not open")


def _jsonl_record(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def recover_incomplete_segment(path: Path) -> Path:
    """Copy a structurally valid prefix into a new complete segment and close its run."""

    if not path.name.endswith(".segment.incomplete.jsonl"):
        raise ValueError("recovery source must be an incomplete segment")
    source = path.read_bytes()
    source_sha256 = hashlib.sha256(source).hexdigest()
    lines = source.splitlines(keepends=True)
    if not lines or not lines[0].endswith(b"\n"):
        raise ValueError("incomplete segment has no complete start record")
    try:
        start = json.loads(lines[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("incomplete segment has invalid start record") from exc
    required = {
        "record", "schema_version", "run_id", "segment_index", "origin",
        "admitted_plan_sha256",
    }
    if (
        not isinstance(start, dict)
        or set(start) != required
        or start["record"] != "segment_start"
        or start["schema_version"] != 1
        or start["origin"] != "native"
    ):
        raise ValueError("incomplete segment start is not recoverable native schema 1")
    run_id = canonical_run_id(start["run_id"])
    segment_index = start["segment_index"]
    if not isinstance(segment_index, int) or isinstance(segment_index, bool) or segment_index < 1:
        raise ValueError("incomplete segment index is invalid")
    exchanges = []
    prefix_bytes = len(lines[0])
    for line in lines[1:]:
        if not line.endswith(b"\n"):
            break
        try:
            record = json.loads(line.decode("utf-8"))
            if not isinstance(record, dict) or record.get("record") != "exchange":
                break
            _validate_exchange(
                record,
                path,
                run_id,
                segment_index,
                len(exchanges) + 1,
                "native",
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            break
        exchanges.append(record)
        prefix_bytes += len(line)
    if not exchanges:
        raise ValueError("incomplete segment has no recoverable exchange")

    recovered_start = {
        **start,
        "recovered_from_sha256": source_sha256,
        "recovered_prefix_bytes": prefix_bytes,
    }
    end = {
        "record": "segment_end",
        "schema_version": 1,
        "run_id": run_id,
        "segment_index": segment_index,
        "reason": "recovered_prefix",
        "exchange_count": len(exchanges),
    }
    output = path.with_name(path.name.replace(".incomplete.jsonl", ".complete.jsonl"))
    encoded = b"".join(_jsonl_record(value) for value in [recovered_start, *exchanges, end])
    _write_new(output, encoded)

    run_directory = path.parent
    run_end_path = run_directory / "run-end.json"
    if run_end_path.exists():
        output.unlink()
        raise ValueError("cannot recover a run that already has run-end metadata")
    complete_paths = sorted(run_directory.glob("*.segment.complete.jsonl"))
    expected_indexes = list(range(1, len(complete_paths) + 1))
    actual_indexes = [int(item.name.split(".", 1)[0]) for item in complete_paths]
    if actual_indexes != expected_indexes:
        output.unlink()
        raise ValueError("recovered run segment indexes are not contiguous")
    run_end = {
        "document_type": "telemetry.run-end",
        "schema_version": 1,
        "run_id": run_id,
        "reason": "recovered_prefix",
        "segment_hashes": [hashlib.sha256(item.read_bytes()).hexdigest() for item in complete_paths],
    }
    _write_new(run_end_path, canonical_document_bytes(run_end))
    return output
