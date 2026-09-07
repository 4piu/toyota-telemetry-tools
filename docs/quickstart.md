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
telemetry review
telemetry plan
telemetry export
```

`catalog import` and `cache rebuild` reconstruct the disposable SQLite cache.
`review` opens the offline-first operator interface; merely opening or
navigating it never starts a process. `plan` compiles policy and prior evidence
into a hashed admitted plan without opening an adapter. `export` creates one
privacy-minimized result generation from source authorities and all valid
captures.

An empty policy is valid but cannot authorize collection. Missing reviewed
cost, safety, decode, or applicability facts also block admission.

## Live collection

After reviewing the admitted plan and its byte-exact transmit scope:

```powershell
telemetry collect
```

`collect` is inherently live and has no mode flag. It verifies the current
source hashes, prints adapter/endpoint/output/plan scope, and then launches one
private adapter worker. Python owns scheduling, decoding, objective feedback,
capture rotation, heartbeat, and stop; C# owns only adapter, CAN, ISO-TP, and
session lifetime.

Use the operator interface for a glanceable status and one stop path. Do not
operate a computer while driving; use a passenger or unattended logger.

The bounded live qualification command remains intentionally unspecified while
its provisional-result and capture contract is reviewed. Do not use a removed
pre-0.4 probe program as a substitute.
