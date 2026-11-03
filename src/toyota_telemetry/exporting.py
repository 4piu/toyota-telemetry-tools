"""Deterministic, generation-atomic export into a deliverable directory.

The workspace stays private. An export writes the reviewed result and exact
copies of the curated inputs that produced it; nothing else leaves.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .identity import SignalRef
from .repository import ProjectRepository


CSV_FIELDS = [
    "signal_ref", "name", "formula", "unit", "evidence_state", "observations",
    "distinct_raw_values", "decoded_minimum", "decoded_maximum", "notes",
]


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field) for field in CSV_FIELDS})
    return stream.getvalue().encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_write(path: Path, data: bytes) -> None:
    if path.is_file():
        if path.read_bytes() != data:
            raise ValueError(f"existing export generation differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_destination(repository: ProjectRepository, destination: Path) -> Path:
    """Resolve an export destination and refuse ones that would mix projects."""

    target = destination.resolve()
    if target == repository.root or target.is_relative_to(repository.root):
        raise ValueError("export to a folder outside the workspace")
    if repository.root.is_relative_to(target):
        raise ValueError("export to a folder that does not contain the workspace")
    if target.exists() and not target.is_dir():
        raise ValueError(f"{target.name} is not a folder")
    pointer_path = target / "result" / "current.json"
    if pointer_path.is_file():
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            manifest_path = target / "result" / "generations" / pointer["generation_id"] / "manifest.json"
            project_id = json.loads(manifest_path.read_text(encoding="utf-8"))["project_id"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"the existing result in {target.name} is unreadable") from exc
        if project_id != repository.project_id:
            raise ValueError(
                f"{target.name} holds results of project {project_id}, not {repository.project_id}"
            )
    return target


def export_project(
    repository: ProjectRepository,
    destination: Path,
    *,
    toolkit_version: str,
    toolkit_commit: str | None,
    generated_at_utc: str,
) -> dict[str, Any]:
    """Write one immutable generation and the curated inputs, then advance the pointer."""

    target = check_destination(repository, destination)

    variants_by_signal: dict[str, list[dict[str, Any]]] = {}
    for variant in repository.catalog_variants():
        variants_by_signal.setdefault(variant["signal_ref"], []).append(variant)
    reviews = {row["signal_ref"]: row for row in repository.review["signals"]}
    observations = repository.signal_observation_summary()
    progress_by_signal = {row["signal_ref"]: row for row in repository.replay_progress()}

    rows = []
    json_rows = []
    for signal_ref in sorted(variants_by_signal, key=SignalRef.parse):
        variants = variants_by_signal[signal_ref]
        review = reviews.get(signal_ref)
        selected = None
        if review and review["catalog_variant_id"] is not None:
            selected = next(
                row for row in variants if row["catalog_variant_id"] == review["catalog_variant_id"]
            )
        if selected is None:
            selected = min(variants, key=lambda row: row["catalog_variant_id"])
        summary = observations.get(
            signal_ref,
            {"observations": 0, "distinct_raw_values": 0, "decoded_minimum": None, "decoded_maximum": None},
        )
        name = review["display_name_override"] if review and review["display_name_override"] is not None else selected.get("name", "")
        formula = review["formula_override"] if review and review["formula_override"] is not None else selected.get("formula", "raw")
        unit = review["unit_override"] if review and review["unit_override"] is not None else selected.get("unit", "")
        flat = {
            "signal_ref": signal_ref,
            "name": name,
            "formula": formula,
            "unit": unit,
            "evidence_state": review["evidence_state"] if review else "Candidate",
            **summary,
            "notes": review["notes"] if review else "",
        }
        rows.append(flat)
        json_rows.append(
            {
                "signal_ref": signal_ref,
                "catalog": {
                    "selected_variant_id": selected["catalog_variant_id"],
                    "variant_count": len(variants),
                    "name": selected.get("name", ""),
                    "formula": selected.get("formula", "raw"),
                    "unit": selected.get("unit", ""),
                },
                "observation": summary,
                "review": review,
                "resolved": {"name": name, "formula": formula, "unit": unit},
                "target_progress": progress_by_signal.get(signal_ref),
            }
        )

    signals_json = _json_bytes(
        {
            "document_type": "telemetry.signal-result",
            "schema_version": 1,
            "project_id": repository.project_id,
            "profile_id": repository.profile_id,
            "signals": json_rows,
        }
    )
    signals_csv = _csv_bytes(rows)
    transport_json = _json_bytes(repository.transport)
    payloads = {
        "signals.csv": signals_csv,
        "signals.json": signals_json,
        "transport.json": transport_json,
    }
    identity = {
        "project_id": repository.project_id,
        "profile_id": repository.profile_id,
        "source_hashes": repository.cached_source_hashes(),
        "toolkit_version": toolkit_version,
        "toolkit_commit": toolkit_commit,
        "generated_at_utc": generated_at_utc,
        "output_hashes": {name: _sha256(data) for name, data in payloads.items()},
        "row_counts": {"signals": len(rows), "endpoints": len(repository.transport["endpoints"])},
    }
    generation_id = _sha256(_json_bytes(identity))[:20]
    manifest = {
        "document_type": "telemetry.result-manifest",
        "schema_version": 1,
        "generation_id": generation_id,
        **identity,
        "privacy": "No raw response payloads, trip metadata, adapter serials, or local paths are exported.",
    }
    payloads["manifest.json"] = _json_bytes(manifest)
    generation = target / "result" / "generations" / generation_id
    for name, data in payloads.items():
        _stable_write(generation / name, data)
    for relative, data in repository.curated_documents().items():
        _atomic_replace(target / relative, data)
    pointer = _json_bytes(
        {
            "document_type": "telemetry.result-pointer",
            "schema_version": 1,
            "generation_id": generation_id,
            "manifest_sha256": _sha256(payloads["manifest.json"]),
        }
    )
    _atomic_replace(target / "result" / "current.json", pointer)
    return manifest


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
