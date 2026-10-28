"""A schema-2 project with one confirmed and one unconfirmed candidate ECU."""

from __future__ import annotations

import json
from pathlib import Path

from tests.project_fixture import ENDPOINT_ID, make_current_project


UNCONFIRMED_KEY = "7a1-7a9"
CANDIDATE_ID = "7a1-7a9-session-a1769d23a560"


def candidate_rows() -> list[dict]:
    return [
        {
            "formula": "raw / 1000",
            "internal_id": "0x0001",
            "name": "Battery voltage",
            "signal_ref": "372/0x1002/0..15",
            "unit": "V",
        },
        {
            "formula": "raw",
            "internal_id": "0x0002",
            "name": "Gear state",
            "signal_ref": "372/0x1003/0..7",
            "unit": "",
        },
        {
            "formula": "raw - 40",
            "internal_id": "0x0003",
            "name": "Coolant temperature",
            "signal_ref": "372/0x1003/8..15",
            "unit": "C",
        },
        {
            "ecu_key": 405,
            "data_id": "0x2001",
            "first_bit": 0,
            "last_bit": 7,
            "name": "Brake switch",
            "unit": "",
            "formula": "raw",
            "ecu_name": "Brake",
            "tester_can_id": "0x7A1",
            "response_can_id": "0x7A9",
        },
        {"ecu_key": 405, "data_id": "0xF100", "first_bit": 0, "last_bit": 7, "name": "Outside range"},
        {"name": "No address"},
    ]


def make_operator_project(root: Path, *, include_capture: bool = True) -> Path:
    make_current_project(root, include_capture=include_capture)
    rows = root / "scratch" / "candidates.json"
    rows.write_text(json.dumps(candidate_rows(), indent=2) + "\n", encoding="utf-8")
    worker = root / "scratch" / "bin" / "telemetry-pcan.exe"
    worker.parent.mkdir(parents=True, exist_ok=True)
    worker.write_bytes(b"")
    return rows


def probe_result(*, responded: bool = True, read_outcome: str | None = None,
                 response_hex: str = "62 10 02 12 34") -> dict:
    result = {
        "event": "probe_result",
        "schema_version": 1,
        "candidate_id": "replaced",
        "backend_kind": "replaced",
        "responded": responded,
        "session_steps": [{
            "label": "extended",
            "outcome": "positive" if responded else "timeout",
            "latency_ms": 4,
            "nrc": None,
            "response_hex": "50 03" if responded else None,
        }],
        "read": None,
    }
    if read_outcome is not None:
        result["read"] = {
            "attempted": True,
            "outcome": read_outcome,
            "latency_ms": 9,
            "nrc": "0x31" if read_outcome == "negative" else None,
            "response_hex": response_hex if read_outcome == "positive" else None,
        }
    return result


class ScriptedProbes:
    """A probe factory that answers from a script keyed by request kind."""

    def __init__(self, *, endpoint_responds: bool = True, did_outcome: str = "positive") -> None:
        self.endpoint_responds = endpoint_responds
        self.did_outcome = did_outcome
        self.configs: list[dict] = []

    def __call__(self, config_path: Path):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.configs.append(config)
        if "preflight_read" in config:
            did = config["preflight_read"]["request_hex"][3:]
            result = probe_result(read_outcome=self.did_outcome, response_hex=f"62 {did} 12 34")
        else:
            result = probe_result(responded=self.endpoint_responds)
        result["candidate_id"] = config["candidate_id"]
        result["backend_kind"] = next(iter(config["device"]))
        return _Worker(result)

    @property
    def transmissions(self) -> int:
        return len(self.configs)


class _Worker:
    def __init__(self, result: dict) -> None:
        self.result = result

    def probe(self) -> dict:
        return self.result


__all__ = [
    "CANDIDATE_ID",
    "ENDPOINT_ID",
    "ScriptedProbes",
    "UNCONFIRMED_KEY",
    "make_operator_project",
]
