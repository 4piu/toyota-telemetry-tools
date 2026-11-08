"""Display names for endpoints and verification states.

Endpoint IDs carry the address, diagnostic session, and a hash; that keeps
them unique but makes them poor labels. Pages show the ECU name and address
instead and keep the ID for details and tooltips.
"""

from __future__ import annotations

from typing import Any

from toyota_telemetry.operator.editing import REQUIREMENT_LABELS, format_condition, format_duration
from toyota_telemetry.operator.inventory import ENDPOINT_SILENT, FAILED, VERIFIED, EndpointItem


READABLE = "✓ Readable"
NOT_CHECKED = "? Not checked"
NO_ANSWER = "– No answer"
REFUSED = "✕ Refused"
ENDPOINT_READABLE = {
    "responded": ("Needs confirming", "Answered; confirm it to plan"),
    "silent": ("No answer", "Did not answer"),
    "not verified": ("Not checked", "Not probed yet"),
    "address unknown": ("No address", "Address unknown; add it under Add candidates"),
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
    text = f"{_can(endpoint.tester_can_id)}→{_can(endpoint.response_can_id)}"
    # Behind a gateway several ECUs share one ID pair; the address byte tells them apart.
    return text + (f" @{endpoint.target_address[2:]}" if endpoint.target_address else "")


def endpoint_label(endpoint: EndpointItem) -> str:
    address = endpoint_address(endpoint)
    return f"{endpoint_name(endpoint)}  {address}" if address else endpoint_name(endpoint)


def did_readable(state: str) -> str:
    return {VERIFIED: READABLE, FAILED: REFUSED, ENDPOINT_SILENT: NO_ANSWER}.get(state, NOT_CHECKED)


def count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def requirement_names(names) -> str:
    """Requirement keys as a short phrase, e.g. "Runs, samples"."""

    text = ", ".join(REQUIREMENT_LABELS[name].lower().replace("minimum ", "") for name in names)
    return text[:1].upper() + text[1:]


def session_label(recipe: dict[str, Any]) -> str:
    """A reviewed session recipe by what it does, e.g. "Extended (10 03)"."""

    steps = recipe.get("steps") or []
    if not steps:
        return recipe.get("id", "Session")
    text = " then ".join(step["label"] for step in steps)
    return f"{text[:1].upper()}{text[1:]} ({steps[-1]['request_hex'].upper()})"


def _can(value: str) -> str:
    return value[2:].upper() if value.lower().startswith("0x") else value.upper()


def still_needed(
    missing: list[str], target: dict[str, Any], counters: dict[str, Any], states: dict[str, str],
) -> str:
    """What a target still lacks; unseen required states by name, e.g. "State ON"."""

    if "required_states" not in missing:
        return requirement_names(missing) or "Everything"
    seen = set(counters.get("states") or ())
    unseen = [raw for raw in target["requirements"]["required_states"] if raw not in seen]
    others = requirement_names([name for name in missing if name != "required_states"])
    text = "State " + ", ".join(states.get(str(raw)) or str(raw) for raw in unseen)
    return text + (f"; {others.lower()}" if others else "")


def states_lines(states: dict[str, str], width: int = 60) -> str:
    """State text wrapped to tooltip width: "0=P, 2=R, 4=N, ..."."""

    lines, line = [], ""
    for raw in sorted(states, key=int):
        entry = f"{raw}={states[raw]}"
        if line and len(line) + 2 + len(entry) > width:
            lines.append(line + ",")
            line = entry
        else:
            line = f"{line}, {entry}" if line else entry
    return "\n".join(lines + [line]) if line else ""


def target_summary(target: dict[str, Any] | None, states: dict[str, str] | None = None) -> str:
    if target is None:
        return ""
    parts = []
    for name, value in target["requirements"].items():
        label = REQUIREMENT_LABELS[name].lower().replace("minimum ", "")
        if name.endswith("_seconds"):
            value = format_duration(value)
        elif isinstance(value, list):
            # Required states read by their text where the signal has one.
            value = ", ".join(
                (states or {}).get(str(raw)) or str(raw) for raw in value
            )
        parts.append(f"{label} {value}")
    if target.get("condition"):
        parts.append(f"when {format_condition(target['condition'])}")
    text = "; ".join(parts)
    return text if target["included"] else f"{text} (paused)"
