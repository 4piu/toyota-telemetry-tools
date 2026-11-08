"""Display names for endpoints and verification states.

Endpoint IDs carry the address, diagnostic session, and a hash; that keeps
them unique but makes them poor labels. Pages show the ECU name and address
instead and keep the ID for details and tooltips.
"""

from __future__ import annotations

from typing import Any

from toyota_telemetry.i18n import N_, _, ngettext

from toyota_telemetry.operator.editing import format_condition, format_duration
from toyota_telemetry.operator.inventory import ENDPOINT_SILENT, FAILED, VERIFIED, EndpointItem


# DID verification labels; did_readable() shows them after a one-character mark.
READABLE = N_("Readable")
NOT_CHECKED = N_("Not checked")
NO_ANSWER = N_("No answer")
REFUSED = N_("Refused")
# Endpoint state -> (label, tooltip), English; pages show them through _().
ENDPOINT_READABLE = {
    "responded": (N_("Needs confirming"), N_("Answered; confirm it to plan")),
    "silent": (N_("No answer"), N_("Did not answer")),
    "not verified": (N_("Not checked"), N_("Not probed yet")),
    "address unknown": (N_("No address"), N_("Address unknown; add it under Add candidates")),
}
# Requirement key -> (form label, short lower-case name used in summaries).
REQUIREMENT_NAMES = {
    "minimum_samples": (N_("Minimum samples"), N_("samples")),
    "minimum_runs": (N_("Minimum runs"), N_("runs")),
    "minimum_span_seconds": (N_("Minimum span"), N_("span")),
    "required_states": (N_("Required states"), N_("required states")),
    "minimum_transitions": (N_("Minimum transitions"), N_("transitions")),
    "minimum_changes": (N_("Minimum changes"), N_("changes")),
    "minimum_applicable_seconds": (N_("Minimum applicable time"), N_("applicable time")),
}


def endpoint_name(endpoint: EndpointItem) -> str:
    """The ECU name(s), or the ECU key when no name is known."""

    names = endpoint.ecu_names
    if names:
        return ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
    if endpoint.ecu_keys:
        keys = ", ".join(map(str, endpoint.ecu_keys[:3])) + ("…" if len(endpoint.ecu_keys) > 3 else "")
        return _("ECU {keys}").format(keys=keys)
    return _("Unnamed ECU")


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
    """A DID's verification state as a mark and a word, e.g. "✓ Readable".

    Callers strip the mark with ``[2:]``, so it stays one character and a space.
    """

    mark, label = {
        VERIFIED: ("✓", READABLE), FAILED: ("✕", REFUSED), ENDPOINT_SILENT: ("–", NO_ANSWER),
    }.get(state, ("?", NOT_CHECKED))
    return f"{mark} {_(label)}"


def requirement_label(name: str) -> str:
    """A requirement's form label, e.g. "Minimum samples"."""

    return _(REQUIREMENT_NAMES[name][0])


def count(number: int, noun: str) -> str:
    """'3 signals' in the current language, with that language's plural form."""

    forms = {
        "DID": lambda n: ngettext("{count} DID", "{count} DIDs", n),
        "signal": lambda n: ngettext("{count} signal", "{count} signals", n),
        "run": lambda n: ngettext("{count} run", "{count} runs", n),
        "target": lambda n: ngettext("{count} target", "{count} targets", n),
        "sample": lambda n: ngettext("{count} sample", "{count} samples", n),
    }
    return forms[noun](number).format(count=f"{number:,}")


def requirement_names(names) -> str:
    """Requirement keys as a short phrase, e.g. "Runs, samples"."""

    text = ", ".join(_(REQUIREMENT_NAMES[name][1]) for name in names)
    return text[:1].upper() + text[1:]


def session_label(recipe: dict[str, Any]) -> str:
    """A reviewed session recipe by what it does, e.g. "Extended (10 03)"."""

    steps = recipe.get("steps") or []
    if not steps:
        return recipe.get("id") or _("Session")
    text = steps[0]["label"]
    for step in steps[1:]:
        text = _("{first} then {next}").format(first=text, next=step["label"])
    return f"{text[:1].upper()}{text[1:]} ({steps[-1]['request_hex'].upper()})"


def _can(value: str) -> str:
    return value[2:].upper() if value.lower().startswith("0x") else value.upper()


def still_needed(
    missing: list[str], target: dict[str, Any], counters: dict[str, Any], states: dict[str, str],
) -> str:
    """What a target still lacks; unseen required states by name, e.g. "State ON"."""

    if "required_states" not in missing:
        return requirement_names(missing) or _("Everything")
    seen = set(counters.get("states") or ())
    unseen = [raw for raw in target["requirements"]["required_states"] if raw not in seen]
    text = _("State {states}").format(states=", ".join(states.get(str(raw)) or str(raw) for raw in unseen))
    others = ", ".join(_(REQUIREMENT_NAMES[name][1]) for name in missing if name != "required_states")
    return f"{text}; {others}" if others else text


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
        label = _(REQUIREMENT_NAMES[name][1])
        if name.endswith("_seconds"):
            value = format_duration(value)
        elif isinstance(value, list):
            # Required states read by their text where the signal has one.
            value = ", ".join(
                (states or {}).get(str(raw)) or str(raw) for raw in value
            )
        parts.append(_("{requirement} {value}").format(requirement=label, value=value))
    if target.get("condition"):
        parts.append(_("when {condition}").format(condition=format_condition(target["condition"])))
    text = "; ".join(parts)
    return text if target["included"] else _("{summary} (paused)").format(summary=text)
