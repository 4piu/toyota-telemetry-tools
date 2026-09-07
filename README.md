# Toyota diagnostic telemetry tools

The programs behind [`70Y074-telemetry`](https://github.com/4piu/70Y074-telemetry),
the template a vehicle repository is created from: a dependency-free Python
package, two vehicle-facing backends, and the extraction utilities that turn an
OEM database into candidate telemetry.

Nothing here is specific to a vehicle. Endpoints, session setup, DID selection,
and tier policy come from a platform repository's configuration; this
repository supplies the machinery that reads it.

## Install into a platform project

A project created from the [template](https://github.com/4piu/70Y074-telemetry)
carries a `bootstrap.ps1` that does this for you — see its README. Manually,
from a platform repository that already has a `telemetry-project.toml`:

```powershell
git clone https://github.com/4piu/70Y074-telemetry-tools.git ..\telemetry-tools
..\telemetry-tools\tools\setup-platform-project.ps1
.\.venv\Scripts\Activate.ps1
```

The setup script reads the marker, checks this repository out at the pinned
revision, builds the backends into the project's ignored `scratch/bin/`, and
installs the package into the project's `.venv`. It needs the network only when
the pin moves; `-Offline` makes that a rule.

`telemetry-project.toml` pins one revision of this repository — the same
checkout the backends are built from, so the Python tools, the backends, and
the Techstream scripts are always the revision reviewed. Start a platform
repository from the template rather than from here; this repository has no
project structure of its own to copy.

## Commands

| Command | Purpose |
|---|---|
| `telemetry-build-plan` | Build a bounded, tiered DID plan from a catalog and platform profile. |
| `telemetry-probe` | Probe every ECU endpoint the catalog knows, and write configuration for those that answer. |
| `telemetry-validate` | Reconstruct progress and run qualification, road, or experiment batches through a configured backend. |
| `telemetry-analyze-capture` | Independently validate and summarize one capture file. |
| `telemetry-review` | Aggregate every capture into a per-signal evidence ledger; list what still needs excitation; check a curated map against the observations. |
| `telemetry-map` | Emit a complete signal map from the catalog annotated with observations, plus the DIDs a vehicle refused. |
| `telemetry-migrate-legacy` | Temporary pre-0.4 converter for the reviewed one-time migration of legacy project evidence. |

Every command prints a human summary by default, `--json` for the machine
document, and reports its version with `--version`. See
[`tools/README.md`](tools/README.md) for the shared output options and the
Techstream encoding contract.

`telemetry-migrate-legacy` is intentionally transitional. It reads legacy
schema-1 captures without modifying them, writes only below an explicit ignored
`scratch/` destination, and emits a privacy-minimized reconciliation report.
Converted evidence must be reviewed before cutover. The command and every old
schema reader will be removed once that migration is accepted; it is not a
compatibility path.

The converter preserves each legacy run's collection settings only as a
non-authoritative snapshot. It does not turn legacy tiers, periods, or sample
limits into current objectives: the converted collection policy starts empty,
and planning cannot authorize live collection until reviewed targets are
added. Existing observations remain available across later policy changes.

Phase-D development now includes a pure deterministic scheduler service. It
accepts one admitted endpoint's acquisition rows and an explicit monotonic
clock, making deadline, priority, slack-fill, failure-backoff, and no-catch-up
behavior testable without opening an adapter. It is not connected to the
legacy live command path.

The native capture service writes an immutable run around that scheduler:
admitted-plan provenance, create-new JSONL segments, continuous run sequence
numbers across rotation, bounded storage, clean stop/transport-loss prefixes,
and explicit offline recovery that leaves the crashed source untouched. Native
capture services are still offline-tested and are not yet connected to an
adapter command.

## Guides

- **[Quickstart](docs/quickstart.md)** — the command sequence end to end.
- [Progressive validation](docs/progressive-validation.md) — the capture loop
  in detail: plans, qualification, road batches, experiments, and the evidence
  ledger.
- [Adding an adapter](docs/adding-an-adapter.md) — implementing the backend
  contract for a device that is not PCAN, J2534, or gs_usb.

Method — what to record, how transport is reconstructed, how a formula is
decoded, what each evidence state claims — lives in the
[template](https://github.com/4piu/70Y074-telemetry), not here.

## Backends

[`backends/`](backends/README.md) holds the programs that talk to a vehicle:
configuration-driven clients for PCAN-Basic, J2534, gs_usb (candleLight_fw
and its clone family), and slcan (LAWICEL over a CDC-ACM serial port) that
implement the runner's executable contract. They build with the C# compiler
shipped with Windows and need no SDK.

You should not have to write C# to use one on a new vehicle — endpoint, session
setup, and DID range come from a `backend-config.json` the project supplies.
Adding a different adapter means implementing one seven-member interface.

## Releases

Each release is tagged, and the [changelog](CHANGELOG.md) records what a bump
costs a platform repository: entries marked **Breaking** need a change there
before its commands will run again. Pin a tag; a branch is not a pin.

## Repository contract

- `src/` is the Python package; it has no third-party dependencies.
- `backends/` contains the vehicle-facing clients and their shared transport core.
- `tools/` contains configurable extraction and analysis utilities.
- `examples/` demonstrates the method with sanitized vehicle evidence.
- No vehicle conclusions live here. A DID, an endpoint, or a formula that is
  true of one car belongs in that car's platform repository.
