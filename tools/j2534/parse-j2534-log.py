#!/usr/bin/env python3
"""Inspect Techstream/J2534 text logs and optionally decode a signal profile.

The observed logger represents an ISO-15765 message as a four-byte big-endian
CAN identifier followed by the complete diagnostic payload. This parser fails
on inconsistent declared lengths and keeps START_OF_MESSAGE indications
separate from complete messages.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


CALL_RE = re.compile(
    r"^\s*(?P<time>\d+\.\d+)s\s+(?P<marker>>>|<<|\+\+|--|\*\*)\s+"
    r"(?P<call>PT\w+)\("
)
MSG_RE = re.compile(
    r"^\s*Msg\[\s*\d+\]\s+(?:(?P<timestamp>\d+\.\d+)s\.\s+)?"
    r"(?P<protocol>\d+):(?P<protocol_name>[^.]+)\.\s+"
    r"(?:(?:Actual data \d+ of )?)(?P<size>\d+) bytes\."
)
BYTE_RE = re.compile(r"(?<![0-9A-Fa-f])\[?([0-9A-Fa-f]{2})\]?(?![0-9A-Fa-f])")


@dataclass(frozen=True)
class Message:
    call_time: float
    bus_time: float | None
    direction: str
    protocol: int
    protocol_name: str
    arbitration_id: int | None
    extended_address: int | None
    payload: bytes
    start_of_message: bool


@dataclass(frozen=True)
class Signal:
    data_id: int
    name: str
    first_bit: int
    last_bit: int
    convert: Callable[[int], float | int | bool]
    unit: str


@dataclass(frozen=True)
class DynamicSource:
    data_id: int
    position: int
    size: int


def parse_int(value: int | str, field: str) -> int:
    try:
        return value if isinstance(value, int) else int(value, 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integer for {field}: {value!r}") from exc


def load_signals(path: Path) -> dict[int, tuple[Signal, ...]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("bit_numbering", "msb-first") != "msb-first":
        raise ValueError("only msb-first signal profiles are currently supported")
    grouped: dict[int, list[Signal]] = collections.defaultdict(list)
    for index, item in enumerate(document.get("signals", [])):
        context = f"signals[{index}]"
        conversion = item.get("conversion", {"kind": "identity"})
        kind = conversion.get("kind", "identity")
        if kind == "identity":
            convert: Callable[[int], float | int | bool] = int
        elif kind == "boolean":
            convert = bool
        elif kind == "linear":
            a = parse_int(conversion.get("a", 1), f"{context}.conversion.a")
            b = parse_int(conversion.get("b", 1), f"{context}.conversion.b")
            c = parse_int(conversion.get("c", 0), f"{context}.conversion.c")
            decimals = parse_int(
                conversion.get("decimals", 0), f"{context}.conversion.decimals"
            )
            if b == 0:
                raise ValueError(f"{context}.conversion.b must not be zero")
            convert = lambda raw, a=a, b=b, c=c, decimals=decimals: (
                (raw * a / b) + c
            ) / (10**decimals)
        else:
            raise ValueError(f"unsupported conversion kind {kind!r} in {context}")
        signal = Signal(
            data_id=parse_int(item["data_id"], f"{context}.data_id"),
            name=str(item["name"]),
            first_bit=parse_int(item["first_bit"], f"{context}.first_bit"),
            last_bit=parse_int(item["last_bit"], f"{context}.last_bit"),
            convert=convert,
            unit=str(item.get("unit", "")),
        )
        if signal.first_bit < 0 or signal.last_bit < signal.first_bit:
            raise ValueError(f"invalid bit range in {context}")
        grouped[signal.data_id].append(signal)
    if not grouped:
        raise ValueError("signal profile contains no signals")
    return {data_id: tuple(items) for data_id, items in grouped.items()}


def parse_messages(path: Path) -> list[Message]:
    messages: list[Message] = []
    call_time = 0.0
    direction: str | None = None
    pending: dict[str, Any] | None = None
    start_of_message = False
    extended_addressing = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        call_match = CALL_RE.match(line)
        if call_match:
            call_time = float(call_match.group("time"))
            marker, call = call_match.group("marker"), call_match.group("call")
            direction = "TX" if call == "PTWriteMsgs" and marker == ">>" else None
            if call == "PTReadMsgs" and marker == "<<":
                direction = "RX"
            pending, start_of_message, extended_addressing = None, False, False
            continue
        msg_match = MSG_RE.match(line)
        if msg_match and direction:
            pending = {
                "bus_time": float(msg_match.group("timestamp"))
                if msg_match.group("timestamp")
                else None,
                "protocol": int(msg_match.group("protocol")),
                "protocol_name": msg_match.group("protocol_name"),
                "size": int(msg_match.group("size")),
            }
            start_of_message, extended_addressing = False, False
            continue
        if pending and "START_OF_MESSAGE" in line:
            start_of_message = True
            continue
        if pending and "ISO15765_ADDR_TYPE" in line:
            extended_addressing = True
            continue
        if pending and line.lstrip().startswith("\\__"):
            raw = bytes(int(value, 16) for value in BYTE_RE.findall(line))
            if len(raw) != pending["size"]:
                raise ValueError(
                    f"declared {pending['size']} bytes but parsed {len(raw)} "
                    f"at {call_time:.3f}s"
                )
            candidate_id = int.from_bytes(raw[:4], "big") if len(raw) >= 4 else None
            arbitration_id = (
                candidate_id
                if candidate_id is not None and candidate_id <= 0x1FFFFFFF
                else None
            )
            offset = 4 if arbitration_id is not None else 0
            extended_address = None
            if extended_addressing and not start_of_message and len(raw) > offset:
                extended_address, offset = raw[offset], offset + 1
            messages.append(
                Message(
                    call_time=call_time,
                    bus_time=pending["bus_time"],
                    direction=direction,
                    protocol=pending["protocol"],
                    protocol_name=pending["protocol_name"],
                    arbitration_id=arbitration_id,
                    extended_address=extended_address,
                    payload=raw[offset:],
                    start_of_message=start_of_message,
                )
            )
            pending, start_of_message, extended_addressing = None, False, False
    return messages


def endpoint(message: Message) -> str:
    if message.arbitration_id is None:
        return "<non-CAN>"
    value = f"0x{message.arbitration_id:X}"
    return value + (
        f"/EA=0x{message.extended_address:02X}"
        if message.extended_address is not None
        else ""
    )


def hex_bytes(value: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in value) or "<empty>"


def extract_msb(data: bytes, first_bit: int, last_bit: int) -> int:
    if first_bit < 0 or last_bit < first_bit or last_bit >= len(data) * 8:
        raise ValueError(f"bit range {first_bit}-{last_bit} does not fit {len(data)} bytes")
    width = last_bit - first_bit + 1
    shift = len(data) * 8 - 1 - last_bit
    return (int.from_bytes(data, "big") >> shift) & ((1 << width) - 1)


def dynamic_definition(payload: bytes) -> tuple[int, tuple[DynamicSource, ...]] | None:
    if len(payload) < 8 or payload[:2] != b"\x2c\x01" or (len(payload) - 4) % 4:
        return None
    target = int.from_bytes(payload[2:4], "big")
    sources = tuple(
        DynamicSource(int.from_bytes(payload[i : i + 2], "big"), payload[i + 2], payload[i + 3])
        for i in range(4, len(payload), 4)
    )
    return None if any(not source.position or not source.size for source in sources) else (target, sources)


def collect_definitions(messages: list[Message]) -> dict[int, tuple[DynamicSource, ...]]:
    definitions: dict[int, tuple[DynamicSource, ...]] = {}
    for message in messages:
        if message.direction == "TX" and not message.start_of_message:
            found = dynamic_definition(message.payload)
            if found:
                definitions[found[0]] = found[1]
    return definitions


def decoded_source(
    source_id: int,
    source_data: bytes,
    position: int,
    signals: dict[int, tuple[Signal, ...]],
) -> list[tuple[Signal, float | int | bool]]:
    selected_first = (position - 1) * 8
    selected_last = selected_first + len(source_data) * 8 - 1
    result = []
    for signal in signals.get(source_id, ()):
        if signal.first_bit < selected_first or signal.last_bit > selected_last:
            continue
        raw = extract_msb(
            source_data,
            signal.first_bit - selected_first,
            signal.last_bit - selected_first,
        )
        result.append((signal, signal.convert(raw)))
    return result


def format_value(value: float | int | bool, unit: str) -> str:
    if isinstance(value, bool):
        rendered = "ON" if value else "OFF"
    elif isinstance(value, float):
        rendered = f"{value:.6f}".rstrip("0").rstrip(".")
    else:
        rendered = str(value)
    return f"{rendered} {unit}".rstrip()


def print_decoded(
    messages: list[Message],
    signals: dict[int, tuple[Signal, ...]],
    changes_only: bool,
    definitions: dict[int, tuple[DynamicSource, ...]],
) -> None:
    last: dict[tuple[int | None, int | None, int, str], object] = {}
    matches = 0
    for message in messages:
        payload = message.payload
        if message.direction == "TX" and not message.start_of_message:
            found = dynamic_definition(payload)
            if found:
                definitions[found[0]] = found[1]
            elif len(payload) == 4 and payload[:2] == b"\x2c\x03":
                definitions.pop(int.from_bytes(payload[2:4], "big"), None)
            continue
        if message.direction != "RX" or message.start_of_message or len(payload) < 3 or payload[0] != 0x62:
            continue
        data_id, data = int.from_bytes(payload[1:3], "big"), payload[3:]
        values: list[tuple[int, Signal, float | int | bool]] = []
        values.extend((data_id, signal, value) for signal, value in decoded_source(data_id, data, 1, signals))
        if data_id in definitions:
            cursor = 0
            for source in definitions[data_id]:
                source_data = data[cursor : cursor + source.size]
                cursor += source.size
                if len(source_data) != source.size:
                    values = []
                    break
                values.extend(
                    (source.data_id, signal, value)
                    for signal, value in decoded_source(source.data_id, source_data, source.position, signals)
                )
        rendered = []
        for source_id, signal, value in values:
            key = (message.arbitration_id, message.extended_address, source_id, signal.name)
            if changes_only and last.get(key) == value:
                continue
            last[key] = value
            rendered.append(f"{signal.name}={format_value(value, signal.unit)}")
        if rendered:
            matches += 1
            print(f"{message.call_time:9.3f}s {endpoint(message):>18} DID 0x{data_id:04X}  " + "; ".join(rendered))
    if not matches:
        print("No matching 0x62 responses were decoded with the supplied profile.")


def print_timeline(messages: list[Message], include_starts: bool) -> None:
    for message in messages:
        if message.start_of_message and not include_starts:
            continue
        suffix = " [ISO-TP start indication]" if message.start_of_message else ""
        print(f"{message.call_time:9.3f}s {message.direction:2} {endpoint(message):>18}  {hex_bytes(message.payload)}{suffix}")


def print_summary(messages: list[Message]) -> None:
    complete = [message for message in messages if not message.start_of_message]
    print(f"Parsed messages: {len(complete)} complete, {len(messages) - len(complete)} start indications")
    grouped: dict[tuple[str, str], list[Message]] = collections.defaultdict(list)
    for message in complete:
        grouped[(message.direction, endpoint(message))].append(message)
    for (direction, name), endpoint_messages in sorted(grouped.items()):
        counts = collections.Counter(message.payload for message in endpoint_messages)
        print(f"\n{direction} {name}: {len(endpoint_messages)} messages, {len(counts)} unique payloads")
        for payload, count in counts.most_common():
            print(f"  {count:4}x  {hex_bytes(payload)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--timeline", action="store_true")
    parser.add_argument("--include-start-indications", action="store_true")
    parser.add_argument("--id", action="append", type=lambda value: int(value, 0), dest="ids")
    parser.add_argument("--signals", type=Path, help="JSON signal profile used for decoding")
    parser.add_argument("--changes-only", action="store_true")
    parser.add_argument("--definitions-from", type=Path, metavar="LOG")
    args = parser.parse_args()

    messages = parse_messages(args.log)
    if args.ids:
        selected = set(args.ids)
        messages = [message for message in messages if message.arbitration_id in selected]
    if args.signals:
        definitions = collect_definitions(parse_messages(args.definitions_from)) if args.definitions_from else {}
        print_decoded(messages, load_signals(args.signals), args.changes_only, definitions)
    elif args.timeline:
        print_timeline(messages, args.include_start_indications)
    else:
        print_summary(messages)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
