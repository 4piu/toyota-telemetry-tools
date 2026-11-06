# Toyota diagnostic telemetry tools

A standalone toolkit for finding, verifying, and collecting diagnostic
telemetry from one vehicle: a dependency-free Python package with a command
line and a desktop application, four vehicle-facing adapter workers, and
extraction utilities that turn an OEM database into candidate telemetry.

Nothing here is specific to a vehicle. Endpoints, session setup, DID selection,
and collection targets live in a project workspace; this repository supplies
the machinery that reads it and the [method](docs/method/README.md) behind it.

Everything it sends is read-only diagnostics (tester present, session control,
and ReadDataByIdentifier), checked against a built-in allowlist before it
reaches the bus. It never writes, codes, clears, or unlocks anything.

> **Not affiliated with Toyota.** Toyota and Techstream are trademarks of their
> owners and are named only to describe what the tools work with. No Toyota
> software or data is included. Importing from Techstream requires your own
> licensed installation. Talking to a vehicle carries risk; use this at your
> own risk, never while driving, and never as an input to vehicle control or
> a safety warning.

## Requirements

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- Windows for the adapter workers and the Techstream import. They build with
  the .NET Framework C# compiler that ships with Windows; no SDK is needed.
  The command line and desktop application run elsewhere, but cannot reach a
  vehicle there.
- A CAN adapter: PEAK PCAN-USB (or compatible), a J2534 pass-thru, a
  candleLight/gs_usb device, or an slcan serial adapter.

## Install

```powershell
git clone https://github.com/4piu/70Y074-telemetry-tools.git
.\70Y074-telemetry-tools\backends\build.ps1        # Windows adapter workers
uv tool install --editable ".\70Y074-telemetry-tools[gui]"
```

This puts `telemetry` and `telemetry-ui` on the path. The workers are built
into the package (`src/toyota_telemetry/workers/`), so every project uses the
installed ones; a wheel built from the checkout bundles them. Omit `[gui]` for
the command line only.

## Workspace and deliverables

A project is one **workspace** folder, created by the tool and kept outside
any repository:

```powershell
telemetry init C:\work\my-car-workspace --profile-id my-car-profile
```

It holds everything the tool works on, including private data: the
transport, collection policy, signal review, candidate list, recordings, and
the local cache. Results leave it only by export:

```powershell
telemetry export C:\work\my-car-deliverables
```

writes the reviewed result (a new immutable generation plus
`result/current.json`) and exact copies of the curated inputs (`config/`,
`review/`) into a **deliverable** folder, typically the vehicle's Git
repository, which also holds the written findings and evidence. Recordings,
connector settings, and the cache never leave the workspace. In the desktop
application these are Project > New project and Project > Export result.

## Two interfaces

The toolkit offers two complete, independent user interfaces over one library
facade (`toyota_telemetry.operator.OperatorService`):

- `telemetry` — the command-line interface, for terminals and scripts;
- `telemetry-ui` — the cross-platform PySide6 desktop application.

Either one can take a project from an empty candidate list to analyzed
recordings without the other. Neither launches or parses the other; a test
proves that every operation exists in both. The [quickstart](docs/quickstart.md)
walks the workflow with the command and desktop page for each step.

| Step | Command | Desktop page |
|---|---|---|
| Readiness and next action | `telemetry status`, `telemetry check` | step bar (the next step's tooltip) |
| Connector | `telemetry connector show\|set\|check` | Setup |
| Candidate list | `telemetry source import file\|tis [--apply]`, `telemetry cache rebuild` | Setup |
| Browse | `telemetry signals [SELECTOR...]`, `telemetry signals show SELECTOR` | Signals |
| Endpoints | `telemetry endpoint add\|confirm` | Signals |
| Verify (LIVE) | `telemetry verify SELECTOR... [--dry-run] [--yes]` | Signals |
| Review and targets | `telemetry signal set`, `telemetry did set`, `telemetry target set\|clear` | Signals |
| Collect (LIVE) | `telemetry collect [--dry-run] [--yes]` | Collect |
| Recordings | `telemetry recordings [show\|recover\|trash\|export]` | Recordings |
| Workspace | `telemetry init FOLDER` | Project > New project |
| Deliverables | `telemetry export FOLDER` | Project > Export result |

Only `verify` and `collect` transmit. Both show the exact scope first; the CLI
asks for `yes` in a terminal, and a scripted `--yes` is accepted only for a
scope identical to the last recorded `--dry-run`. The desktop application uses
a confirmation dialog whose default button is Cancel. One live job may own a
project at a time, whichever interface started it. Verification and selectors
never sweep CAN ID or DID ranges: they act only on the finite selection shown.

Every command prints a human summary by default and accepts `--json`; live
commands emit versioned JSON Lines. Exit codes are 0 success, 1 failure, 2
usage error, and 3 refused by confirmation or safety rules.

### Desktop application

```powershell
uv sync --extra gui
uv run --extra gui telemetry-ui [PROJECT]
```

A step bar across the top is both the navigation and the progress view: each
page shows what is done, what is next, and what needs attention. Four pages:
Setup (project check, connector, candidate sources), Signals (the collection
plan: an ECU → DID → signal table showing what is readable, what is
collected and how often, and each signal's target; a Filter menu and search;
a side panel edits the settings, targets, and definition of everything
selected, showing "mixed" where rows differ, as a draft until Save), Collect
(automatic readiness, start/stop, live progress, and every target's
progress), and Recordings (runs by start time; selecting runs lists and
plots their signals; recovery, Delete to the system trash, and an Export
dialog for samples). Export
result is in the Project menu. The plot
follows dashboard conventions: hover reads the nearest sample, click pins it,
drag zooms into a time range, double-click shows everything, sideways scroll
pans, and pinch or Ctrl+scroll zooms time. Buttons are short;
descriptions and disabled reasons are in tooltips. The default install
remains dependency-free; importing the core package does not import Qt, NumPy,
or PyQtGraph.

Windows packaging is a separate development operation so compiler/deployment
tools do not enter either runtime dependency set. The tested command uses
Nuitka directly because `pyside6-deploy` persists environment-specific paths
in its generated spec and does not forward PyQtGraph's dynamically imported Qt
modules reliably across isolated environments:

```powershell
uv run --isolated --python 3.13 --extra gui --group package python -m nuitka `
  src/toyota_telemetry/ui/app.py --enable-plugin=pyside6 --standalone `
  --assume-yes-for-downloads --noinclude-qt-translations `
  --include-qt-plugins=platforminputcontexts `
  --include-module=PySide6.QtOpenGL `
  --include-module=PySide6.QtOpenGLWidgets `
  --windows-console-mode=force --output-dir=scratch/operator-bundle `
  --output-filename=telemetry-ui.exe
```

The isolated Python 3.13 environment is intentional: current Nuitka 2.8 does
not compile against Python 3.14's changed internal garbage-collector layout.
This packaging constraint does not limit the installed application or core
toolkit, which are tested on Python 3.14.

### Collection internals

The scheduler is pure and deterministic: one endpoint, an explicit monotonic
clock, deadline, priority, slack-fill, failure-backoff, and no-catch-up
behavior testable without an adapter. The capture writer produces immutable
runs with continuous sequence numbers across rotated segments, bounded
storage, clean stop/transport-loss prefixes, and explicit offline recovery
that leaves the crashed source untouched. Python owns scheduling, decoding,
target feedback, capture, heartbeat, and stop; one private C# worker owns only
the adapter, CAN, ISO-TP, and session lifetime, resolves each transaction by
identifier against its launch-time table, and passes every request through the
built-in, vehicle-agnostic telemetry guard. The collector/worker integration is
covered by synthetic offline tests and has run multi-hour road collections.

## Guides

- **[Quickstart](docs/quickstart.md)** — the workflow end to end, in both interfaces.
- [Progressive validation](docs/progressive-validation.md) — the capture loop
  in detail: targets, readiness, mixed-cadence collection, and evidence.
- [Adding an adapter](docs/adding-an-adapter.md) — implementing the backend
  contract for a device that is not PCAN, J2534, or gs_usb.

- [Method](docs/method/README.md) — what to record, how transport is
  reconstructed, how a formula is decoded, what each evidence state claims,
  and what belongs in a deliverable repository.

## Backends

[`backends/`](backends/README.md) holds the programs that talk to a vehicle:
private workers for PCAN-Basic, J2534, gs_usb (candleLight_fw
and its clone family), and slcan (LAWICEL over a CDC-ACM serial port) that
implement the collector's transaction contract. They build with the C# compiler
shipped with Windows and need no SDK.

You should not have to write C# to use one on a new vehicle—endpoint, session,
and safety facts come from the workspace's transport settings, while local device
binding comes from machine-local settings in the workspace.
Adding a different adapter means implementing one seven-member interface.

## Repository contract

- `src/` is the Python package; its core has no third-party dependencies and
  the optional `gui` extra adds the desktop application.
- `backends/` contains the vehicle-facing clients and their shared transport core.
- `tools/` contains configurable extraction and analysis utilities.
- `docs/method/` is the vehicle-independent method.
- No vehicle conclusions live here. A DID, an endpoint, or a formula that is
  true of one car belongs in that car's workspace and deliverable repository.

## License

[MIT](LICENSE). The bundled `libusb-1.0.dll` is LGPL-2.1; see
[its notice](backends/Telemetry.Backend.Gsusb/runtime/README.md).
