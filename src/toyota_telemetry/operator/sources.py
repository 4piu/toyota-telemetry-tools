"""Candidate source adapters that normalize external inputs into catalog rows.

Every adapter returns the same ``SourceRows``: a stable source identity, a
privacy-minimized label (never a local path), and rows that each carry a
canonical ``signal_ref``. Rows that cannot be addressed as a direct DID are
returned as rejections rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable

from ..identity import SignalRef, canonical_target_address, stable_slug
from ..project import WORKERS


# Techstream's database DLL is 32-bit, so a bundled 32-bit helper reads it.
TISDB = "telemetry-tisdb.exe"
_PHASE = re.compile(r"_P(\d)\.ddb$", re.IGNORECASE)
_HEX = re.compile(r"(?:0x)?([0-9A-Fa-f]+)")
_TIS_ROW_FIELDS = (
    "name", "unit", "formula", "formula_state", "value_type", "internal_id",
    "ecu_name", "tester_can_id", "response_can_id", "target_address", "states",
)


@dataclass(frozen=True)
class SourceRows:
    source_id: str
    kind: str
    label: str
    rows: tuple[dict[str, Any], ...]
    rejections: tuple[tuple[str, str], ...] = field(default=())


def canonical_can_id(value: Any) -> str | None:
    """Return ``0xNNN`` for an 11-bit identifier, or None when unusable."""

    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str) and (match := _HEX.fullmatch(value.strip())):
        number = int(match.group(1), 16)
    else:
        return None
    if not 0 < number <= 0x7FF:
        return None
    return f"0x{number:03X}"


def normalize_row(raw: Any) -> dict[str, Any]:
    """Return one catalog row with a canonical signal_ref, or raise ValueError."""

    if not isinstance(raw, dict):
        raise ValueError("row is not an object")
    row = dict(raw)
    if "signal_ref" in row:
        row["signal_ref"] = str(SignalRef.parse(str(row["signal_ref"])))
    else:
        try:
            ecu_key = int(row["ecu_key"])
            data_id = str(row["data_id"])
            first_bit = int(row["first_bit"])
            last_bit = int(row["last_bit"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("row needs signal_ref or ecu_key/data_id/first_bit/last_bit") from exc
        match = _HEX.fullmatch(data_id.strip())
        if match is None or int(match.group(1), 16) > 0xFFFF:
            raise ValueError(f"data_id is not a DID: {data_id!r}")
        row["signal_ref"] = str(
            SignalRef.parse(f"{ecu_key}/0x{int(match.group(1), 16):04X}/{first_bit}..{last_bit}")
        )
        for name in ("tester_can_id", "response_can_id"):
            if name in row:
                row[name] = canonical_can_id(row[name])
    if "target_address" in row:
        # Extended addressing: the ECU's address byte behind a shared gateway ID.
        address = canonical_target_address(row["target_address"])
        if address is None:
            del row["target_address"]
        else:
            row["target_address"] = address
    return row


class NormalizedFileSource:
    """A JSON array of rows, or an object holding a ``rows``/``signals`` array."""

    kind = "file"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def source_id(self) -> str:
        stem = re.sub(r"[^a-z0-9]+", "-", self.path.stem.lower()).strip("-") or "rows"
        return stable_slug(f"file-{stem}", "source id")

    def availability(self) -> str | None:
        if not self.path.is_file():
            return f"file not found: {self.path.name}"
        return None

    def read(self) -> SourceRows:
        problem = self.availability()
        if problem:
            raise FileNotFoundError(problem)
        try:
            document = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{self.path.name}: invalid JSON: {exc}") from exc
        if isinstance(document, dict):
            document = document.get("rows", document.get("signals"))
        if not isinstance(document, list):
            raise ValueError(f"{self.path.name}: expected a JSON array of rows")
        rows, rejections = _normalize_all(document)
        return SourceRows(self.source_id, self.kind, self.path.name, rows, rejections)


class TisDatabaseSource:
    """Installed Techstream Phase-5 databases read through the bundled telemetry-tisdb helper.

    Selection narrows the installed ECU catalog by database phase and ECU key.
    Database records are candidates; only verification establishes support.
    """

    kind = "tis"
    source_id = "tis"

    def __init__(
        self,
        database_directory: Path,
        *,
        phases: Iterable[int] = (5,),
        ecu_keys: Iterable[int] = (),
        techstream_bin_directory: Path | None = None,
        text_code_page: int = 932,
    ) -> None:
        self.database_directory = Path(database_directory)
        self.phases = tuple(sorted(set(phases)))
        self.ecu_keys = tuple(sorted(set(ecu_keys)))
        self.techstream_bin_directory = techstream_bin_directory
        self.text_code_page = text_code_page

    def availability(self) -> str | None:
        if os.name != "nt":
            return "the TIS database reader needs Windows (32-bit Techstream libraries)"
        if not (WORKERS / TISDB).is_file():
            return f"{TISDB} is missing from this installation; build it with backends\\build.ps1"
        if not (self.database_directory / "DB").is_dir():
            return f"no DB folder under the database directory {self.database_directory.name}"
        return None

    def read(self) -> SourceRows:
        problem = self.availability()
        if problem:
            raise FileNotFoundError(problem)
        catalog = [
            row for row in self._query(["--catalog"])
            if row.get("Ecu") and row.get("File") and row.get("Key") is not None
        ]
        installed = {path.name.lower() for path in (self.database_directory / "DB").glob("*.ddb")}
        selected: dict[int, dict[str, Any]] = {}
        for record in catalog:
            key = int(record["Key"])
            name = str(record["File"])
            if name.lower() not in installed:
                continue
            if self.ecu_keys and key not in self.ecu_keys:
                continue
            match = _PHASE.search(name)
            if self.phases and (match is None or int(match.group(1)) not in self.phases):
                continue
            selected.setdefault(key, {"key": key, "name": record["Ecu"]})
        if not selected:
            raise ValueError("no installed TIS database matches the phase/ECU selection")
        raw_rows: list[dict[str, Any]] = []
        for key in sorted(selected):
            raw_rows.extend(self._ecu_rows(selected[key]))
        rows, rejections = _normalize_all(raw_rows)
        phases = ",".join(str(value) for value in self.phases) or "any"
        label = f"TIS databases (phase {phases}, {len(selected)} ECUs)"
        return SourceRows(self.source_id, self.kind, label, rows, rejections)

    def _ecu_rows(self, ecu: dict[str, Any]) -> list[dict[str, Any]]:
        key = ecu["key"]
        monitors = [
            row for row in self._query(["--keys", str(key), "--monitors"])
            if row.get("Status") == "0x00000000" and row.get("Name")
        ]
        protocol = next(
            (
                row for row in self._query(["--keys", str(key), "--protocol"])
                if row.get("Status") == "0x00000000" and int(row.get("Count", 0) or 0) > 0
            ),
            {},
        )
        groups = sorted({str(row["PhysicalGroup"]) for row in monitors if row.get("PhysicalGroup")})
        physical: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(groups):
            for row in self._query(["--keys", str(key), "--physical", ",".join(chunk)]):
                if row.get("Status") == "0x00000000" and int(row.get("Count", 0) or 0) > 0:
                    physical[str(row["PhysicalGroup"])] = row
        unit_ids = sorted({str(row["UnitId"]) for row in physical.values() if row.get("UnitId") is not None})
        units: dict[str, str] = {}
        for chunk in _chunks(unit_ids):
            for row in self._query(["--keys", str(key), "--units", ",".join(chunk)]):
                if row.get("Status") == "0x00000000" and int(row.get("Count", 0) or 0) > 0:
                    units[str(row["UnitId"])] = row.get("Unit") or ""
        state_sets = sorted({int(row.get("StateSet") or 0) for row in monitors} - {0})
        states: dict[int, dict[str, str]] = {}
        for chunk in _chunks([str(value) for value in state_sets]):
            for row in self._query(["--keys", str(key), "--states", ",".join(chunk)]):
                if row.get("Status") == "0x00000000" and row.get("Text") is not None:
                    states.setdefault(int(row["StateSet"]), {})[str(row["Value"])] = row["Text"]
        rows = []
        for monitor in monitors:
            group = physical.get(str(monitor.get("PhysicalGroup") or ""))
            formula, formula_state = _formula_for(group)
            row: dict[str, Any] = {
                "ecu_key": key,
                "data_id": monitor["DataId"],
                "first_bit": int(monitor["FirstBit"]),
                "last_bit": int(monitor["LastBit"]),
                "name": monitor["Name"],
                "unit": units.get(str(group.get("UnitId") or ""), "") if group else "",
                "formula": formula,
                "formula_state": formula_state,
                "value_type": group.get("ValueType", "") if group else "",
                "internal_id": monitor["InternalId"],
                "ecu_name": ecu["name"],
                "tester_can_id": protocol.get("PhysicalRequestCanId"),
                "response_can_id": protocol.get("ResponseCanIdCandidate"),
                "target_address": protocol.get("TargetAddress"),
            }
            # Text Techstream shows for listed raw values; other values display as numbers.
            if states.get(int(monitor.get("StateSet") or 0)):
                row["states"] = states[int(monitor["StateSet"])]
            if monitor.get("Acquisition") != "direct DID":
                row["rejected"] = "mapped or dynamic record, not a direct DID"
            elif protocol.get("Addressing") not in (None, "normal", "extended"):
                row["rejected"] = f"not a CAN diagnostic address ({protocol['Addressing']})"
            rows.append(row)
        return rows

    def _query(self, arguments: list[str]) -> list[dict[str, Any]]:
        command = [
            str(WORKERS / TISDB),
            "--database", str(self.database_directory),
            "--code-page", str(self.text_code_page),
        ]
        if self.techstream_bin_directory is not None:
            command += ["--bin", str(self.techstream_bin_directory)]
        command += arguments
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode:
            raise RuntimeError(f"TIS query failed ({result.returncode}): {result.stderr.strip()}")
        records = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                records.append(json.loads(line))
        return records


def _normalize_all(raw_rows: list[Any]) -> tuple[tuple[dict[str, Any], ...], tuple[tuple[str, str], ...]]:
    rows = []
    rejections = []
    for index, raw in enumerate(raw_rows):
        if isinstance(raw, dict) and raw.get("rejected"):
            rejections.append((f"row {index}", str(raw["rejected"])))
            continue
        try:
            row = normalize_row(raw)
        except ValueError as exc:
            rejections.append((f"row {index}", str(exc)))
            continue
        if "signal_ref" not in raw:
            # Rows already carrying a signal_ref keep their exact content
            # because catalog variant identities are hashed from it.
            row = {
                "signal_ref": row["signal_ref"],
                **{name: row[name] for name in _TIS_ROW_FIELDS if name in row},
            }
        rows.append(row)
    return tuple(rows), tuple(rejections)


def _formula_for(physical: dict[str, Any] | None) -> tuple[str, str]:
    if not physical or physical.get("Status") != "0x00000000":
        return "raw", "missing-physical-record"
    a = int(physical["CoefficientA"])
    b = int(physical["CoefficientB"])
    c = int(physical["CoefficientC"])
    n = int(physical["SignalLsb"])
    if b == 0:
        return "raw", "invalid-zero-divisor"
    if (a, b, c, n) == (1, 1, 0, 0):
        return "raw", "joined"
    return f"((raw * {a} / {b}) + {c}) / (10 ** {n})", "joined"


def _chunks(values: list[str], size: int = 300) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]
