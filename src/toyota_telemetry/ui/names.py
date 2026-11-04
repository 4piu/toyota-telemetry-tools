"""Display names for endpoints and verification states.

Endpoint IDs carry the address, diagnostic session, and a hash; that keeps
them unique but makes them poor labels. Pages show the ECU name and address
instead and keep the ID for details and tooltips.
"""

from __future__ import annotations

from typing import Any

from toyota_telemetry.operator.editing import REQUIREMENT_LABELS, format_condition, format_duration
from toyota_telemetry.operator.inventory import FAILED, VERIFIED, EndpointItem


READABLE = "✓ Readable"
NOT_CHECKED = "? Not checked"
REFUSED = "✕ Refused"
ENDPOINT_READABLE = {
    "responded": ("Needs confirming", "Answered a probe; confirm it to plan collection"),
    "silent": ("No answer", "Did not answer when probed"),
    "not verified": ("Not checked", "Not probed yet; Verify checks it"),
    "address unknown": ("No address", "No CAN address is known; Add endpoint to give it one"),
}


def endpoint_name(endpoint: EndpointItem) -> str:
    """The ECU name(s), or the ECU key when no name is known."""

    names = endpoint.ecu_names
    if names:
        return ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
    if endpoint.ecu_keys:
        return "ECU " + ", ".join(map(str, endpoint.ecu_keys[:3])) + ("…" if len(endpoint.ecu_keys) > 3 else "")
    return "Unnamed ECU"


def endpoint_address(endpoint: EndpointItem) -> str:
    if endpoint.tester_can_id is None or endpoint.response_can_id is None:
        return ""
    return f"{_can(endpoint.tester_can_id)}→{_can(endpoint.response_can_id)}"


def endpoint_label(endpoint: EndpointItem) -> str:
    address = endpoint_address(endpoint)
    return f"{endpoint_name(endpoint)}  {address}" if address else endpoint_name(endpoint)


def did_readable(state: str) -> str:
    return READABLE if state == VERIFIED else REFUSED if state == FAILED else NOT_CHECKED


def count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def requirement_names(names) -> str:
    """Requirement keys as a short phrase, e.g. "Runs, samples"."""

    text = ", ".join(REQUIREMENT_LABELS[name].lower().replace("minimum ", "") for name in names)
    return text[:1].upper() + text[1:]


def _can(value: str) -> str:
    return value[2:].upper() if value.lower().startswith("0x") else value.upper()


def target_summary(target: dict[str, Any] | None) -> str:
    if target is None:
        return ""
    parts = []
    for name, value in target["requirements"].items():
        label = REQUIREMENT_LABELS[name].lower().replace("minimum ", "")
        if name.endswith("_seconds"):
            value = format_duration(value)
        elif isinstance(value, list):
            value = ",".join(map(str, value))
        parts.append(f"{label} {value}")
    if target.get("condition"):
        parts.append(f"when {format_condition(target['condition'])}")
    text = "; ".join(parts)
    return text if target["included"] else f"{text} (excluded)"
