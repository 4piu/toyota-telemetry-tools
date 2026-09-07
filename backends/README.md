# Private CAN adapter workers

The C# executables are private implementation details of `telemetry collect`.
Each keeps one adapter channel, ISO-TP conversation, and diagnostic session
alive for one endpoint. It accepts only ordered transactions from the Python
collector through an exact JSON-Lines protocol and enforces the admitted
byte-exact request allowlist again at the vehicle-facing boundary.

The worker does not own scheduling, decoding, objective progress, capture
files, public status, or operator interaction. It permits one outstanding
request and supports stop while a response is pending.

## Supported adapters

| Executable | Adapter API |
|---|---|
| `telemetry-pcan.exe` | PEAK PCAN-Basic and compatible devices |
| `telemetry-j2534.exe` | SAE J2534 PassThru DLLs |
| `telemetry-gsusb.exe` | candleLight/gs_usb over WinUSB or libusb |
| `telemetry-slcan.exe` | LAWICEL/slcan over a serial port |

Adapter paths, channels, and serial-device names belong in the vehicle
project's ignored `scratch/local-settings.json`. Portable endpoints, session
steps, timing bounds, and dynamic-DID definitions belong in its tracked
`config/transport.json`. Python composes those authorities with the admitted
plan into a temporary exact worker config under ignored scratch.

## Build and offline verification

```powershell
.\build.ps1 -OutputDirectory .\bin\Release
.\bin\Release\telemetry-pcan.exe --self-test
.\bin\Release\telemetry-j2534.exe --self-test
.\bin\Release\telemetry-gsusb.exe --self-test
.\bin\Release\telemetry-slcan.exe --self-test
```

Self-test checks ISO-TP framing, payload/frame allowlists, and worker command
schema handling without opening a device. Unknown command-line arguments fail.
The only other accepted process form is the private
`--worker --config PATH` protocol launched by `telemetry collect`; operators
should not invoke it directly.

## Safety boundary

- Classical CAN, standard 11-bit identifiers, and normal ISO-TP addressing
  only.
- One endpoint and one in-flight transaction per worker.
- Session steps and requests must exactly match the temporary admitted worker
  config.
- No vehicle control, coding, clearing, security access, memory access, or
  programming operations.
- No adapter auto-detection or fallback between bindings.

See [adding an adapter](../docs/adding-an-adapter.md) for the narrow channel
interface and test expectations.
