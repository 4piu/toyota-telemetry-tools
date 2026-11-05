"""State text: what the manufacturer's tool shows for a signal's raw values.

A catalog row may carry ``states``, {raw value as text: label}. A raw value
missing from the set has no label and is shown as its number.
"""

from __future__ import annotations

from typing import Mapping


def raw_number(raw_hex: str) -> int:
    """The unsigned raw value of an observation's ``raw_hex`` bytes."""

    return int.from_bytes(bytes.fromhex(raw_hex), "big")


def state_label(states: Mapping[str, str], raw: int | None) -> str | None:
    if raw is None:
        return None
    return states.get(str(raw))


def state_text(states: Mapping[str, str]) -> str:
    """Raw-value labels as one line, e.g. "0=OFF, 1=ON", in raw order."""

    return ", ".join(f"{raw}={states[raw]}" for raw in sorted(states, key=int))
