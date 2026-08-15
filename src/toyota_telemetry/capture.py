"""Validate and summarize generic UDS ReadDataByIdentifier capture JSONL."""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

from .project import atomic_json


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}: {exc}") from exc
            if not isinstance(record, dict) or "record" not in record:
                raise ValueError(f"line {line_number} is not a typed JSON object")
            record["_line"] = line_number
            records.append(record)
    if not records:
        raise ValueError("capture is empty")
    return records


def classify_nrc(code: int) -> str:
    return {
        0x21: "busy_retry",
        0x22: "conditions_not_correct",
        0x31: "request_out_of_range",
        0x33: "security_denied",
        0x7E: "session_restricted",
        0x7F: "session_restricted",
    }.get(code, "negative_response")


def validate_candidate_capture(
    records: list[dict[str, Any]],
    minimum_samples: int,
    maximum_dids: int = 6,
    accepted_end_reasons: frozenset[str] = frozenset({"completed"}),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    start = records[0]
    if start.get("record") != "session_start" or start.get("schema_version") != 1:
        raise ValueError("first record must be a candidate schema-v1 session_start")
    mode = start.get("mode")
    if mode not in {"candidate_qualification", "candidate_batch"}:
        raise ValueError(f"unsupported mode: {mode!r}")
    dids = start.get("dids")
    if (
        not isinstance(dids, list)
        or not 1 <= len(dids) <= maximum_dids
        or len(set(dids)) != len(dids)
    ):
        raise ValueError(f"session_start must contain 1-{maximum_dids} unique DIDs")
    samples = [record for record in records if record.get("record") == "candidate_sample"]
    for index, sample in enumerate(samples, 1):
        line = sample["_line"]
        if sample.get("sequence") != index:
            raise ValueError(f"non-contiguous sample sequence at line {line}")
        expected_did = dids[(index - 1) % len(dids)]
        did = sample.get("did")
        if did != expected_did:
            raise ValueError(
                f"schedule mismatch at line {line}: expected {expected_did}, got {did!r}"
            )
        try:
            response = bytes.fromhex(sample.get("response_hex"))
            did_number = int(did, 16)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid DID or response_hex at line {line}") from exc
        positive_prefix = bytes((0x62, did_number >> 8, did_number & 0xFF))
        supported = sample.get("supported")
        if supported is True:
            if not response.startswith(positive_prefix) or sample.get(
                "negative_response_code"
            ) is not None:
                raise ValueError(f"inconsistent positive response at line {line}")
            if sample.get("qualification_status", "supported") != "supported":
                raise ValueError(f"inconsistent qualification status at line {line}")
        elif supported is False:
            if mode != "candidate_qualification":
                raise ValueError(f"negative response in road batch at line {line}")
            if len(response) < 3 or response[:2] != b"\x7f\x22":
                raise ValueError(f"invalid UDS negative response at line {line}")
            if sample.get("negative_response_code") != f"0x{response[2]:02X}":
                raise ValueError(f"negative-response code mismatch at line {line}")
            expected_status = classify_nrc(response[2])
            if sample.get("qualification_status", expected_status) != expected_status:
                raise ValueError(f"negative-response classification mismatch at line {line}")
        else:
            raise ValueError(f"supported must be boolean at line {line}")
        timing = (
            sample.get("request_elapsed_ms"),
            sample.get("response_elapsed_ms"),
            sample.get("latency_ms"),
        )
        if not all(isinstance(value, int) and value >= 0 for value in timing):
            raise ValueError(f"invalid timing at line {line}")
        if timing[1] < timing[0]:
            raise ValueError(f"response precedes request at line {line}")
        retry_count = sample.get("retry_count", 0)
        if (
            not isinstance(retry_count, int)
            or isinstance(retry_count, bool)
            or retry_count < 0
        ):
            raise ValueError(f"invalid retry_count at line {line}")

    ends = [record for record in records if record.get("record") == "session_end"]
    if len(ends) != 1 or records[-1].get("record") != "session_end":
        raise ValueError("capture is incomplete: exactly one final session_end is required")
    end = ends[0]
    if end.get("reason") not in accepted_end_reasons:
        raise ValueError(f"capture is incomplete: session_end reason is {end.get('reason')!r}")
    if any(record.get("record") == "error" for record in records):
        raise ValueError("capture contains an error record")
    counts = Counter(sample["did"] for sample in samples)
    if end.get("sample_count") != len(samples) or end.get("did_counts") != dict(counts):
        raise ValueError("session_end sample counts do not match parsed records")
    if mode == "candidate_qualification":
        if len(samples) != len(dids) or any(counts[did] != 1 for did in dids):
            raise ValueError("qualification must contain exactly one response for every DID")
    elif any(counts[did] < minimum_samples for did in dids):
        shortage = {did: counts[did] for did in dids if counts[did] < minimum_samples}
        raise ValueError(
            f"batch did not reach the minimum {minimum_samples} samples per DID: {shortage}"
        )
    return start, samples


def validate_capture_context(
    start: dict[str, Any], profile_id: str, endpoint: dict[str, Any]
) -> None:
    if start.get("profile_id") != profile_id:
        raise ValueError(
            f"capture profile {start.get('profile_id')!r} does not match {profile_id!r}"
        )
    transport = start.get("transport")
    if not isinstance(transport, dict):
        raise ValueError("session_start has no transport object")
    expected = {
        "bitrate": int(endpoint["bitrate"]),
        "tester_id": str(endpoint["tester_can_id"]),
        "response_id": str(endpoint["response_can_id"]),
    }
    actual = {
        "bitrate": transport.get("bitrate"),
        "tester_id": transport.get("tester_id"),
        "response_id": transport.get("response_id"),
    }
    if actual != expected:
        raise ValueError(f"capture transport {actual!r} does not match endpoint {expected!r}")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return (
        ordered[low]
        if low == high
        else ordered[low] * (high - position) + ordered[high] * (position - low)
    )


def extract_raw(data: bytes, first_bit: int, last_bit: int) -> int:
    if first_bit < 0 or last_bit < first_bit or last_bit >= len(data) * 8:
        raise ValueError("catalog bit range exceeds the response data")
    width = last_bit - first_bit + 1
    shift = len(data) * 8 - 1 - last_bit
    return (int.from_bytes(data, "big") >> shift) & ((1 << width) - 1)


def evaluate_formula(expression: str, raw: int) -> float:
    tree = ast.parse(expression, mode="eval")

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name) and node.id == "raw":
            return float(raw)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "signed"
            and len(node.args) == 2
            and not node.keywords
        ):
            value, width_value = visit(node.args[0]), visit(node.args[1])
            width = int(width_value)
            if width_value != width or width < 1 or width > 64:
                raise ValueError("signed() width must be an integer from 1 through 64")
            unsigned = int(value)
            if value != unsigned or unsigned < 0 or unsigned >= (1 << width):
                raise ValueError("signed() value does not fit the requested width")
            return float(
                unsigned - (1 << width)
                if unsigned & (1 << (width - 1))
                else unsigned
            )
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
        ):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            return left**right
        raise ValueError(f"unsupported formula syntax: {expression}")

    return visit(tree)


def load_decode_overrides(
    path: Path | None, ecu_key: int
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if document.get("schema_version") != 1:
        raise ValueError("only decode-override schema 1 is supported")
    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for override in document.get("signals", []):
        if int(override.get("ecu_key", -1)) != ecu_key:
            continue
        key = (
            f"0x{int(override['data_id'], 16):04X}",
            str(override["name"]),
            str(override["first_bit"]),
            str(override["last_bit"]),
        )
        if key in result:
            raise ValueError(f"duplicate decode override: {key}")
        result[key] = override
    return result


def load_catalog(
    path: Path | None,
    dids: set[str],
    ecu_key: int,
    decode_overrides: Path | None = None,
) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    if path is None or not path.is_file():
        return result
    normalized = {f"0x{int(did, 16):04X}" for did in dids}
    overrides = load_decode_overrides(decode_overrides, ecu_key)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for source_row in csv.DictReader(handle):
            try:
                did = f"0x{int(source_row.get('data_id', ''), 16):04X}"
            except ValueError:
                continue
            if (
                source_row.get("ecu_key") == str(ecu_key)
                and did in normalized
                and source_row.get("first_bit")
                and source_row.get("last_bit")
            ):
                row = dict(source_row)
                key = (did, row.get("name", ""), row["first_bit"], row["last_bit"])
                override = overrides.get(key)
                if override:
                    row["formula"] = str(override["formula"])
                    row["unit"] = str(override.get("unit", row.get("unit", "")))
                    row["formula_source"] = "reviewed_override"
                else:
                    row["formula_source"] = "catalog"
                result[did].append(row)
    return result


def summarize_candidate_capture(
    start: dict[str, Any],
    samples: list[dict[str, Any]],
    catalog_path: Path | None,
    ecu_key: int,
    decode_overrides: Path | None = None,
) -> dict[str, Any]:
    dids = start["dids"]
    supported = sorted({sample["did"] for sample in samples if sample["supported"]})
    negatives = {
        sample["did"]: {
            "nrc": sample["negative_response_code"],
            "classification": sample.get(
                "qualification_status",
                classify_nrc(int(sample["negative_response_code"], 16)),
            ),
        }
        for sample in samples
        if not sample["supported"]
    }
    counts = Counter(sample["did"] for sample in samples)
    latency: dict[str, list[float]] = defaultdict(list)
    data_by_did: dict[str, list[bytes]] = defaultdict(list)
    retry_counts: Counter[str] = Counter()
    for sample in samples:
        latency[sample["did"]].append(float(sample["latency_ms"]))
        if sample["supported"]:
            data_by_did[sample["did"]].append(bytes.fromhex(sample["response_hex"])[3:])
        retry_counts[sample["did"]] += int(sample.get("retry_count", 0))
    catalog = load_catalog(catalog_path, set(supported), ecu_key, decode_overrides)
    signals: list[dict[str, Any]] = []
    for did in supported:
        for row in catalog.get(did, []):
            try:
                raw_values = [
                    extract_raw(data, int(row["first_bit"]), int(row["last_bit"]))
                    for data in data_by_did[did]
                ]
            except ValueError:
                continue
            formula = row.get("formula") or "raw"
            try:
                values = [evaluate_formula(formula, raw) for raw in raw_values]
            except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
                values = []
            signals.append(
                {
                    "did": did,
                    "name": row.get("name"),
                    "bits": f"{row['first_bit']}..{row['last_bit']}",
                    "formula": formula,
                    "formula_source": row.get("formula_source", "catalog"),
                    "unit": row.get("unit"),
                    "sample_count": len(raw_values),
                    "raw_min": min(raw_values),
                    "raw_max": max(raw_values),
                    "unique_raw_values": len(set(raw_values)),
                    "value_min": min(values) if values else None,
                    "value_max": max(values) if values else None,
                    "evidence_hint": (
                        "varied" if len(set(raw_values)) > 1 else "constant_in_this_run"
                    ),
                }
            )
    return {
        "schema_version": 1,
        "profile_id": start.get("profile_id"),
        "source_mode": start["mode"],
        "batch_id": start.get("batch_id"),
        "complete": True,
        "dids": dids,
        "supported_dids": supported,
        "negative_dids": negatives,
        "sample_counts": dict(counts),
        "latency_ms": {
            did: {
                "min": min(values),
                "median": statistics.median(values),
                "p95": percentile(values, 0.95),
                "max": max(values),
            }
            for did, values in latency.items()
        },
        "transport_retries": {
            "total": sum(retry_counts.values()),
            "samples_with_retry": sum(
                int(sample.get("retry_count", 0)) > 0 for sample in samples
            ),
            "maximum_for_one_sample": max(
                (int(sample.get("retry_count", 0)) for sample in samples), default=0
            ),
            "by_did": {
                did: count for did, count in sorted(retry_counts.items()) if count
            },
        },
        "catalog_signal_observations": signals,
        "catalog_available": bool(catalog_path and catalog_path.is_file()),
        "confidence_policy": (
            "Evidence hints are review aids only; this tool never changes the validated signal map."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--ecu-key", type=int, required=True)
    parser.add_argument("--decode-overrides", type=Path)
    parser.add_argument("--minimum-samples", type=int, default=0)
    parser.add_argument("--maximum-dids", type=int, default=6)
    parser.add_argument("--summary-json", type=Path)
    args = parser.parse_args()
    try:
        start, samples = validate_candidate_capture(
            read_jsonl(args.capture), args.minimum_samples, args.maximum_dids
        )
        report = summarize_candidate_capture(
            start, samples, args.catalog, args.ecu_key, args.decode_overrides
        )
        if args.summary_json:
            atomic_json(args.summary_json, report, replace=False)
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
