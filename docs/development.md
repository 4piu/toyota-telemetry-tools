# Development

## Layout

- `src/toyota_telemetry/` is the Python package. Its core has no third-party
  dependencies; the optional `gui` extra adds the desktop application, and
  importing the core never imports Qt, NumPy, or PyQtGraph.
- `backends/` holds the C# vehicle-facing workers, their shared CAN/ISO-TP
  core, and the Techstream database helper ([details](../backends/README.md)).
- `tools/` holds offline utilities ([details](../tools/README.md)).
- `docs/` holds the user guides.

No vehicle facts live here. A DID, an endpoint, or a formula that is true of
one vehicle belongs in that vehicle's workspace.

## Two interfaces, one service

`telemetry` (command line) and `telemetry-ui` (PySide6 desktop application)
are complete, independent interfaces over one library facade,
`toyota_telemetry.operator.OperatorService`. Neither launches or parses the
other, and a test checks that every operation exists in both.

## Build and test

```powershell
.\backends\build.ps1                       # workers into src/toyota_telemetry/workers/
uv sync --extra gui --group test
uv run --extra gui --group test pytest
uv run --extra gui telemetry-ui            # run the desktop application from the checkout
```

The workers build with the .NET Framework C# compiler that ships with
Windows. Each supports `--self-test`, which checks ISO-TP framing, the
built-in telemetry guard, and the worker protocol without opening a device.
After pulling changes to `backends/`, rebuild the workers; the editable
install picks up Python changes directly. A wheel built from the checkout
bundles the built workers.

`pyproject.toml` is the version authority; keep `uv.lock` in step with it.

## Collection internals

The scheduler is pure and deterministic: one endpoint, an explicit monotonic
clock, deadline, priority, slack-fill, failure-backoff, and no-catch-up
behavior, all testable without an adapter. The capture writer produces
immutable runs with continuous sequence numbers across rotated segments,
bounded storage, clean stop and transport-loss prefixes, and explicit offline
recovery that leaves a crashed source untouched.

Python owns scheduling, decoding, target feedback, capture, heartbeat, and
stop. One private C# worker owns only the adapter, CAN, ISO-TP, and the
session lifetime. It resolves each transaction by identifier against its
launch-time table and passes every request through the built-in,
vehicle-agnostic telemetry guard. The collector/worker integration is covered
by synthetic offline tests and has run multi-hour road collections.

To support another adapter, see [adding an adapter](adding-an-adapter.md).

## Windows packaging

Packaging is a separate development operation, so compiler and deployment
tools stay out of both runtime dependency sets. The tested command uses
Nuitka directly, because `pyside6-deploy` persists environment-specific paths
in its generated spec and does not reliably forward PyQtGraph's dynamically
imported Qt modules across isolated environments:

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

The isolated Python 3.13 environment is intentional: Nuitka 2.8 does not
compile against Python 3.14's changed garbage-collector layout. This limits
only packaging; the installed application and core toolkit are tested on
Python 3.14.
