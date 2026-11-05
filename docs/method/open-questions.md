# Open questions

Things this method does not yet solve. Listed so a new vehicle project knows
where the ground is soft rather than discovering it halfway through a session.

## Recovering the session setup for a new platform

**Status: unsolved. This is the biggest gap in the workflow.**

Before any DID can be read, the backend replays a sequence of UDS requests that
opens a session the ECU will answer in — typically tester present, then a
default session, then an extended session. That sequence goes into the
workspace's `config/transport.json` as a session recipe, and everything
downstream depends on it being right. A new workspace starts with no session
recipes.

There is no automated way to derive it.

What has worked: capture a real tester conversation over J2534 while the
manufacturer's diagnostic application displays a live data list, then read the
setup requests off the trace with
[`tools/j2534/parse-j2534-log.py`](../../tools/j2534/parse-j2534-log.py). See
[capturing a J2534 conversation](03-locate/j2534-capture.md).

That route has real preconditions: a working J2534 interface, a licensed copy
of the manufacturer's diagnostic application, and a vehicle it will talk to. A
project missing any of those is stuck at this step.

Partial mitigations, none sufficient on their own:

- A tester-present / default-session / extended-session sequence is common on
  Toyota platforms and may inform a bounded experiment. It remains a guess
  until captured or otherwise evidenced for the target endpoint.
- Some ECUs answer `0x22` reads with no session setup at all beyond tester
  present. Worth trying before assuming a complex sequence.
- A static analysis of the diagnostic application can reveal the sequence
  without a vehicle, at much higher effort. See
  [static analysis](03-locate/static-analysis.md).

Better acquisition paths are wanted. Anything that reduces this to "run a tool
and read the answer" would remove the single largest barrier to reusing this
method on an unfamiliar platform. Candidate directions, none investigated:
deriving the sequence from the diagnostic database rather than a live trace;
a bounded, safe probe that tries a small set of known session patterns and
reports which one the ECU accepts.

## Deriving response CAN IDs

Where a protocol table gives a tester ID but no response ID, this method infers
tester + 8 by convention and marks the result a candidate until observed. That
convention is common but not universal, and nothing here validates it beyond
"a response arrived".

## Enum semantics

Raw state values are recorded faithfully, and a state that changes with an
observable event can be correlated. Mapping a raw state to a *meaning* —
which gear, which control mode, which inverter request — depends on the OEM
database labels being present and trustworthy. Techstream's state table now
supplies them (see [enums and invalid values](05-decode/enums-and-invalids.md));
whether they hold for a given calibration is still checked against observed
behavior. Where a set is absent, the enum stays unmapped and the signal stays
Observed.

## Signals that cannot be safely excited

ABS and traction/stability intervention, collision and pre-collision states,
airbag-related fields, and similar safety-critical signals cannot be induced
without unsafe operation. They remain untested unless a naturally occurring
event happens to be captured, or a proper test facility is available.

This is a permanent limitation, not a gap to close. The evidence model handles
it by never treating absence of transitions as a negative result — see the
constant-signal guidance in [the quickstart](../quickstart.md).

## CAN FD and extended addressing

The shipped backends implement Classical CAN with normal addressing only.
Both are rejected explicitly in configuration rather than silently mishandled.
Extending to CAN FD would touch ISO-TP framing (larger frames, different length
encoding) and every device binding.
