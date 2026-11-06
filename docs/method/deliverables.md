# Deliverable repository

A deliverable repository publishes what was learned about one vehicle. It
contains no programs and no private working data. The test for where a file
belongs is whether a second vehicle would want it unchanged: if so, it belongs
in the tool or this method, not in a deliverable repository.

## Layout

```text
README.md            the vehicle, the current result, and how to reproduce it
PRIVATE_DATA.md      what is private for this vehicle, and why
config/              exported transport, collection policy, catalog manifest
review/              exported signal review
result/              exported result generations and current pointer
docs/findings/       how endpoints, transport, and formulas were established
docs/evidence/       one report per capture campaign
docs/hardware/       adapter, wiring, and measured throughput
docs/validation/     scope, policy choices, and campaign status
docs/reproduce.md    how the result is reproduced with an installed tool
```

`config/`, `review/`, and `result/` are written by `telemetry export DIR` (GUI:
Project > Export result). Edit their sources in the workspace and export
again; a hand edit in the deliverable repository is overwritten by the next
export. Endpoint and dynamic-DID `provenance` entries in the workspace should
name documents in the deliverable repository, for example
`docs/findings/data-path.md`, as relative paths.

Raw captures, the candidate list, full database-derived atlases, local
settings, and plans stay in the workspace. Ignore rules are not a
confidentiality boundary; review every staged file.

## Findings

How this vehicle's endpoints, transport, and formulas were established: the
reasoning and the evidence, not the procedure. Procedure lives in this method.
One page per question answered. Suggested starting points:

- the diagnostic data path: which physical connection, which gateway, which
  addressing;
- the static catalog: what the OEM database claims, and what it cannot tell
  you;
- the map: how candidate rows became evidence-backed rows.

## Evidence

One report per capture campaign: what ran, what it showed, and what it did not
establish.

These are records. When the tooling that produced them is later replaced, keep
the commands as they were run and add a note saying so; rewriting them would
falsify the record. Only "re-run this" instructions should be updated to name
tools a reader can still invoke.

## Hardware

The adapter used, how it was wired, and what throughput was measured.

Record the bitrate and why: it belongs to the vehicle, not to the adapter.
Transmitting at a rate the bus does not use puts malformed frames on a live
network and can drive nodes to bus-off.

## Validation

What is specific to this vehicle's validation: the scope (which ECUs,
endpoints, and DIDs, and what has been captured), the policy choices in the
collection policy, and where the campaign stands. Procedure lives in the
[quickstart](../quickstart.md) and
[progressive validation](../progressive-validation.md).

For collection targets, record the included endpoint+DID settings and cadence,
each signal's completion requirements and condition, and the safety reasoning.
Targets explain collection; they do not filter otherwise valid observations
from later review.

A negative reply means the read was refused in the tested session and vehicle
state, not that the DID is absent. Static database aggregation means many
candidates describe equipment the vehicle does not have, which is expected.

## Private data

Nothing in a deliverable repository should identify a vehicle or its owner.
Use an arbitrary instance alias such as `vehicle-a`.

| Kind | Where it lives | Published? |
|---|---|---|
| Raw captures | Workspace `captures/` | No |
| Local analysis, plans, settings | Workspace `state/` | No |
| Candidate list and full source atlas | Workspace | No; only provenance is exported |
| Signal definitions and evidence states | Exported `review/` and `result/` | Yes |

Exported results may contain privacy-minimized aggregate observations, but not
raw payloads, timestamps, run notes, adapter identifiers, or local paths.
Review those aggregates for identity-bearing values before publication. Before
publishing any example capture, sanitize it: a raw capture of an
identity-bearing DID does contain the identifier.
