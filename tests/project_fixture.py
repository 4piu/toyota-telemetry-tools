from __future__ import annotations

import hashlib
import json
from pathlib import Path


ENDPOINT_ID = "700-708-session-a1769d23a560"
RUN_ID = "8f09bc54-93b1-5a1a-872d-11fdcf7f0232"
SOURCE_SHA256 = "8e4016a95fcc5847456e62df346140dab3c7b6f83ce784ebb354ef7149573fd6"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_json(path: Path, value: object) -> bytes:
    data = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def make_current_project(root: Path, *, include_capture: bool = True) -> list[dict]:
    root.joinpath("telemetry-project.toml").write_text(
        "\n".join([
            "schema_version = 2",
            'project_kind = "vehicle"',
            'project_id = "test-project"',
            "",
            "[toolkit]",
            'repository = "https://example.invalid/tools.git"',
            'revision = "main"',
            "",
            "[paths]",
            'transport = "config/transport.json"',
            'collection_policy = "config/collection-policy.json"',
            'signal_review = "review/signals.json"',
            'catalog_manifest = "result/catalog-manifest.json"',
            'result = "result"',
            'scratch = "scratch"',
            'local_settings = "scratch/local-settings.json"',
            'database = "scratch/project.sqlite"',
            'captures = "scratch/captures"',
            'plans = "scratch/plans"',
            'toolkit = "../telemetry-tools"',
            "",
        ]),
        encoding="utf-8",
    )
    _write_json(root / "config/transport.json", {
        "document_type": "telemetry.transport",
        "schema_version": 1,
        "profile_id": "test-profile",
        "network": {"type": "classical-can", "bitrate": 500000, "addressing": "normal"},
        "safety": {
            "did_min": "0x0100",
            "did_max": "0xEFFF",
            "request_start_floor_ms": 100,
            "maximum_request_rate_hz": 10,
            "maximum_estimated_bus_utilization": 0.05,
            "maximum_nominal_session_utilization": 0.5,
            "initial_response_timeout_ms": 1000,
            "response_pending_budget_ms": 10000,
            "transport_retries": 2,
        },
        "session_recipes": [{
            "id": "session-a1769d23a560",
            "steps": [{
                "request_hex": "10 03",
                "expected_prefix_hex": "50 03",
                "label": "extended",
                "tolerate_negative": False,
            }],
        }],
        "endpoints": [{
            "id": ENDPOINT_ID,
            "tester_can_id": "0x700",
            "response_can_id": "0x708",
            "session_recipe_id": "session-a1769d23a560",
            "ecu_keys": [372],
            "state": "confirmed",
            "provenance": ["docs/evidence.md"],
        }],
        "dynamic_dids": [],
    })
    _write_json(root / "config/collection-policy.json", {
        "document_type": "telemetry.collection-policy",
        "schema_version": 1,
        "profile_id": "test-profile",
        "storage": {
            "segment_seconds": 60,
            "segment_max_mib": 16,
            "capture_tree_max_mib": 2048,
            "disk_reserve_mib": 256,
        },
        "class_defaults": {},
        "acquisitions": [],
        "objectives": [],
    })
    _write_json(root / "review/signals.json", {
        "document_type": "telemetry.signal-review",
        "schema_version": 1,
        "profile_id": "test-profile",
        "signals": [{
            "signal_ref": "372/0x1002/0..15",
            "catalog_variant_id": None,
            "display_name_override": "Battery voltage",
            "formula_override": "raw / 1000",
            "unit_override": "V",
            "evidence_state": "Observed and replayed",
            "notes": "Reviewed decode.",
            "evidence_refs": [],
        }],
    })
    _write_json(root / "result/catalog-manifest.json", {
        "document_type": "telemetry.catalog-manifest",
        "schema_version": 1,
        "profile_id": "test-profile",
        "catalog_row_count": 1,
        "legacy_manifest_sha256": "d9b547f5d5355a93fd222072eda0e4d037461e0c155a7605e4fe1603eec6c9d0",
    })
    _write_json(root / "scratch/local-settings.json", {
        "document_type": "telemetry.local-settings",
        "schema_version": 1,
        "project_id": "test-project",
        "backend": {
            "kind": "pcan",
            "executable": "scratch/bin/telemetry-pcan.exe",
            "settings": {"channel": "PCAN_USBBUS1"},
        },
    })
    rows = [{
        "formula": "raw / 1000",
        "internal_id": "0x0001",
        "name": "Battery voltage",
        "signal_ref": "372/0x1002/0..15",
        "unit": "V",
    }]
    if include_capture:
        _write_imported_capture(root / "scratch/captures")
    return rows


def _write_imported_capture(captures: Path) -> None:
    directory = captures / RUN_ID
    directory.mkdir(parents=True)
    plan = {
        "document_type": "telemetry.admitted-plan",
        "schema_version": 1,
        "project_id": "test-project",
        "profile_id": "test-profile",
        "origin": "legacy_import",
        "source_sha256": SOURCE_SHA256,
        "endpoint_id": ENDPOINT_ID,
        "acquisition_ids": [f"{ENDPOINT_ID}/0x1002"],
        "live_authorized": False,
        "collection_settings": {
            "authority": "legacy_snapshot",
            "batch_id": "one",
            "mode": "candidate_qualification",
            "period_ms": 100,
        },
    }
    plan_bytes = _write_json(directory / "admitted-plan.json", plan)
    _write_json(directory / "run-start.json", {
        "document_type": "telemetry.run-start",
        "schema_version": 1,
        "run_id": RUN_ID,
        "project_id": "test-project",
        "profile_id": "test-profile",
        "origin": "legacy_import",
        "independence": "unknown",
        "created_utc": None,
        "backend_kind": "synthetic",
        "endpoint_id": ENDPOINT_ID,
        "ecu_key": 372,
        "source_sha256": SOURCE_SHA256,
        "admitted_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "privacy": "VIN, location, adapter serial, and source paths are not included",
    })
    records = [
        {
            "record": "segment_start", "schema_version": 1, "run_id": RUN_ID,
            "segment_index": 1, "origin": "legacy_import", "source_sha256": SOURCE_SHA256,
        },
        {
            "record": "exchange", "schema_version": 1, "run_id": RUN_ID,
            "segment_index": 1, "segment_sequence": 1, "run_sequence": 1,
            "acquisition_id": f"{ENDPOINT_ID}/0x1002", "endpoint_id": ENDPOINT_ID,
            "data_id": "0x1002", "response_utc": None,
            "request_elapsed_ms": 100, "response_elapsed_ms": 110,
            "request_hex": "22 10 02", "response_hex": "62 10 02 34 12",
            "outcome": "positive", "negative_response_code": None,
            "pending_count": None, "pending_duration_ms": None, "latency_ms": 10,
            "retry_count": 0, "scheduler_reason": "legacy_import", "objective_ids": [],
            "observations": [{
                "signal_ref": "372/0x1002/0..15", "raw_hex": "34 12",
                "decoded_value": 13.33, "applicability": "unknown",
            }],
        },
        {
            "record": "segment_end", "schema_version": 1, "run_id": RUN_ID,
            "segment_index": 1, "reason": "plan_complete", "exchange_count": 1,
        },
    ]
    segment_bytes = b"".join(
        (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for record in records
    )
    (directory / "000001.segment.complete.jsonl").write_bytes(segment_bytes)
    _write_json(directory / "run-end.json", {
        "document_type": "telemetry.run-end",
        "schema_version": 1,
        "run_id": RUN_ID,
        "reason": "plan_complete",
        "segment_hashes": [hashlib.sha256(segment_bytes).hexdigest()],
        "source_sha256": SOURCE_SHA256,
    })
