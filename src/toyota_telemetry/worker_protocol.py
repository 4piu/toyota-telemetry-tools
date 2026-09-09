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
        transaction_id = _text(
            acquisition.get("acquisition_id"), f"acquisitions[{index}].acquisition_id"
        )
        request = _canonical_hex(acquisition.get("request_hex"), f"acquisitions[{index}].request_hex")
        data_id = _text(acquisition.get("data_id"), f"acquisitions[{index}].data_id")
        did = int(data_id, 16)
        expected = f"62 {did >> 8:02X} {did & 0xFF:02X}"
        transactions.append({
            "transaction_id": transaction_id,
            "request_hex": request,
            "expected_prefix_hex": expected,
            "label": f"read {data_id}",
        })
    if not transactions:
        raise ValueError(f"worker endpoint has no admitted transactions: {endpoint_id}")
    transactions.sort(key=lambda row: row["transaction_id"])

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


def build_probe_config(
    transport: Mapping[str, Any],
    candidate_id: str,
    tester_can_id: str,
    response_can_id: str,
    session_recipe_id: str,
    backend: Mapping[str, Any],
    *,
    read_data_id: str | None = None,
) -> dict[str, Any]:
    """Build one temporary, single-shot probe config for a candidate CAN ID
    pair, replaying an already-reviewed session recipe from tracked
    transport authority. Used by `telemetry discover endpoints` (no
    read_data_id) and `telemetry preflight dids` (one read_data_id)."""

    _exact_keys(backend, {"kind", "executable", "settings"}, "local backend")
    kind = _text(backend["kind"], "local backend kind")
    settings = _object(backend["settings"], "local backend settings")

    network = _object(transport["network"], "transport network")
    recipes = {row["id"]: row for row in transport.get("session_recipes", [])}
    if session_recipe_id not in recipes:
        raise ValueError(f"unknown session_recipe_id: {session_recipe_id}")
    session_setup = []
    for row in recipes[session_recipe_id]["steps"]:
        session_setup.append({
            "request_hex": _canonical_hex(row["request_hex"], "session request"),
            "expected_prefix_hex": _canonical_hex(row["expected_prefix_hex"], "session response"),
            "label": _text(row["label"], "session label"),
        })

    safety = _object(transport["safety"], "transport safety")
    config: dict[str, Any] = {
        "document_type": "telemetry.adapter-probe",
        "schema_version": SCHEMA_VERSION,
        "candidate_id": _text(candidate_id, "candidate_id"),
        "transport": {
            "network": network["type"],
            "bitrate": _integer(network["bitrate"], "network bitrate", 10_000),
            "addressing": network["addressing"],
            "tester_can_id": _text(tester_can_id, "tester_can_id"),
            "response_can_id": _text(response_can_id, "response_can_id"),
            "initial_response_timeout_ms": _integer(
                safety["initial_response_timeout_ms"], "initial response timeout", 1
            ),
            "response_pending_budget_ms": _integer(
                safety["response_pending_budget_ms"], "response-pending budget", 1
            ),
        },
        "session_setup": session_setup,
        "device": {kind: dict(settings)},
    }
    if read_data_id is not None:
        did = int(read_data_id, 16)
        config["preflight_read"] = {
            "request_hex": f"22 {did >> 8:02X} {did & 0xFF:02X}",
            "expected_prefix_hex": f"62 {did >> 8:02X} {did & 0xFF:02X}",
            "label": f"read {read_data_id}",
        }
    return config


def write_probe_config(path: Path, config: Mapping[str, Any]) -> None:
    atomic_json(path, dict(config), replace=False)


def parse_probe_result(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("probe emitted invalid JSON") from exc
    value = _object(value, "probe event")
    if value.get("event") != "probe_result":
        raise ValueError(f"unknown probe event: {value.get('event')!r}")
    _exact_keys(
        value,
        {
            "event", "schema_version", "candidate_id", "backend_kind", "responded",
            "session_steps", "read",
        },
        "probe result",
    )
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("probe event schema mismatch")
    _text(value["candidate_id"], "probe candidate_id")
    _text(value["backend_kind"], "probe backend_kind")
    _boolean(value["responded"], "probe responded")
    if not isinstance(value["session_steps"], list):
        raise ValueError("probe session_steps must be a list")
    for step in value["session_steps"]:
        step = _object(step, "probe session step")
        _exact_keys(
            step, {"label", "outcome", "latency_ms", "nrc", "response_hex"}, "probe session step"
        )
        _text(step["label"], "probe step label")
        _probe_step_outcome(step)
    if value["read"] is not None:
        read = _object(value["read"], "probe read")
        if read.get("attempted") is False:
            _exact_keys(read, {"attempted", "outcome"}, "probe read")
            if read["outcome"] != "session_unavailable":
                raise ValueError("probe read outcome must be session_unavailable when not attempted")
        else:
            _exact_keys(
                read, {"attempted", "outcome", "latency_ms", "nrc", "response_hex"}, "probe read"
            )
            _probe_step_outcome(read)
    return value


def _probe_step_outcome(step: Mapping[str, Any]) -> None:
    if step["outcome"] not in {"positive", "negative", "timeout", "transport_error"}:
        raise ValueError("probe step outcome is invalid")
    _integer(step["latency_ms"], "probe step latency_ms", 0)
    if step["nrc"] is not None:
        nrc = _text(step["nrc"], "probe step nrc")
        if len(nrc) != 4 or not nrc.startswith("0x"):
            raise ValueError("probe step nrc must be canonical 0xNN")
        int(nrc[2:], 16)
    if step["response_hex"] is not None:
        _canonical_hex(step["response_hex"], "probe step response_hex")


def exchange_command(sequence: int, transaction_id: str) -> str:
    """Select one entry from the worker's own immutable transaction table.

    The command never resupplies request bytes: it names a transaction_id the
    worker already holds from its launch configuration, so a compromised or
    buggy caller cannot substitute a different payload at runtime.
    """

    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError("worker command sequence must be a positive integer")
    value = {
        "command": "exchange",
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "transaction_id": _text(transaction_id, "worker command transaction_id"),
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
