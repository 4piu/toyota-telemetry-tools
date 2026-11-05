"""Exact readers for immutable schema-1 run and segment capture trees."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Collection, Mapping

from .identity import AcquisitionRef, SignalRef, canonical_did, canonical_run_id


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_document_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _keys(value: Mapping[str, Any], where: str, expected: set[str]) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing:
        raise ValueError(f"{where} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _exact_json(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path}: UTF-8 BOM is not allowed")
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise ValueError(f"{path}: document must end with exactly one newline")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON: {exc}") from exc
    return _object(value, str(path)), data


def _hex_bytes(value: Any, where: str, *, nullable: bool = False) -> bytes | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value != value.upper():
        raise ValueError(f"{where} must be uppercase hexadecimal bytes")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{where} must be hexadecimal bytes") from exc
    if not decoded or decoded.hex(" ").upper() != value:
        raise ValueError(f"{where} must be canonical space-separated hexadecimal bytes")
    return decoded


@dataclass(frozen=True)
class CaptureSegment:
    path: Path
    relative_path: str
    sha256: str
    index: int
    start: dict[str, Any]
    exchanges: tuple[dict[str, Any], ...]
    end: dict[str, Any]


@dataclass(frozen=True)
class CaptureRun:
    run_id: str
    start: dict[str, Any]
    segments: tuple[CaptureSegment, ...]


_FILE_HASHES: dict[str, tuple[int, int, str]] = {}


def _file_sha256(entry: os.DirEntry) -> str:
    """Content hash, reused while the file's size and modification time hold.

    Capture files are written once and never edited in place, so a changed
    file always changes its size or modification time.
    """

    stat = entry.stat()
    cached = _FILE_HASHES.get(entry.path)
    if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
        return cached[2]
    with open(entry.path, "rb") as handle:
        digest = _sha256(handle.read())
    _FILE_HASHES[entry.path] = (stat.st_size, stat.st_mtime_ns, digest)
    return digest


def _capture_files(directory: str, prefix: str) -> list[tuple[str, os.DirEntry]]:
    files = []
    with os.scandir(directory) as entries:
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                files.extend(_capture_files(entry.path, relative + "/"))
            elif entry.is_file():
                files.append((relative, entry))
    return files


def capture_set_hash(captures_root: Path | None) -> str:
    entries = []
    if captures_root is not None and captures_root.is_dir():
        for relative, entry in sorted(_capture_files(str(captures_root), ""), key=lambda item: item[0]):
            entries.append((relative, _file_sha256(entry)))
    encoded = json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return _sha256(encoded)


def capture_run_hashes(captures_root: Path | None) -> dict[str, str]:
    """One content hash per run folder, keyed by folder name.

    It changes whenever a file in that run is added, removed, renamed, or
    rewritten, so a cache can re-read only the runs that changed.
    """

    hashes: dict[str, str] = {}
    if captures_root is None or not captures_root.is_dir():
        return hashes
    with os.scandir(captures_root) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            files = sorted(_capture_files(entry.path, ""), key=lambda item: item[0])
            encoded = json.dumps(
                [(relative, _file_sha256(file)) for relative, file in files],
                separators=(",", ":"), ensure_ascii=True,
            ).encode("ascii")
            hashes[entry.name] = _sha256(encoded)
    return hashes


def load_capture_tree(
    captures_root: Path, project_root: Path, names: Collection[str] | None = None
) -> tuple[CaptureRun, ...]:
    """Read and validate every run, or only the run folders in ``names``."""

    if not captures_root.is_dir():
        return ()
    runs: list[CaptureRun] = []
    for directory in sorted(
        path for path in captures_root.iterdir()
        if path.is_dir() and (names is None or path.name in names)
    ):
        run_id = canonical_run_id(directory.name)
        start, _ = _exact_json(directory / "run-start.json")
        origin = start.get("origin")
        if origin == "legacy_import":
            _keys(
                start,
                "run-start",
                {
                    "document_type", "schema_version", "run_id", "project_id", "profile_id",
                    "origin", "independence", "created_utc", "backend_kind", "endpoint_id",
                    "ecu_key", "source_sha256", "admitted_plan_sha256", "privacy",
                },
            )
            if start["independence"] != "unknown":
                raise ValueError(f"{directory}: invalid imported run independence")
        elif origin == "native":
            _keys(
                start,
                "run-start",
                {
                    "document_type", "schema_version", "run_id", "project_id", "profile_id",
                    "origin", "independence", "created_utc", "backend_kind", "endpoint_id",
                    "run_note", "source_hashes", "admitted_plan_sha256", "privacy",
                },
            )
            if start["independence"] not in {"unknown", "reviewed_independent"}:
                raise ValueError(f"{directory}: invalid native run independence")
        else:
            raise ValueError(f"{directory}: unsupported run origin")
        if (
            start["document_type"] != "telemetry.run-start"
            or start["schema_version"] != 1
            or start["run_id"] != run_id
        ):
            raise ValueError(f"{directory}: invalid run-start")
        plan, plan_bytes = _exact_json(directory / "admitted-plan.json")
        if origin == "legacy_import":
            _validate_imported_plan(plan, directory)
        else:
            _validate_native_plan(plan, directory, start)
        if _sha256(plan_bytes) != start["admitted_plan_sha256"]:
            raise ValueError(f"{directory}: admitted plan hash mismatch")

        segment_paths = sorted(directory.glob("*.segment.complete.jsonl"))
        if not segment_paths:
            if origin == "native" and list(directory.glob("*.segment.*complete.jsonl")):
                continue
            raise ValueError(f"{directory}: run has no complete segment")
        segments = tuple(
            _read_segment(path, run_id, project_root, origin, start["admitted_plan_sha256"])
            for path in segment_paths
        )
        if [segment.index for segment in segments] != list(range(1, len(segments) + 1)):
            raise ValueError(f"{directory}: segment indexes are not contiguous")
        run_sequences = [
            exchange["run_sequence"]
            for segment in segments
            for exchange in segment.exchanges
        ]
        if run_sequences != list(range(1, len(run_sequences) + 1)):
            raise ValueError(f"{directory}: run-global exchange sequence is not contiguous")

        run_end_path = directory / "run-end.json"
        if not run_end_path.is_file():
            if origin == "native" and list(directory.glob("*.segment.*complete.jsonl")):
                runs.append(CaptureRun(run_id, start, segments))
                continue
            raise FileNotFoundError(run_end_path)
        end, _ = _exact_json(run_end_path)
        end_keys = {"document_type", "schema_version", "run_id", "reason", "segment_hashes"}
        if origin == "legacy_import":
            end_keys.add("source_sha256")
        _keys(end, "run-end", end_keys)
        expected_hashes = [segment.sha256 for segment in segments]
        if (
            end["document_type"] != "telemetry.run-end"
            or end["schema_version"] != 1
            or end["run_id"] != run_id
            or end["segment_hashes"] != expected_hashes
            or end["reason"] not in (
                {"plan_complete", "operator_stop", "recovered_prefix"}
                if origin == "legacy_import"
                else {"operator_stop", "transport_lost", "plan_complete", "size_limit", "recovered_prefix",
                      "slice_end"}
            )
        ):
            raise ValueError(f"{directory}: run-end does not match its segments")
        if origin == "legacy_import" and end["source_sha256"] != start["source_sha256"]:
            raise ValueError(f"{directory}: imported run-end source mismatch")
        runs.append(CaptureRun(run_id, start, segments))
    return tuple(runs)


def inspect_capture_tree(captures_root: Path, project_root: Path) -> dict[str, Any]:
    """Return a privacy-minimized structural summary without payload bytes."""

    runs = load_capture_tree(captures_root, project_root)
    outcomes: dict[str, int] = {}
    segments = 0
    exchanges = 0
    observations = 0
    for run in runs:
        segments += len(run.segments)
        for segment in run.segments:
            exchanges += len(segment.exchanges)
            for exchange in segment.exchanges:
                outcome = exchange["outcome"]
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                observations += len(exchange["observations"])
    return {
        "document_type": "telemetry.capture-inspection",
        "schema_version": 1,
        "capture_set_sha256": capture_set_hash(captures_root),
        "runs": len(runs),
        "segments": segments,
        "exchanges": exchanges,
        "observations": observations,
        "outcomes": dict(sorted(outcomes.items())),
        "privacy": "No payload bytes, timestamps, notes, adapter identifiers, or paths are reported.",
    }


def _read_segment(
    path: Path,
    run_id: str,
    project_root: Path,
    origin: str,
    admitted_plan_sha256: str,
) -> CaptureSegment:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf") or not data.endswith(b"\n"):
        raise ValueError(f"{path}: segment must be UTF-8 without BOM and newline-terminated")
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(data.splitlines(), 1):
        try:
            records.append(_object(json.loads(raw.decode("utf-8")), f"{path}:{line_number}"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    if len(records) < 2:
        raise ValueError(f"{path}: segment is incomplete")
    start, end = records[0], records[-1]
    start_keys = {"record", "schema_version", "run_id", "segment_index", "origin"}
    start_keys.add("source_sha256" if origin == "legacy_import" else "admitted_plan_sha256")
    if origin == "native" and (
        "recovered_from_sha256" in start or "recovered_prefix_bytes" in start
    ):
        start_keys.update({"recovered_from_sha256", "recovered_prefix_bytes"})
    _keys(start, "segment_start", start_keys)
    if (
        start["record"] != "segment_start"
        or start["schema_version"] != 1
        or start["run_id"] != run_id
        or start["origin"] != origin
    ):
        raise ValueError(f"{path}: invalid segment_start")
    if origin == "native" and start["admitted_plan_sha256"] != admitted_plan_sha256:
        raise ValueError(f"{path}: segment plan hash mismatch")
    if "recovered_prefix_bytes" in start and (
        not isinstance(start["recovered_prefix_bytes"], int)
        or isinstance(start["recovered_prefix_bytes"], bool)
        or start["recovered_prefix_bytes"] < 1
    ):
        raise ValueError(f"{path}: invalid recovered prefix length")
    index = start["segment_index"]
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ValueError(f"{path}: invalid segment index")
    exchanges: list[dict[str, Any]] = []
    for expected, exchange in enumerate(records[1:-1], 1):
        _validate_exchange(exchange, path, run_id, index, expected, origin)
        exchanges.append(exchange)
    _keys(
        end,
        "segment_end",
        {"record", "schema_version", "run_id", "segment_index", "reason", "exchange_count"},
    )
    if (
        end["record"] != "segment_end"
        or end["schema_version"] != 1
        or end["run_id"] != run_id
        or end["segment_index"] != index
        or end["exchange_count"] != len(exchanges)
        or end["reason"] not in (
            {"plan_complete", "operator_stop", "recovered_prefix"}
            if origin == "legacy_import"
            else {
                "rotation", "operator_stop", "transport_lost", "plan_complete",
                "size_limit", "recovered_prefix", "slice_end",
            }
        )
    ):
        raise ValueError(f"{path}: invalid segment_end")
    return CaptureSegment(
        path,
        path.resolve().relative_to(project_root.resolve()).as_posix(),
        _sha256(data),
        index,
        start,
        tuple(exchanges),
        end,
    )


def _validate_exchange(
    exchange: dict[str, Any],
    path: Path,
    run_id: str,
    segment_index: int,
    sequence: int,
    origin: str,
) -> None:
    _keys(
        exchange,
        "exchange",
        {
            "record", "schema_version", "run_id", "segment_index", "segment_sequence",
            "run_sequence", "acquisition_id", "endpoint_id", "data_id", "response_utc",
            "request_elapsed_ms", "response_elapsed_ms", "request_hex", "response_hex",
            "outcome", "negative_response_code", "pending_count", "pending_duration_ms",
            "latency_ms", "retry_count", "scheduler_reason", "objective_ids", "observations",
        },
    )
    if (
        exchange["record"] != "exchange"
        or exchange["schema_version"] != 1
        or exchange["run_id"] != run_id
        or exchange["segment_index"] != segment_index
        or exchange["segment_sequence"] != sequence
    ):
        raise ValueError(f"{path}: invalid exchange sequence")
    if (
        not isinstance(exchange["run_sequence"], int)
        or isinstance(exchange["run_sequence"], bool)
        or exchange["run_sequence"] < 1
    ):
        raise ValueError(f"{path}: invalid run-global exchange sequence")
    acquisition = AcquisitionRef.parse(exchange["acquisition_id"])
    if acquisition.endpoint_id != exchange["endpoint_id"]:
        raise ValueError(f"{path}: exchange endpoint identity mismatch")
    if canonical_did(exchange["data_id"]) != exchange["data_id"] or acquisition.data_id != int(exchange["data_id"], 16):
        raise ValueError(f"{path}: exchange DID identity mismatch")
    request = _hex_bytes(exchange["request_hex"], "exchange.request_hex")
    response = _hex_bytes(exchange["response_hex"], "exchange.response_hex", nullable=True)
    if request != bytes((0x22, acquisition.data_id >> 8, acquisition.data_id & 0xFF)):
        raise ValueError(f"{path}: exchange is not the declared 0x22 request")
    outcome = exchange["outcome"]
    allowed_outcomes = (
        {"positive", "negative"}
        if origin == "legacy_import"
        else {"positive", "negative", "timeout", "transport_error", "abandoned"}
    )
    if outcome not in allowed_outcomes:
        raise ValueError(f"{path}: unsupported exchange outcome")
    if outcome == "positive" and (response is None or not response.startswith(bytes((0x62, acquisition.data_id >> 8, acquisition.data_id & 0xFF)))):
        raise ValueError(f"{path}: invalid positive response")
    if outcome == "negative" and (response is None or not response.startswith(b"\x7f\x22")):
        raise ValueError(f"{path}: invalid negative response")
    if outcome in {"timeout", "transport_error", "abandoned"} and response is not None:
        raise ValueError(f"{path}: failed exchange must not contain a response")
    for field in ("request_elapsed_ms", "response_elapsed_ms", "latency_ms", "retry_count"):
        if not isinstance(exchange[field], int) or isinstance(exchange[field], bool) or exchange[field] < 0:
            raise ValueError(f"{path}: exchange.{field} must be a nonnegative integer")
    for field in ("pending_count", "pending_duration_ms"):
        if exchange[field] is not None and (
            not isinstance(exchange[field], int)
            or isinstance(exchange[field], bool)
            or exchange[field] < 0
        ):
            raise ValueError(f"{path}: exchange.{field} must be a nonnegative integer or null")
    if exchange["response_elapsed_ms"] < exchange["request_elapsed_ms"]:
        raise ValueError(f"{path}: exchange response precedes request")
    if (
        not isinstance(exchange["objective_ids"], list)
        or not all(isinstance(value, str) and value for value in exchange["objective_ids"])
        or not isinstance(exchange["observations"], list)
    ):
        raise ValueError(f"{path}: exchange arrays are invalid")
    if outcome != "positive" and exchange["observations"]:
        raise ValueError(f"{path}: failed exchange must not contain observations")
    for observation in exchange["observations"]:
        _keys(observation, "observation", {"signal_ref", "raw_hex", "decoded_value", "applicability"})
        ref = SignalRef.parse(observation["signal_ref"])
        allowed_applicability = (
            {"unknown"} if origin == "legacy_import" else {"true", "false", "unknown"}
        )
        if (
            ref.data_id != acquisition.data_id
            or observation["applicability"] not in allowed_applicability
        ):
            raise ValueError(f"{path}: observation identity or applicability mismatch")
        _hex_bytes(observation["raw_hex"], "observation.raw_hex")
        value = observation["decoded_value"]
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ValueError(f"{path}: decoded observation must be numeric or null")


def _validate_imported_plan(plan: dict[str, Any], directory: Path) -> None:
    _keys(
        plan,
        "admitted-plan",
        {
            "document_type", "schema_version", "project_id", "profile_id", "origin",
            "source_sha256", "endpoint_id", "acquisition_ids", "collection_settings",
            "live_authorized",
        },
    )
    if (
        plan["document_type"] != "telemetry.admitted-plan"
        or plan["schema_version"] != 1
        or plan["origin"] != "legacy_import"
        or plan["live_authorized"] is not False
    ):
        raise ValueError(f"{directory}: imported admitted plan schema mismatch")
    settings = _object(plan["collection_settings"], "admitted-plan.collection_settings")
    _keys(
        settings,
        "admitted-plan.collection_settings",
        {"authority", "mode", "batch_id", "period_ms"},
    )
    if settings["authority"] != "legacy_snapshot":
        raise ValueError(f"{directory}: imported settings are not a legacy snapshot")


def _validate_native_plan(
    plan: dict[str, Any], directory: Path, start: Mapping[str, Any]
) -> None:
    _keys(
        plan,
        "admitted-plan",
        {
            "document_type", "schema_version", "project_id", "profile_id", "created_utc",
            "source_hashes", "admitted", "live_authorized", "admission", "stress",
            "storage", "endpoint_sessions", "acquisitions", "objectives", "decodes",
            "content_sha256",
        },
    )
    if (
        plan["document_type"] != "telemetry.admitted-plan"
        or plan["schema_version"] != 1
        or plan["admitted"] is not True
        or plan["live_authorized"] is not True
        or plan["project_id"] != start["project_id"]
        or plan["profile_id"] != start["profile_id"]
        or plan["source_hashes"] != start["source_hashes"]
    ):
        raise ValueError(f"{directory}: native admitted plan schema mismatch")
    content = dict(plan)
    content_sha256 = content.pop("content_sha256")
    if _sha256(canonical_document_bytes(content)) != content_sha256:
        raise ValueError(f"{directory}: native admitted plan content hash mismatch")
