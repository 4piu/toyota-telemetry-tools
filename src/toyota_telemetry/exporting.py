"""Deterministic, generation-atomic export of the result into a folder.

The workspace stays private. An export writes the reviewed signal list and
the transport it is read with; nothing else leaves.
"""

from __future__ import annotations

import csv
from fractions import Fraction
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .identity import SignalRef
from .repository import ProjectRepository
from .states import state_text


CSV_FIELDS = [
    "signal_ref", "ecu", "name", "formula", "unit", "states", "evidence_state", "recorded",
    "observations", "distinct_raw_values", "notes",
]
# How a signal appears in the recordings. Exports carry counts, never values:
# recorded values include readings such as the odometer.
CHANGED = "changed"
CONSTANT = "constant"
BEYOND_RESPONSE = "beyond response"
NOT_RECORDED = "not recorded"
RECORDED_MEANING = {
    CHANGED: "took more than one value, so the formula was exercised",
    CONSTANT: "answered but never changed; the DID is readable, the formula unchecked",
    BEYOND_RESPONSE: "lies past the end of this vehicle's response; not available here",
    NOT_RECORDED: "never recorded",
}
_SCALED = re.compile(r"\(\(raw \* (-?\d+) / (-?\d+)\) \+ (-?\d+)\) / \(10 \*\* (\d+)\)")


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
    """Write one immutable result generation, then advance the pointer."""

    target = check_destination(repository, destination)

    variants_by_signal: dict[str, list[dict[str, Any]]] = {}
    for variant in repository.catalog_variants():
        variants_by_signal.setdefault(variant["signal_ref"], []).append(variant)
    reviews = {row["signal_ref"]: row for row in repository.review["signals"]}
    observations = repository.signal_observation_summary()
    response_bits = repository.response_data_bits()
    endpoint_by_ecu = {
        int(ecu): endpoint for endpoint in repository.transport["endpoints"] for ecu in endpoint["ecu_keys"]
    }
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
        recorded = observations.get(signal_ref)
        ref = SignalRef.parse(signal_ref)
        endpoint = endpoint_by_ecu.get(ref.ecu_key)
        data_bits = response_bits.get((endpoint["id"], ref.data_id)) if endpoint else None
        summary = {
            "observations": recorded["observations"] if recorded else 0,
            "distinct_raw_values": recorded["distinct_raw_values"] if recorded else 0,
            "recorded": _recorded(recorded, data_bits, ref.last_bit),
        }
        ecu = selected.get("ecu_name") or f"ECU {ref.ecu_key}"
        name = review["display_name_override"] if review and review["display_name_override"] is not None else selected.get("name", "")
        formula = review["formula_override"] if review and review["formula_override"] is not None else selected.get("formula", "raw")
        unit = review["unit_override"] if review and review["unit_override"] is not None else selected.get("unit", "")
        states = selected.get("states") or {}
        flat = {
            "signal_ref": signal_ref,
            "ecu": ecu,
            "name": name,
            "formula": formula,
            "unit": unit,
            "states": state_text(states),
            "evidence_state": review["evidence_state"] if review else "Candidate",
            **summary,
            "notes": review["notes"] if review else "",
        }
        rows.append(flat)
        json_rows.append(
            {
                "signal_ref": signal_ref,
                "ecu": ecu,
                "catalog": {
                    "selected_variant_id": selected["catalog_variant_id"],
                    "variant_count": len(variants),
                    "name": selected.get("name", ""),
                    "formula": selected.get("formula", "raw"),
                    "unit": selected.get("unit", ""),
                    "states": states,
                },
                "observation": summary,
                "review": review,
                "resolved": {"name": name, "formula": formula, "unit": unit, "states": states},
                "target_progress": progress_by_signal.get(signal_ref),
            }
        )

    signals_json = _json_bytes(
        {
            "document_type": "telemetry.signal-result",
            "schema_version": 2,
            "project_id": repository.project_id,
            "profile_id": repository.profile_id,
            "recorded_meaning": RECORDED_MEANING,
            "signals": json_rows,
        }
    )
    signals_csv = _csv_bytes(rows)
    transport_json = _json_bytes(repository.transport)
    payloads = {
        "signals.csv": signals_csv,
        "signals.json": signals_json,
        "signals.md": _reference_bytes(repository, rows, endpoint_by_ecu),
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
        "privacy": (
            "No recorded values, raw response payloads, trip metadata, adapter serials, "
            "or local paths are exported; recordings appear only as counts."
        ),
    }
    payloads["manifest.json"] = _json_bytes(manifest)
    generation = target / "result" / "generations" / generation_id
    for name, data in payloads.items():
        _stable_write(generation / name, data)
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


def _recorded(summary: dict[str, Any] | None, data_bits: int | None, last_bit: int) -> str:
    if summary and summary["observations"]:
        return CHANGED if summary["distinct_raw_values"] > 1 else CONSTANT
    if data_bits is not None and last_bit >= data_bits:
        return BEYOND_RESPONSE
    return NOT_RECORDED


def readable_formula(formula: str) -> str:
    """The stored ((raw * A / B) + C) / (10 ** N) form as scale and offset."""

    match = _SCALED.fullmatch(formula.strip())
    if match is None:
        return formula
    a, b, c, n = (int(value) for value in match.groups())
    scale = Fraction(a, b) / 10 ** n
    offset = Fraction(c) / 10 ** n
    text = "raw" if scale == 1 else f"raw × {_number(scale)}"
    if offset:
        text += f" {'-' if offset < 0 else '+'} {_number(abs(offset))}"
    return text


def _number(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else format(float(value), ".10g")


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _reference_bytes(
    repository: ProjectRepository, rows: list[dict[str, Any]], endpoint_by_ecu: dict[int, dict[str, Any]],
) -> bytes:
    """A readable signal reference: one table per ECU, from the same rows as the CSV."""

    by_ecu: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_ecu.setdefault(SignalRef.parse(row["signal_ref"]).ecu_key, []).append(row)
    counts = {key: sum(1 for row in rows if row["recorded"] == key) for key in RECORDED_MEANING}
    lines = [
        f"# Signal reference: {repository.profile_id}",
        "",
        "Generated by `telemetry export`; do not edit. Each signal is read with UDS",
        "`22 DID` on its ECU's endpoint. Bit 0 is the most significant bit of the",
        "first data byte after the `62 DID` echo; the bits form an unsigned",
        "big-endian raw value. Formulas come from the manufacturer database unless",
        "the review overrides them. States give the text Techstream shows for",
        "the listed raw values; other raw values are numbers.",
        "",
        "Recorded, over every recording of this vehicle:",
        "",
    ]
    lines += [f"- **{key}** ({counts[key]}): {meaning}" for key, meaning in RECORDED_MEANING.items()]
    for ecu_key in sorted(by_ecu):
        ecu_rows = by_ecu[ecu_key]
        endpoint = endpoint_by_ecu.get(ecu_key)
        address = (
            f", request {endpoint['tester_can_id']}, response {endpoint['response_can_id']}" if endpoint else ""
        )
        lines += [
            "",
            f"## {ecu_rows[0]['ecu']} (ECU {ecu_key}{address})",
            "",
            "| DID | Bits | Name | Formula | Unit | States | Evidence | Recorded | Notes |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for row in ecu_rows:
            ref = SignalRef.parse(row["signal_ref"])
            lines.append("| " + " | ".join(_cell(value) for value in (
                f"0x{ref.data_id:04X}", f"{ref.first_bit}..{ref.last_bit}", row["name"],
                readable_formula(row["formula"]), row["unit"], row["states"], row["evidence_state"], row["recorded"],
                row["notes"],
            )) + " |")
    return ("\n".join(lines) + "\n").encode("utf-8")


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
