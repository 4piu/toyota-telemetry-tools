# Private CAN adapter workers

The C# executables are private implementation details of live collection and
verification (`telemetry collect` / `telemetry verify`, or the desktop
application's Collect and Verify actions). Each accepts
exactly two live process forms:

- `--worker --config PATH` — the long-lived collect worker. It keeps one
  adapter channel, ISO-TP conversation, and diagnostic session alive for one
  endpoint, accepting only ordered transaction identifiers from the Python
  collector through an exact JSON-Lines protocol and resolving each one
  against its own immutable launch-time transaction table.
- `--probe --config PATH` — the bounded, single-shot probe used by verification
  (endpoint and DID probes). It opens a channel, attempts a fixed session-setup sequence
  and optionally one ReadDataByIdentifier, prints exactly one JSON result
  line, and exits. It takes no stdin commands.

Both forms pass every resolved request through the same built-in,
vehicle-agnostic telemetry guard at the vehicle-facing boundary before it is
ever transmitted, independent of whichever table or config produced it.

The worker does not own scheduling, decoding, target progress, capture
files, public status, or operator interaction. It permits one outstanding
request and supports stop while a response is pending. The probe owns none of
that either: it has no scheduler, no capture writer, and no retry logic — one
bounded attempt per invocation.

## Supported adapters

| Executable | Adapter API |
|---|---|
| `telemetry-pcan.exe` | PEAK PCAN-Basic and compatible devices |
| `telemetry-j2534.exe` | SAE J2534 PassThru DLLs |
| `telemetry-gsusb.exe` | candleLight/gs_usb over WinUSB or libusb |
| `telemetry-slcan.exe` | LAWICEL/slcan over a serial port |

`build.ps1` writes the workers into the Python package
(`src/toyota_telemetry/workers/`), where the tool finds them; a connector may
name another executable instead. Adapter paths, channels, and serial-device
names belong in the workspace's machine-local `state/local-settings.json`.
Portable endpoints, session steps, timing bounds, and dynamic-DID definitions
belong in its `config/transport.json`. Python composes those authorities with
the admitted plan into a temporary exact worker config.

## Build and offline verification

```powershell
.\build.ps1 -OutputDirectory .\bin\Release
.\bin\Release\telemetry-pcan.exe --self-test
.\bin\Release\telemetry-j2534.exe --self-test
.\bin\Release\telemetry-gsusb.exe --self-test
.\bin\Release\telemetry-slcan.exe --self-test
.\bin\Release\telemetry-tisdb.exe --self-test
```

Self-test checks ISO-TP framing, the built-in telemetry guard, and
worker/probe protocol schema handling without opening a device. Unknown
command-line arguments fail. The only other accepted process forms are the
private `--worker --config PATH` protocol launched by collection and the
private `--probe --config PATH` protocol launched by verification; operators
should not invoke either directly.

## Techstream database helper

`telemetry-tisdb.exe` (`Telemetry.TisDb/`) is not a worker and never talks to
a vehicle. `telemetry source import tis` runs it to read an installed
Techstream database through Techstream's own `KgpDataCtrl.dll`, which is
32-bit, so the helper builds x86 and without the CAN core. It writes one JSON
record per line: the ECU catalog, data-monitor records, protocol CAN IDs,
physical-conversion groups, and unit strings. Run it with `--help` for the
query forms; `--self-test` checks argument parsing, record output, and code
page 932 text without opening a database.

## Safety boundary

- Classical CAN, standard 11-bit identifiers, and normal ISO-TP addressing
  only.
- One endpoint and one in-flight transaction per worker.
- Runtime transactions are selected by identifier from the worker's own
  immutable table; the wire protocol never resupplies request bytes.
- The built-in telemetry guard recognizes only TesterPresent, default/extended
  DiagnosticSessionControl, a bounded DefineByIdentifier dynamic-DID
  definition, and ReadDataByIdentifier. No vehicle control, coding, clearing,
  security access, memory access, or programming operation is a recognized
  shape, regardless of what the transaction table contains.
- DefineByIdentifier requires one or more complete source-DID/position/size
  tuples; truncated tuples and zero/overflowing ranges are rejected.
- No adapter auto-detection or fallback between bindings.
- No automatic CAN ID or DID sweep: verification only ever probes the finite
  endpoint and DID selection the operator confirmed. The Python parent enforces a wall-clock deadline derived from
  the tracked per-step response bounds and terminates a stuck probe worker.

See [adding an adapter](../docs/adding-an-adapter.md) for the narrow channel
interface and test expectations.
