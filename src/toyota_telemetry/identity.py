"""Stable identities for the schema-v2 telemetry project model."""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid


_SIGNAL = re.compile(
    r"(?P<ecu>[0-9]+)/0x(?P<did>[0-9A-Fa-f]{4})/"
    r"(?P<first>[0-9]+)\.\.(?P<last>[0-9]+)"
)
_ACQUISITION = re.compile(
    r"(?P<endpoint>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)/"
    r"0x(?P<did>[0-9A-Fa-f]{4})"
)
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def canonical_did(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9A-Fa-f]{4}", value):
        raise ValueError(f"invalid DID: {value!r}")
    return f"0x{int(value, 16):04X}"


# Data identifiers below 0x0100 are reserved by ISO 14229 and never read as
# DIDs here; the toolkit uses them for the one-byte local identifiers of older
# (KWP2000-style, Toyota "phase 4") ECUs, read with 21 LL -> 61 LL ...
LOCAL_IDENTIFIER_LIMIT = 0x100


def is_local_identifier(data_id: int) -> bool:
    return 0 <= data_id < LOCAL_IDENTIFIER_LIMIT


def read_request_hex(data_id: int) -> str:
    """The read request: ``22 HH LL`` for a DID, ``21 LL`` for a local identifier."""

    if is_local_identifier(data_id):
        return f"21 {data_id:02X}"
    return f"22 {data_id >> 8:02X} {data_id & 0xFF:02X}"


def response_prefix_hex(data_id: int) -> str:
    """The positive-response echo that precedes the data."""

    if is_local_identifier(data_id):
        return f"61 {data_id:02X}"
    return f"62 {data_id >> 8:02X} {data_id & 0xFF:02X}"


def response_header_bytes(data_id: int) -> int:
    return 2 if is_local_identifier(data_id) else 3


def within_read_range(data_id: int, did_min: int, did_max: int) -> bool:
    """DIDs must lie in the reviewed safety range; local identifiers always may."""

    return is_local_identifier(data_id) or did_min <= data_id <= did_max


def canonical_target_address(value: object) -> str | None:
    """The extended-addressing target byte as ``0xNN``, or None for normal addressing.

    Accepts None, an integer 0..255, or text such as ``2A`` / ``0x2a``.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid target address: {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0[xX])?[0-9A-Fa-f]{1,2}", value.strip()):
        number = int(value.strip().removeprefix("0x").removeprefix("0X"), 16)
    else:
        raise ValueError(f"invalid target address: {value!r}")
    if not 0 <= number <= 0xFF:
        raise ValueError(f"target address is outside one byte: {value!r}")
    return f"0x{number:02X}"


def address_suffix(target_address: str | None) -> str:
    """The identifier suffix that keeps modules behind one gateway ID apart."""

    return "" if target_address is None else f"-{target_address[2:].lower()}"


def stable_slug(value: str, label: str = "identifier") -> str:
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


@dataclass(frozen=True, order=True)
class SignalRef:
    ecu_key: int
    data_id: int
    first_bit: int
    last_bit: int

    @classmethod
    def parse(cls, value: str) -> "SignalRef":
        match = _SIGNAL.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise ValueError(f"invalid signal_ref: {value!r}")
        result = cls(
            int(match.group("ecu")),
            int(match.group("did"), 16),
            int(match.group("first")),
            int(match.group("last")),
        )
        if not 0 <= result.ecu_key <= 65535:
            raise ValueError(f"signal_ref ECU key is out of range: {value!r}")
        if result.last_bit < result.first_bit:
            raise ValueError(f"signal_ref bit range is reversed: {value!r}")
        return result

    def __str__(self) -> str:
        return (
            f"{self.ecu_key}/0x{self.data_id:04X}/"
            f"{self.first_bit}..{self.last_bit}"
        )


@dataclass(frozen=True, order=True)
class AcquisitionRef:
    endpoint_id: str
    data_id: int

    @classmethod
    def parse(cls, value: str) -> "AcquisitionRef":
        match = _ACQUISITION.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise ValueError(f"invalid acquisition_id: {value!r}")
        return cls(match.group("endpoint"), int(match.group("did"), 16))

    def __str__(self) -> str:
        return f"{self.endpoint_id}/0x{self.data_id:04X}"


def canonical_run_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid run_id: {value!r}") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"run_id is not canonical lowercase UUID: {value!r}")
    return canonical
