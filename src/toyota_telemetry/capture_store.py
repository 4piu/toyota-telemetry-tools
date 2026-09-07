"""Exact readers for immutable schema-1 run and segment capture trees."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .identity import AcquisitionRef, SignalRef, canonical_did, canonical_run_id


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def capture_set_hash(captures_root: Path | None) -> str:
    entries = []
    if captures_root is not None and captures_root.is_dir():
        for path in sorted(item for item in captures_root.rglob("*") if item.is_file()):
            entries.append(
                (path.relative_to(captures_root).as_posix(), _sha256(path.read_bytes()))
            )
    encoded = json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return _sha256(encoded)


def load_capture_tree(captures_root: Path, project_root: Path) -> tuple[CaptureRun, ...]:
    if not captures_root.is_dir():
        return ()
    runs: list[CaptureRun] = []
    for directory in sorted(path for path in captures_root.iterdir() if path.is_dir()):
        run_id = canonical_run_id(directory.name)
        start, _ = _exact_json(directory / "run-start.json")
        _keys(
            start,
            "run-start",
            {
                "document_type", "schema_version", "run_id", "project_id", "profile_id",
                "origin", "independence", "created_utc", "backend_kind", "endpoint_id",
                "ecu_key", "source_sha256", "admitted_plan_sha256", "privacy",
            },
        )
        if (
            start["document_type"] != "telemetry.run-start"
            or start["schema_version"] != 1
            or start["run_id"] != run_id
            or start["origin"] != "legacy_import"
            or start["independence"] != "unknown"
        ):
            raise ValueError(f"{directory}: invalid imported run-start")
        plan, plan_bytes = _exact_json(directory / "admitted-plan.json")
        _keys(
            plan,
            "admitted-plan",
            {
                "document_type", "schema_version", "project_id", "profile_id", "origin",
                "source_sha256", "endpoint_id", "acquisition_ids", "live_authorized",
            },
        )
        if (
            plan["document_type"] != "telemetry.admitted-plan"
            or plan["schema_version"] != 1
            or plan["origin"] != "legacy_import"
            or plan["live_authorized"] is not False
            or _sha256(plan_bytes) != start["admitted_plan_sha256"]
        ):
            raise ValueError(f"{directory}: admitted plan hash or schema mismatch")

        segment_paths = sorted(directory.glob("*.segment.complete.jsonl"))
        if not segment_paths:
            raise ValueError(f"{directory}: imported run has no complete segment")
        segments = tuple(
            _read_segment(path, run_id, project_root) for path in segment_paths
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

        end, _ = _exact_json(directory / "run-end.json")
        _keys(
            end,
            "run-end",
            {
                "document_type", "schema_version", "run_id", "reason", "segment_hashes",
                "source_sha256",
            },
        )
        expected_hashes = [segment.sha256 for segment in segments]
        if (
            end["document_type"] != "telemetry.run-end"
            or end["schema_version"] != 1
            or end["run_id"] != run_id
            or end["source_sha256"] != start["source_sha256"]
            or end["segment_hashes"] != expected_hashes
        ):
            raise ValueError(f"{directory}: run-end does not match its segments")
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


def _read_segment(path: Path, run_id: str, project_root: Path) -> CaptureSegment:
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
    _keys(
        start,
        "segment_start",
        {"record", "schema_version", "run_id", "segment_index", "origin", "source_sha256"},
    )
    if start["record"] != "segment_start" or start["schema_version"] != 1 or start["run_id"] != run_id:
        raise ValueError(f"{path}: invalid segment_start")
    index = start["segment_index"]
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ValueError(f"{path}: invalid segment index")
    exchanges: list[dict[str, Any]] = []
    for expected, exchange in enumerate(records[1:-1], 1):
        _validate_exchange(exchange, path, run_id, index, expected)
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
        or end["reason"] not in {"plan_complete", "operator_stop", "recovered_prefix"}
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
    exchange: dict[str, Any], path: Path, run_id: str, segment_index: int, sequence: int
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
    if outcome not in {"positive", "negative"}:
        raise ValueError(f"{path}: unsupported imported exchange outcome")
    if outcome == "positive" and (response is None or not response.startswith(bytes((0x62, acquisition.data_id >> 8, acquisition.data_id & 0xFF)))):
        raise ValueError(f"{path}: invalid positive response")
    if outcome == "negative" and (response is None or not response.startswith(b"\x7f\x22")):
        raise ValueError(f"{path}: invalid negative response")
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
    if not isinstance(exchange["objective_ids"], list) or not isinstance(exchange["observations"], list):
        raise ValueError(f"{path}: exchange arrays are invalid")
    for observation in exchange["observations"]:
        _keys(observation, "observation", {"signal_ref", "raw_hex", "decoded_value", "applicability"})
        ref = SignalRef.parse(observation["signal_ref"])
        if ref.data_id != acquisition.data_id or observation["applicability"] != "unknown":
            raise ValueError(f"{path}: observation identity or applicability mismatch")
        _hex_bytes(observation["raw_hex"], "observation.raw_hex")
        value = observation["decoded_value"]
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ValueError(f"{path}: decoded observation must be numeric or null")
