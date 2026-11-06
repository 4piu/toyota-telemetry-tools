# The method

How to turn a vehicle's diagnostic data into a reviewed, evidence-backed
signal map. It is written for any vehicle; nothing in it is specific to one.
Command-level usage is in the [quickstart](../quickstart.md).

| Step | Page |
|---|---|
| 0 | [Scope and safety](00-scope-safety.md) — read before connecting anything |
| 1 | [Record the vehicle profile](01-vehicle-profile.md) |
| 2 | [Define the target signal](02-target-signal.md) |
| 3 | [Locate acquisition metadata](03-locate/README.md) |
| 4 | [Reconstruct diagnostic transport](04-reconstruct/diagnostic-transport.md) and [dynamic DIDs](04-reconstruct/dynamic-dids.md) |
| 5 | [Decode formulas and bitfields](05-decode/formulas-and-bitfields.md) and [enums and invalid values](05-decode/enums-and-invalids.md) |
| 6 | [Verify on the vehicle](06-verify/README.md) |
| 7 | [Evidence records and catalog schema](07-catalog/evidence-and-schema.md) |

[Deliverables](deliverables.md) describes what a vehicle's published repository
contains. [Open questions](open-questions.md) records where the method is still
soft. [`templates/`](templates/) holds blank vehicle-profile, signal-dossier,
and experiment records to copy.

## Where things live

A vehicle project has three parts:

| Part | Contents |
|---|---|
| The tool (this repository) | Programs, adapter workers, and this method. Installed once, for example `uv tool install --editable PATH-TO/toyota-telemetry-tools[gui]`. It holds no vehicle facts. |
| The workspace | One private directory per project, created by `telemetry init DIR` or Project > New project, outside any repository. It is the source of truth for transport, collection policy, signal review, and catalog manifest, and holds captures and local state. |
| The deliverable repository | Findings, evidence write-ups, hardware and validation notes, and the results written by `telemetry export DIR` or Project > Export result. Its `config/`, `review/`, and `result/` are exported copies, not hand-edited. |

A program that would help a second vehicle belongs in the tool, not in a
deliverable repository.

Confidence words used throughout: Candidate, Observed, Correlated, Validated,
Replayed, Ported.
