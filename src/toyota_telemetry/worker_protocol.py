"""Exact JSONL boundary for the private C# CAN/ISO-TP transaction worker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .project import atomic_json


SCHEMA_VERSION = 1


def build_worker_config(
    admitted_plan: Mapping[str, Any],
    endpoint_id: str,
    backend: Mapping[str, Any],
) -> dict[str, Any]:
    """Reduce one admitted endpoint and local adapter binding to worker input."""

    if admitted_plan.get("document_type") != "telemetry.admitted-plan":
        raise ValueError("worker requires an admitted-plan document")
    if admitted_plan.get("schema_version") != 1 or admitted_plan.get("admitted") is not True:
        raise ValueError("worker requires admitted-plan schema 1 with admission=true")
    if admitted_plan.get("live_authorized") is not True:
        raise ValueError("worker plan is not live-authorized")
    sessions = [
        row for row in admitted_plan.get("endpoint_sessions", [])
        if isinstance(row, Mapping) and row.get("endpoint_id") == endpoint_id
    ]
    if len(sessions) != 1:
        raise ValueError(f"worker endpoint must resolve exactly once: {endpoint_id}")
    session = sessions[0]
    expected_session = {
        "endpoint_id", "tester_can_id", "response_can_id", "session_recipe_id",
        "network", "request_start_floor_ms", "initial_response_timeout_ms",
        "response_pending_budget_ms", "transport_retries", "session_setup",
    }
    _exact_keys(session, expected_session, "endpoint session")

    _exact_keys(backend, {"kind", "executable", "settings"}, "local backend")
    kind = _text(backend["kind"], "local backend kind")
    _text(backend["executable"], "local backend executable")
    settings = _object(backend["settings"], "local backend settings")

    transactions = []
    for index, acquisition in enumerate(admitted_plan.get("acquisitions", [])):
        acquisition = _object(acquisition, f"acquisitions[{index}]")
        if acquisition.get("endpoint_id") != endpoint_id:
            continue
        request = _canonical_hex(acquisition.get("request_hex"), f"acquisitions[{index}].request_hex")
        data_id = _text(acquisition.get("data_id"), f"acquisitions[{index}].data_id")
        did = int(data_id, 16)
        expected = f"62 {did >> 8:02X} {did & 0xFF:02X}"
        transactions.append({
            "request_hex": request,
            "expected_prefix_hex": expected,
            "label": f"read {data_id}",
        })
    if not transactions:
        raise ValueError(f"worker endpoint has no admitted transactions: {endpoint_id}")
    transactions.sort(key=lambda row: row["request_hex"])

    network = _object(session["network"], "endpoint session network")
    _exact_keys(network, {"type", "bitrate", "addressing"}, "endpoint session network")
    setup = []
    for index, raw in enumerate(session["session_setup"]):
        row = _object(raw, f"endpoint session setup[{index}]")
        _exact_keys(
            row,
            {"request_hex", "expected_prefix_hex", "label", "tolerate_negative"},
            f"endpoint session setup[{index}]",
        )
        setup.append({
            "request_hex": _canonical_hex(row["request_hex"], "session request"),
            "expected_prefix_hex": _canonical_hex(row["expected_prefix_hex"], "session response"),
            "label": _text(row["label"], "session label"),
            "tolerate_negative": _boolean(row["tolerate_negative"], "session tolerance"),
        })
    if not setup:
        raise ValueError("worker session setup must not be empty")

    return {
        "document_type": "telemetry.adapter-worker",
        "schema_version": SCHEMA_VERSION,
        "profile_id": _text(admitted_plan.get("profile_id"), "plan profile_id"),
        "endpoint_id": endpoint_id,
        "transport": {
            "network": network["type"],
            "bitrate": _integer(network["bitrate"], "network bitrate", 10_000),
            "addressing": network["addressing"],
            "tester_can_id": session["tester_can_id"],
            "response_can_id": session["response_can_id"],
            "initial_response_timeout_ms": _integer(
                session["initial_response_timeout_ms"], "initial response timeout", 1
            ),
            "response_pending_budget_ms": _integer(
                session["response_pending_budget_ms"], "response-pending budget", 1
            ),
        },
        "session_setup": setup,
        "allowed_transactions": transactions,
        "device": {kind: dict(settings)},
    }


def write_worker_config(path: Path, config: Mapping[str, Any]) -> None:
    atomic_json(path, dict(config), replace=False)


def exchange_command(sequence: int, request_hex: str) -> str:
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError("worker command sequence must be a positive integer")
    value = {
        "command": "exchange",
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "request_hex": _canonical_hex(request_hex, "worker command request_hex"),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def stop_command() -> str:
    return '{"command":"stop","schema_version":1}'


def parse_event(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("worker emitted invalid JSON") from exc
    value = _object(value, "worker event")
    event = value.get("event")
    shapes = {
        "ready": {"event", "schema_version", "endpoint_id", "backend_kind"},
        "response_pending": {
            "event", "schema_version", "sequence", "pending_count", "pending_duration_ms",
        },
        "result": {
            "event", "schema_version", "sequence", "outcome", "response_hex", "nrc",
            "pending_count", "pending_duration_ms", "latency_ms", "detail",
        },
        "stopped": {"event", "schema_version", "reason"},
    }
    if event not in shapes:
        raise ValueError(f"unknown worker event: {event!r}")
    _exact_keys(value, shapes[event], f"worker {event} event")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("worker event schema mismatch")
    if event == "ready":
        _text(value["endpoint_id"], "worker endpoint_id")
        _text(value["backend_kind"], "worker backend_kind")
    elif event == "response_pending":
        _integer(value["sequence"], "worker sequence", 1)
        _integer(value["pending_count"], "worker pending_count", 1)
        _integer(value["pending_duration_ms"], "worker pending_duration_ms", 0)
    elif event == "result":
        _integer(value["sequence"], "worker sequence", 1)
        if value["outcome"] not in {"positive", "negative", "timeout", "transport_error", "abandoned"}:
            raise ValueError("worker result outcome is invalid")
        if value["response_hex"] is not None:
            _canonical_hex(value["response_hex"], "worker response_hex")
        if value["nrc"] is not None:
            nrc = _text(value["nrc"], "worker nrc")
            if len(nrc) != 4 or not nrc.startswith("0x"):
                raise ValueError("worker nrc must be canonical 0xNN")
            int(nrc[2:], 16)
        for name in ("pending_count", "pending_duration_ms", "latency_ms"):
            _integer(value[name], f"worker {name}", 0)
        if value["detail"] is not None:
            _text(value["detail"], "worker detail")
    elif value["reason"] != "stop":
        raise ValueError("worker stopped reason is invalid")
    return value


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], keys: set[str], where: str) -> None:
    if set(value) != keys:
        raise ValueError(f"{where} fields mismatch: expected {sorted(keys)}, got {sorted(value)}")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be a non-empty string")
    return value


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be boolean")
    return value


def _integer(value: Any, where: str, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{where} must be an integer >= {minimum}")
    return value


def _canonical_hex(value: Any, where: str) -> str:
    text = _text(value, where)
    parts = text.split(" ")
    if not parts or any(len(part) != 2 for part in parts):
        raise ValueError(f"{where} must be uppercase space-separated bytes")
    try:
        bytes(int(part, 16) for part in parts)
    except ValueError as exc:
        raise ValueError(f"{where} must be hexadecimal bytes") from exc
    canonical = " ".join(part.upper() for part in parts)
    if text != canonical:
        raise ValueError(f"{where} must use canonical uppercase hexadecimal")
    return text
