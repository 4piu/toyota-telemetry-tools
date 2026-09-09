# Toolkit quickstart

The toolkit runs inside a schema-2 vehicle project. A project owns vehicle
facts and evidence; this repository owns reusable programs.

## Install

From the vehicle project root:

```powershell
..\telemetry-tools\tools\setup-platform-project.ps1
.\.venv\Scripts\Activate.ps1
telemetry --version
```

Setup reads `[toolkit]` and `[paths]` from `telemetry-project.toml`, builds all
private adapter workers into the project's ignored `scratch/bin/`, and installs
the Python package from that same checkout.

## Project inputs

The required tracked authorities are:

- `config/transport.json`: confirmed network, endpoints, session bytes,
  dynamic-DID definitions, and measured safety limits;
- `config/collection-policy.json`: reviewed acquisitions and signal
  objectives;
- `review/signals.json`: reviewed signal identities, decode choices, evidence
  states, and notes;
- `result/catalog-manifest.json`: provenance for the normalized private catalog
  input.

Adapter selection belongs only in ignored `scratch/local-settings.json`.
Normalized catalog rows and immutable captures also stay under `scratch/`.

## Offline workflow

```powershell
telemetry catalog import --catalog-rows scratch/catalog-rows.json
telemetry capture inspect
telemetry-tui
telemetry plan
telemetry export
```

`catalog import` and `cache rebuild` reconstruct the disposable SQLite cache.
`telemetry-tui` opens the standalone offline-first operator application;
merely opening or navigating it never starts a process. Curation in the TUI
records human facts and collection objectives. `plan` compiles policy and prior evidence
into a hashed admitted plan without opening an adapter. `export` creates one
privacy-minimized result generation from source authorities and all valid
captures.

An empty policy is valid but cannot authorize collection. Missing reviewed
cost, safety, decode, or applicability facts also block admission.

## Endpoint discovery and DID preflight

Before a candidate endpoint has a confirmed entry in `config/transport.json`,
or before a candidate DID is curated into policy, use the two explicitly live
qualification commands. Both are bounded: every candidate is named on the
command line, never swept automatically.

```powershell
telemetry discover endpoints --candidate 0x700:0x708:default-extended
telemetry preflight dids --endpoint 700-708-extended --did 0x1075 --did 0x1090
```

`discover endpoints` opens the adapter, replays the named reviewed session
recipe (`config/transport.json` `session_recipes`) against each candidate
`TESTER_CAN_ID:RESPONSE_CAN_ID:SESSION_RECIPE_ID`, and records whether it
responded as a provisional fact — it never edits tracked transport. `preflight
dids` does the same against `--endpoint` (a confirmed endpoint id, or a
`tester-response-session-recipe` candidate id printed by a prior responding
discovery run) plus one
`--did` read each, recording support/negative/timeout outcomes as provisional
facts — it never authors a collection objective.

Both commands' results are provisional SQLite facts with no durable source:
`telemetry cache rebuild` does not repopulate them, so re-run either command
after a rebuild if you still need the facts. Promoting a responding discovery
candidate into a tracked confirmed endpoint is the separate `[t] endpoint
curation` action in `telemetry-tui`. The operator supplies ECU keys and tracked
provenance, reviews the resulting endpoint, and types `SAVE` before the TUI
atomically writes `config/transport.json`; there is no CLI shortcut that
constructs a new session or dynamic-DID definition, matching the read-only,
reviewed-bytes-only safety boundary.

The probe parent derives a finite wall-clock deadline from the tracked initial
response and response-pending budgets plus a three-second process margin. It
kills a worker that exceeds that bound and also terminates the child if the
operator interrupts the command.

## Live collection

After inspecting the admitted plan, resolved transaction scope, and bounds:

```powershell
telemetry collect
```

`collect` is inherently live and has no mode flag. It verifies the current
source hashes, prints adapter/endpoint/output/plan scope, and then launches one
private adapter worker. Python owns scheduling, decoding, objective feedback,
capture rotation, heartbeat, and stop; C# owns only adapter, CAN, ISO-TP, and
session lifetime.

Use `telemetry-tui` for a glanceable status and one stop path. Do not
operate a computer while driving; use a passenger or unattended logger.
