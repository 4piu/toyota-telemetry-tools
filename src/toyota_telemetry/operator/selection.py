"""One selector grammar shared by the CLI and the desktop tree.

``ENDPOINT``, ``ENDPOINT/0xNNNN``, ``ENDPOINT/0xNNNN/FIRST..LAST``, or a
signal reference ``ECU/0xNNNN/FIRST..LAST`` choose nodes in the inventory
tree. Filters narrow the chosen (or, without tokens, all) nodes. Selection
never creates ranges over CAN IDs or DIDs: an explicit DID token on a known
endpoint is the only way to name a DID absent from the candidate list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

from ..i18n import _
from ..identity import SignalRef, canonical_did
from .inventory import (
    ENDPOINT_NOT_VERIFIED,
    ENDPOINT_SILENT,
    FAILED,
    NOT_VERIFIED,
    VERIFIED,
    DidItem,
    EndpointItem,
    InventoryTree,
)


STATE_FILTERS = {
    "verified": {VERIFIED},
    # A silent ECU's DIDs were never read; it may answer another time.
    "not-verified": {NOT_VERIFIED, ENDPOINT_NOT_VERIFIED, ENDPOINT_SILENT},
    "failed": {FAILED},
}
SIGNAL_FILTERS = {"targeted", "untargeted"}
_BITS = re.compile(r"[0-9]+\.\.[0-9]+")


@dataclass
class Selection:
    """Ordered DID keys and signal references chosen from one tree."""

    dids: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    manual_dids: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.dids and not self.signals

    def as_dict(self) -> dict[str, list[str]]:
        return {"dids": list(self.dids), "signals": list(self.signals)}


def resolve_selection(
    tree: InventoryTree,
    tokens: Iterable[str] = (),
    *,
    ecus: Iterable[int] = (),
    states: Iterable[str] = (),
    search: str | None = None,
    require: bool = True,
) -> Selection:
    tokens = [token.strip() for token in tokens if token.strip()]
    ecu_filter = {int(value) for value in ecus}
    state_filter = list(states)
    for state in state_filter:
        if state not in STATE_FILTERS and state not in SIGNAL_FILTERS:
            choices = ", ".join(sorted([*STATE_FILTERS, *SIGNAL_FILTERS]))
            raise ValueError(_("unknown state filter {state!r}; choose from {choices}").format(state=state, choices=choices))
    if require and not tokens and not ecu_filter and not state_filter and not search:
        raise ValueError(_("nothing selected: name an endpoint, DID, or signal, or use a filter"))

    picked: list[tuple[EndpointItem, DidItem, list[str] | None]] = []
    manual: list[str] = []
    if tokens:
        for token in tokens:
            picked.extend(_resolve_token(tree, token, manual))
    else:
        picked = [(endpoint, did, None) for endpoint in tree.endpoints for did in endpoint.dids]

    selection = Selection(manual_dids=manual)
    seen_dids: set[str] = set()
    seen_signals: set[str] = set()
    needle = search.lower() if search else None
    for endpoint, did, only in picked:
        if ecu_filter and not ecu_filter & set(endpoint.ecu_keys):
            continue
        did_states = [state for state in state_filter if state in STATE_FILTERS]
        if did_states and not any(did.state in STATE_FILTERS[state] for state in did_states):
            continue
        signals = [
            signal for signal in did.signals
            if (only is None or signal.signal_ref in only)
            and (not ecu_filter or signal.ecu_key in ecu_filter)
            and _signal_matches(signal, state_filter, needle)
        ]
        restricted = only is not None or needle or _has_signal_filter(state_filter) or ecu_filter
        if restricted and did.signals and not signals:
            continue
        if did.key not in seen_dids:
            seen_dids.add(did.key)
            selection.dids.append(did.key)
        for signal in signals:
            if signal.signal_ref not in seen_signals:
                seen_signals.add(signal.signal_ref)
                selection.signals.append(signal.signal_ref)
    if require and selection.empty:
        raise ValueError(_("the selection matched nothing in the candidate list"))
    return selection


def _resolve_token(
    tree: InventoryTree, token: str, manual: list[str]
) -> list[tuple[EndpointItem, DidItem, list[str] | None]]:
    parts = token.split("/")
    if len(parts) == 3 and parts[0].isdigit():
        signal_ref = str(SignalRef.parse(token))
        found = tree.find_signal(signal_ref)
        if found is None:
            raise ValueError(_("signal is not in the candidate list: {signal}").format(signal=signal_ref))
        endpoint, did, _signal = found
        return [(endpoint, did, [signal_ref])]
    endpoint = tree.endpoint(parts[0])
    if endpoint is None:
        raise ValueError(_("unknown endpoint {endpoint!r}").format(endpoint=parts[0]))
    if len(parts) == 1:
        return [(endpoint, did, None) for did in endpoint.dids]
    if len(parts) > 3:
        raise ValueError(_("invalid selector {token!r}").format(token=token))
    try:
        data_id = canonical_did(parts[1] if parts[1].lower().startswith("0x") else f"0x{parts[1]}")
    except ValueError as exc:
        raise ValueError(_("invalid DID in selector {token!r}").format(token=token)) from exc
    did = next((row for row in endpoint.dids if row.data_id == data_id), None)
    if did is None:
        if len(parts) == 3:
            raise ValueError(_("DID {did} is not in the candidate list for {endpoint}").format(did=data_id, endpoint=endpoint.key))
        did = DidItem(endpoint.key, data_id, state=(
            NOT_VERIFIED if endpoint.confirmed or endpoint.state == "responded" else ENDPOINT_NOT_VERIFIED
        ), manual=True)
        endpoint.dids.append(did)
        manual.append(did.key)
        return [(endpoint, did, None)]
    if len(parts) == 2:
        return [(endpoint, did, None)]
    if _BITS.fullmatch(parts[2]) is None:
        raise ValueError(_("invalid signal bits in selector {token!r}").format(token=token))
    ecu = endpoint.ecu_keys[0] if len(endpoint.ecu_keys) == 1 else _ecu_for(did, parts[2])
    signal_ref = str(SignalRef.parse(f"{ecu}/{data_id}/{parts[2]}"))
    if not any(signal.signal_ref == signal_ref for signal in did.signals):
        raise ValueError(_("signal is not in the candidate list: {signal}").format(signal=token))
    return [(endpoint, did, [signal_ref])]


def _ecu_for(did: DidItem, bits: str) -> int:
    matches = [signal.ecu_key for signal in did.signals if signal.signal_ref.endswith(f"/{bits}")]
    if len(set(matches)) != 1:
        raise ValueError(
            _("bits {bits} are ambiguous on {did}; use the full signal reference ECU/DID/BITS").format(
                bits=bits, did=did.key,
            )
        )
    return matches[0]


def _has_signal_filter(states: list[str]) -> bool:
    return any(state in SIGNAL_FILTERS for state in states)


def _signal_matches(signal, states: list[str], needle: str | None) -> bool:
    if "targeted" in states and signal.target is None:
        return False
    if "untargeted" in states and signal.target is not None:
        return False
    if needle and needle not in signal.name.lower() and needle not in signal.signal_ref.lower():
        return False
    return True
