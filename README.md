# Toyota diagnostic telemetry tools

The programs behind [`70Y074-telemetry`](https://github.com/4piu/70Y074-telemetry),
the template a vehicle repository is created from: a dependency-free Python
package, four vehicle-facing adapter workers, and extraction utilities that turn an
OEM database into candidate telemetry.

Nothing here is specific to a vehicle. Endpoints, session setup, DID selection,
and objective policy come from a platform repository's configuration; this
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
| `telemetry catalog import` | Import normalized catalog rows and rebuild the private derived cache. |
| `telemetry cache rebuild` | Rebuild the private derived cache from authored inputs, catalog rows, and captures. |
| `telemetry discover endpoints` | Explicitly live: probe operator-named candidate tester/response CAN ID pairs, replaying a reviewed session recipe, for presence. Records provisional facts only; never edits tracked transport. |
| `telemetry preflight dids` | Explicitly live: probe operator-named candidate DIDs against a confirmed endpoint or a responding discovery candidate. Records provisional facts only; never authors a collection objective. |
| `telemetry plan` | Compile and admit collection work offline. |
| `telemetry collect` | Execute one current admitted endpoint plan through the private adapter worker. This verb is live; there is no `--live` option. |
| `telemetry capture inspect` | Validate and summarize the private capture tree without printing payload bytes. |
| `telemetry export` | Generate the tracked, privacy-minimized project result from authored sources and private evidence. |

`telemetry discover endpoints` and `telemetry preflight dids` never sweep an
ID range automatically: every candidate CAN ID pair (`--candidate
TESTER:RESPONSE:SESSION_RECIPE_ID`, repeatable) and every candidate DID
(`--did 0xNNNN`, repeatable) is named explicitly, keeping each run a small,
bounded, reviewable set of transactions. Both use the same built-in telemetry
guard and single-shot `--probe` worker protocol as `telemetry collect`'s
long-lived worker; results are provisional SQLite facts with no durable
source, so `telemetry cache rebuild` does not repopulate them. Promoting a
responding discovery candidate into a tracked confirmed endpoint remains a
separate explicit curation operation over the
`toyota_telemetry.discovery.promote_confirmed_endpoint` library service; it is
not a probing side effect or a CLI shortcut.

Finite data commands print a human summary by default and accept `--json` for
one machine document. `telemetry collect --json` emits versioned JSON Lines.
Use `telemetry --version` for the package version.

The scheduler service is pure and deterministic. It
accepts one admitted endpoint's acquisition rows and an explicit monotonic
clock, making deadline, priority, slack-fill, failure-backoff, and no-catch-up
behavior testable without opening an adapter.

The native capture service writes an immutable run around that scheduler:
admitted-plan provenance, create-new JSONL segments, continuous run sequence
numbers across rotation, bounded storage, clean stop/transport-loss prefixes,
and explicit offline recovery that leaves the crashed source untouched.

Collection uses Python `telemetry collect` as the public scheduler,
decoder, capture, heartbeat, and stop owner. It launches one private long-lived
C# worker for the selected endpoint; that worker owns only the adapter, CAN,
ISO-TP, and session lifetime, resolves each runtime transaction by identifier
against its own immutable launch-time table, and passes every resolved
request through the built-in, vehicle-agnostic telemetry guard before
transmission. The collector/worker integration is covered by synthetic offline
tests. It has not yet been exercised on a vehicle.

## Desktop prototype

The optional cross-platform PySide6 operator application has an offline
interaction prototype. Install the GUI extra and launch it without a project:

```powershell
uv sync --extra gui
uv run --extra gui telemetry-ui
```

An existing project path may be supplied as the positional argument. The
prototype remains entirely offline. Its second workflow draft uses the task
destinations Overview, Connect hardware, Build signal list, Verify with
vehicle, Set collection targets, Collect data, and Analyze recordings. It
previews TIS/file/future-J2534 source selection, hierarchical endpoint/DID
verification, visually grouped DID signals, direct completion requirements,
automatic collection readiness, overall run progress, bounded evidence, and
advanced scheduling diagnostics. Safe local mock interactions work; live and
project-mutating actions are visibly disabled. The candidate table still
virtualizes 10,000 rows and analysis reduces a synthetic million-point series
to a viewport-bounded PyQtGraph view. The prototype does not load project
authorities, open an adapter, transmit, save edits, compile a real collection
scope, or collect. Those services are added in the gated slices documented by
the workspace operator-UI specification.

The default install remains dependency-free and `telemetry` remains the
headless/automation interface. Importing the core package does not import Qt,
NumPy, or PyQtGraph.

Windows packaging is a separate development operation so compiler/deployment
tools do not enter either runtime dependency set. The tested prototype command
uses Nuitka directly because `pyside6-deploy` persists environment-specific
paths in its generated spec and does not forward PyQtGraph's dynamically
imported Qt modules reliably across isolated environments:

```powershell
uv run --isolated --python 3.13 --extra gui --group package python -m nuitka `
  src/toyota_telemetry/ui/app.py --enable-plugin=pyside6 --standalone `
  --assume-yes-for-downloads --noinclude-qt-translations `
  --include-qt-plugins=platforminputcontexts `
  --include-module=PySide6.QtOpenGL `
  --include-module=PySide6.QtOpenGLWidgets `
  --windows-console-mode=force --output-dir=scratch/operator-bundle `
  --output-filename=telemetry-ui-prototype.exe
```

The isolated Python 3.13 environment is intentional: current Nuitka 2.8 does
not compile against Python 3.14's changed internal garbage-collector layout.
This packaging constraint does not limit the installed application or core
toolkit, which are tested on Python 3.14.

## Guides

- **[Quickstart](docs/quickstart.md)** — the command sequence end to end.
- [Progressive validation](docs/progressive-validation.md) — the capture loop
  in detail: objectives, admission, mixed-cadence collection, and evidence.
- [Adding an adapter](docs/adding-an-adapter.md) — implementing the backend
  contract for a device that is not PCAN, J2534, or gs_usb.

Method — what to record, how transport is reconstructed, how a formula is
decoded, what each evidence state claims — lives in the
[template](https://github.com/4piu/70Y074-telemetry), not here.

## Backends

[`backends/`](backends/README.md) holds the programs that talk to a vehicle:
private workers for PCAN-Basic, J2534, gs_usb (candleLight_fw
and its clone family), and slcan (LAWICEL over a CDC-ACM serial port) that
implement the collector's transaction contract. They build with the C# compiler
shipped with Windows and need no SDK.

You should not have to write C# to use one on a new vehicle—endpoint, session,
and safety facts come from tracked transport authority, while local device
binding comes from ignored settings.
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
