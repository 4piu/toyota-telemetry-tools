# Toyota diagnostic telemetry field guide

This repository is a reusable, read-only workflow for finding, decoding, and validating manufacturer-specific Toyota telemetry. It treats the OEM diagnostic path as structured evidence: begin with a named Data List value, recover its database and transport metadata, then verify the smallest possible diagnostic exchange.

It is not a vehicle database or dashboard. Vehicle-specific profiles and results belong in a platform repository; acquisition applications consume those reviewed results later.

## Choose a route

| What you have | Start here |
|---|---|
| Techstream and a working J2534 VIM | [Capture the Techstream/J2534 conversation](docs/03-locate/j2534-capture.md) |
| A local Techstream installation but no working VIM | [Query the local database](docs/03-locate/techstream-database.md) |
| A saved `.TSE` session | [Inventory a saved session](docs/03-locate/saved-session.md) |
| A candidate DID and a USB-CAN adapter | [Verify through USB-CAN](docs/06-verify/usb-can.md) |
| An ambiguity left by data and traces | [Use focused static analysis](docs/03-locate/static-analysis.md) |
| An unfamiliar adapter | [Add an adapter backend](docs/08-adapters/adding-an-adapter.md) |
| A confirmed endpoint and candidate catalog | [Run progressive validation](docs/06-verify/progressive-validation.md) |

Read [scope and safety](docs/00-scope-safety.md) before transmitting. The complete sequence and evidence states are indexed in [the documentation map](docs/README.md).

## Reusable toolkit

The repository is also a dependency-free Python package. With Python 3.11+ and
`uv`, `uv sync` installs three commands:

- `telemetry-build-plan` creates a bounded, privacy-aware DID plan from a
  catalog and platform profile;
- `telemetry-validate` reconstructs progress and runs qualification/road batches
  or configuration-hashed custom experiments through a configured backend;
  custom experiments may finish at a time bound or by a validated manual stop;
- `telemetry-analyze-capture` independently validates and summarizes the common
  JSONL capture format.

See [progressive validation](docs/06-verify/progressive-validation.md) for the
bootstrap sequence and [the adapter contract](docs/08-adapters/adding-an-adapter.md)
for connecting PCAN, J2534, SocketCAN, or another transport implementation.

## Repository contract

- `docs/` contains modular methods, not vehicle conclusions.
- `tools/` contains configurable extraction and analysis utilities.
- `templates/` defines portable experiment, profile, signal, and allowlist records.
- `examples/` demonstrates the method with sanitized vehicle evidence.

Generated plans, raw captures, reverse-engineering state, and private analysis use the repository-local `scratch/` directory. The whole directory is ignored so a project created from the template remains self-contained without relying on a specially named sibling directory.

Raw captures, OEM databases, binaries, Ghidra projects, VINs, and machine-local configuration do not belong in this repository. See [PRIVATE_DATA.md](PRIVATE_DATA.md).

For a vehicle-specific repository, follow the [platform-project contract](docs/09-platform-projects.md). It defines the project marker, versioned toolkit relationship, and repository-local scratch workspace.

The techniques describe observed software and database formats, not a supported Toyota API. Revalidate every candidate on the exact ECU calibration and obey applicable licenses and law.
