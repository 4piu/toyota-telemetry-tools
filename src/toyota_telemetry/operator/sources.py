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


TECHSTREAM_REGIONS = ("NA", "EU", "OT", "JP")
FALLBACK_PROGRAM_FILES = ("C:\\Program Files (x86)",)


def default_tis_database() -> Path | None:
    """The installed Techstream database folder in the standard location, if any.

    Techstream installs under Program Files (x86)\\Toyota Diagnostics\\Techstream
    with one folder per region; NA is preferred, then EU, OT, and JP.
    """

    roots = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), *FALLBACK_PROGRAM_FILES]
    for root in dict.fromkeys(value for value in roots if value):
        techstream = Path(root) / "Toyota Diagnostics" / "Techstream"
        for region in TECHSTREAM_REGIONS:
            folder = techstream / region
            if (folder / "DB").is_dir():
                return folder
    return None


class TisDatabaseSource:
    """Installed Techstream databases read through the bundled telemetry-tisdb helper.

    Phase 5 items are read with 22 DID. Phase 4 items are read with 21 LL (a
    one-byte local identifier) and stored with DID 0x00LL; items whose layout
    the helper does not understand are rejected rather than guessed.

    Every installed ECU database is read; its generation comes from the data
    list it holds, and ECUs without a CAN diagnostic address are left out.
    An optional list of ECU keys narrows the selection.
    Database records are candidates; only verification establishes support.
    """

    kind = "tis"
    source_id = "tis"

    def __init__(
        self,
        database_directory: Path,
        *,
        ecu_keys: Iterable[int] = (),
        techstream_bin_directory: Path | None = None,
        text_code_page: int = 932,
    ) -> None:
        self.database_directory = Path(database_directory)
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
            selected.setdefault(key, {"key": key, "name": record["Ecu"]})
        if not selected:
            raise ValueError("no installed TIS database matches the ECU selection")
        raw_rows: list[dict[str, Any]] = []
        skipped: list[tuple[str, str]] = []
        imported = 0
        for key in sorted(selected):
            ecu_rows, problem = self._ecu_rows(selected[key])
            if problem:
                skipped.append((f"ECU {key}", problem))
                continue
            imported += 1
            raw_rows.extend(ecu_rows)
        rows, rejections = _normalize_all(raw_rows)
        label = f"TIS databases ({imported} ECUs)"
        return SourceRows(self.source_id, self.kind, label, rows, tuple(skipped) + rejections)

    def _ecu_rows(self, ecu: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        """Rows of one ECU, or a reason the whole ECU is left out.

        The database generation is taken from which data list the ECU has,
        not from its file name (partner-built and variant files carry
        suffixes such as _P5FHI or AdjustP4).
        """
        key = ecu["key"]
        protocols = [
            row for row in self._query(["--keys", str(key), "--protocol"])
            if row.get("Status") == "0x00000000" and int(row.get("Count", 0) or 0) > 0
        ]
        # An ECU may list several transports (e.g. K-line variants); use its CAN one.
        protocol = next(
            (row for row in protocols if row.get("Addressing") in (None, "normal", "extended")), None,
        )
        if protocol is None and protocols:
            return [], f"not a CAN diagnostic address ({protocols[0].get('Addressing')})"
        protocol = protocol or {}
        monitors = [
            row for row in self._query(["--keys", str(key), "--monitors"])
            if row.get("Status") == "0x00000000" and row.get("Name")
        ]
        phase4 = not monitors
        if phase4:
            monitors = [
                row for row in self._query(["--keys", str(key), "--p4-monitors"])
                if row.get("Status") == "0x00000000" and row.get("Name")
            ]
            for row in monitors:
                # A local identifier LL is stored as DID 0x00LL and read with 21 LL.
                row["DataId"] = f"0x{int(row['LocalId'], 16):04X}"
                row["InternalId"] = row["LocalId"]
                row["Acquisition"] = "direct DID" if row.get("Layout") == "local identifier" else "unknown layout"
        if not monitors:
            return [], "no data list in a supported layout"
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
            if monitor.get("Acquisition") == "unknown layout":
                row["rejected"] = "phase-4 item layout not understood"
            elif monitor.get("Acquisition") != "direct DID":
                row["rejected"] = "mapped or dynamic record, not a direct DID"
            elif not phase4 and int(monitor["DataId"], 16) < 0x100:
                # Phase-5 items below 0x0100 (seen: 0x0000 placeholders) would be
                # read as local identifiers, which they are not.
                row["rejected"] = "DID below 0x0100 is reserved (local identifiers)"
            rows.append(row)
        return rows, None

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
