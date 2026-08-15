"""Load and track explicit, resumable telemetry experiments."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Any

from .capture import read_jsonl


ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
DID_PATTERN = re.compile(r"0x[0-9A-F]{4}\Z")
SCHEDULE_FIELDS = {
    "period_ms",
    "duration_seconds",
    "minimum_samples_per_did",
    "transport_retries",
}


@dataclass(frozen=True)
class ExperimentRun:
    experiment_id: str
    batch_id: str
    repetition: int
    dids: tuple[str, ...]
    period_ms: int
    duration_seconds: int
    minimum_samples_per_did: int
    transport_retries: int
    description: str
    vehicle_state: str
    digest: str

    @property
    def backend_batch_id(self) -> str:
        return (
            f"experiment-{self.experiment_id}-{self.batch_id}-"
            f"r{self.repetition:02d}-{self.digest}"
        )


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be {minimum}..{maximum}")
    return value


def _text(value: Any, name: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def load_experiment(
    path: Path,
    profile_id: str,
    plan: dict[str, Any],
    qualified_dids: set[str],
) -> tuple[dict[str, Any], list[ExperimentRun]]:
    """Validate a TOML experiment and expand its configured repetitions."""
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    allowed_top = {
        "schema_version", "experiment_id", "profile_id", "description", "defaults", "batches"
    }
    unknown_top = set(document) - allowed_top
    if unknown_top:
        raise ValueError(f"unknown experiment fields: {sorted(unknown_top)}")
    if document.get("schema_version") != 1:
        raise ValueError("only experiment schema 1 is supported")
    experiment_id = _text(document.get("experiment_id"), "experiment_id", required=True)
    if not ID_PATTERN.fullmatch(experiment_id):
        raise ValueError("experiment_id must use lowercase letters, digits, and hyphens")
    if document.get("profile_id") != profile_id:
        raise ValueError("experiment profile_id does not match validation profile")
    defaults = document.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("experiment defaults must be a table")
    unknown_defaults = set(defaults) - SCHEDULE_FIELDS
    if unknown_defaults:
        raise ValueError(f"unknown experiment default fields: {sorted(unknown_defaults)}")
    batches = document.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ValueError("experiment must contain at least one [[batches]] table")
    if len(batches) > 100:
        raise ValueError("experiment cannot contain more than 100 batches")

    candidates = {item["data_id"] for item in plan["candidates"]}
    maximum_dids = int(plan["limits"]["maximum_dids_per_road_batch"])
    seen_batches: set[str] = set()
    runs: list[ExperimentRun] = []
    normalized_batches: list[dict[str, Any]] = []
    allowed_batch = {
        "batch_id", "dids", "description", "vehicle_state", "repetitions", *SCHEDULE_FIELDS
    }
    for index, batch in enumerate(batches, 1):
        if not isinstance(batch, dict):
            raise ValueError(f"batches[{index}] must be a table")
        unknown_batch = set(batch) - allowed_batch
        if unknown_batch:
            raise ValueError(f"unknown fields in batches[{index}]: {sorted(unknown_batch)}")
        batch_id = _text(batch.get("batch_id"), f"batches[{index}].batch_id", required=True)
        if not ID_PATTERN.fullmatch(batch_id):
            raise ValueError(f"invalid batch_id {batch_id!r}")
        if batch_id in seen_batches:
            raise ValueError(f"duplicate experiment batch_id {batch_id!r}")
        seen_batches.add(batch_id)
        dids_value = batch.get("dids")
        if not isinstance(dids_value, list) or not dids_value:
            raise ValueError(f"batch {batch_id!r} must contain a DID array")
        if len(dids_value) > maximum_dids:
            raise ValueError(
                f"batch {batch_id!r} has {len(dids_value)} DIDs; platform maximum is {maximum_dids}"
            )
        dids: list[str] = []
        for did in dids_value:
            if not isinstance(did, str) or not DID_PATTERN.fullmatch(did):
                raise ValueError(f"batch {batch_id!r} has invalid DID {did!r}")
            if did in dids:
                raise ValueError(f"batch {batch_id!r} repeats DID {did}")
            if did not in candidates:
                raise ValueError(f"batch {batch_id!r} DID {did} is outside the reviewed plan")
            if did not in qualified_dids:
                raise ValueError(f"batch {batch_id!r} DID {did} has no positive qualification")
            dids.append(did)

        def schedule(name: str, minimum: int, maximum: int) -> int:
            value = batch.get(name, defaults.get(name))
            if value is None:
                raise ValueError(f"batch {batch_id!r} has no {name}")
            return _integer(value, f"batch {batch_id!r} {name}", minimum, maximum)

        period_ms = schedule("period_ms", 70, 5000)
        duration_seconds = schedule("duration_seconds", 0, 7200)
        minimum_samples = schedule("minimum_samples_per_did", 1, 1_000_000)
        transport_retries = schedule("transport_retries", 0, 5)
        theoretical_samples = (
            duration_seconds * 1000 // (period_ms * len(dids))
            if duration_seconds
            else None
        )
        if theoretical_samples is not None and minimum_samples > theoretical_samples:
            raise ValueError(
                f"batch {batch_id!r} requires {minimum_samples} samples per DID but its "
                f"ideal maximum is {theoretical_samples}"
            )
        repetitions = _integer(
            batch.get("repetitions", 1), f"batch {batch_id!r} repetitions", 1, 20
        )
        description = _text(batch.get("description"), f"batch {batch_id!r} description")
        vehicle_state = _text(batch.get("vehicle_state"), f"batch {batch_id!r} vehicle_state")
        normalized = {
            "batch_id": batch_id,
            "dids": dids,
            "period_ms": period_ms,
            "duration_seconds": duration_seconds,
            "minimum_samples_per_did": minimum_samples,
            "transport_retries": transport_retries,
            "repetitions": repetitions,
            "description": description,
            "vehicle_state": vehicle_state,
        }
        normalized_batches.append(normalized)
        for repetition in range(1, repetitions + 1):
            signature = {
                "schema_version": 1,
                "experiment_id": experiment_id,
                "profile_id": profile_id,
                "batch": normalized,
                "repetition": repetition,
            }
            digest = hashlib.sha256(
                json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:10]
            runs.append(
                ExperimentRun(
                    experiment_id,
                    batch_id,
                    repetition,
                    tuple(dids),
                    period_ms,
                    duration_seconds,
                    minimum_samples,
                    transport_retries,
                    description,
                    vehicle_state,
                    digest,
                )
            )
    normalized_document = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "profile_id": profile_id,
        "description": _text(document.get("description"), "description"),
        "batches": normalized_batches,
    }
    normalized_document["sha256"] = hashlib.sha256(
        json.dumps(normalized_document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return normalized_document, runs


def discover_completed_experiment_runs(
    output_root: Path, profile_id: str, expected_ids: set[str]
) -> tuple[set[str], list[str]]:
    completed: set[str] = set()
    corrupt: list[str] = []
    if not output_root.exists():
        return completed, corrupt
    for path in output_root.rglob("*.complete.jsonl"):
        try:
            records = read_jsonl(path)
            if not records:
                raise ValueError("empty capture")
            start, end = records[0], records[-1]
            batch_id = start.get("batch_id")
            if batch_id not in expected_ids or start.get("profile_id") != profile_id:
                continue
            if end.get("record") != "session_end" or end.get("reason") not in {
                "completed",
                "user_stop",
            }:
                raise ValueError("invalid completion marker")
            completed.add(batch_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            corrupt.append(str(path))
    return completed, corrupt


def experiment_snapshot(
    document: dict[str, Any], runs: list[ExperimentRun], completed: set[str], corrupt: list[str]
) -> dict[str, Any]:
    return {
        **document,
        "runs": [
            {**asdict(run), "dids": list(run.dids), "complete": run.backend_batch_id in completed}
            for run in runs
        ],
        "status": {
            "configured": len(runs),
            "complete": sum(run.backend_batch_id in completed for run in runs),
            "pending": sum(run.backend_batch_id not in completed for run in runs),
            "corrupt_complete_files": corrupt,
        },
    }
