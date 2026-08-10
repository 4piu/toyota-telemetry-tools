#!/usr/bin/env python3
"""Print a privacy-minimized section inventory from a Techstream .TSE file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import struct
import sys


GLOBAL_SECTION_COUNT = 14
ECU_SECTION_COUNT = 18


def read_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def read_slot(data: bytes, offset: int) -> tuple[str, int]:
    name = data[offset : offset + 12].split(b"\0", 1)[0].decode("ascii", "replace")
    return name, read_u32(data, offset + 12)


def inspect(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    if len(data) < 0x100 or data[:8] != b"\x9a\x53\xdb\x12TSE\0":
        raise ValueError("not a recognized Techstream TSE file")

    cursor = 0x1A
    version_length = read_u16(data, cursor)
    cursor += 2
    version = data[cursor : cursor + version_length].decode("ascii", "replace")
    cursor += version_length
    locale_length = read_u16(data, cursor)
    cursor += 2
    database_locale = data[cursor : cursor + locale_length].decode("ascii", "replace")
    cursor += locale_length

    for _ in range(GLOBAL_SECTION_COUNT):
        read_slot(data, cursor)
        cursor += 16

    ecu_count = read_u16(data, cursor)
    cursor += 2
    ecus = []
    for _ in range(ecu_count):
        sections = []
        for _ in range(ECU_SECTION_COUNT):
            name, content_offset = read_slot(data, cursor)
            sections.append({"name": name, "offset": content_offset})
            cursor += 16
        match = re.fullmatch(r"EcuData(\d{4})", sections[0]["name"])
        ecus.append(
            {
                "key": int(match.group(1)) if match else None,
                "section_names": [section["name"] for section in sections],
            }
        )

    return {
        "format": "TSE",
        "techstream_version": version,
        "database_locale": database_locale,
        "ecu_section_count": ecu_count,
        "ecu_keys": [ecu["key"] for ecu in ecus],
        "ecus": ecus,
        "note": "Vehicle identifiers and raw section contents are intentionally omitted.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.session), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
