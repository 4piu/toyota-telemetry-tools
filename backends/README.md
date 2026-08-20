# Validation backends

These are the programs that actually talk to the vehicle. `telemetry-validate`
never opens a CAN device itself — it builds a bounded, allowlisted command and
runs one of these per batch.

You need exactly one working backend for your adapter. Both shipped backends
are configuration-driven: **you should not need to edit any C# to use one on a
new vehicle.** Everything vehicle-specific lives in a `backend-config.json`
that you write.

## Which backend

| Backend | Adapter | Process |
|---|---|---|
| `telemetry-pcan` | PEAK PCAN-USB, or any adapter whose firmware the PEAK driver enumerates as a PCAN channel (for example a CANable flashed with PCAN-compatible firmware) | x64 |
| `telemetry-j2534` | A J2534 / PassThru interface with a vendor DLL | x86 by default; x64 if the vendor DLL is 64-bit |

Both operate the adapter in **raw CAN** mode and run the same
application-managed ISO-TP layer. That is deliberate: it keeps every backend
byte-identical on the bus, so a result from one adapter can be compared with a
result from another. Letting a J2534 adapter do ISO-TP internally would make
transport behaviour vendor-specific and the comparison meaningless.

## Build

No SDK is required. The build uses the C# compiler that ships with Windows.

```powershell
.\build.ps1                      # both backends into backends\bin\Release
.\build.ps1 -Backend pcan        # just one
.\build.ps1 -J2534Platform x64   # for a 64-bit vendor DLL
```

Build straight into your platform project's ignored scratch directory so the
binary is never committed and never assumed to sit in a sibling checkout:

```powershell
.\build.ps1 -OutputDirectory C:\path\to\my-platform-project\scratch\bin
```

Then point the platform's `validation-profile.json` at it:

```json
"backend": {
  "schema_version": 1,
  "executable": "scratch/bin/telemetry-pcan.exe",
  "arguments": ["--config", "data/config/backend-config.json"],
  "mode_arguments": {
    "qualification": ["--candidate-qualify", "--live"],
    "road": ["--candidate-batch", "--live"]
  }
}
```

## Verify before connecting to a vehicle

```powershell
.\bin\Release\telemetry-pcan.exe --self-test --output selftest.jsonl
```

This opens no device. It asserts ISO-TP segmentation byte-for-byte (single
frame, first frame, both consecutive-frame boundaries), checks that the
transmit allowlist accepts what it should and rejects everything else, and
writes a synthetic capture so the JSONL writer is exercised end to end. Run it
after every build and after any change to the core.

Confirm the toolkit agrees with the output:

```powershell
telemetry-analyze-capture selftest.jsonl --ecu-key 0 --minimum-samples 1
```

## Bring-up order

Do these in order the first time on any vehicle. Each step isolates a
different failure, which matters because a wiring fault and a gateway routing
failure look identical from the application.

```powershell
# 1. Config parses, endpoint reads back correctly, nothing transmitted.
.\telemetry-pcan.exe --config data\config\backend-config.json --probe

# 2. Standardized OBD query. Proves bit timing, TX, RX, filtering, and wiring
#    without depending on any manufacturer-specific endpoint.
.\telemetry-pcan.exe --config data\config\backend-config.json --obd-probe --live

# 3. One tester-present to your configured endpoint. Proves gateway routing.
.\telemetry-pcan.exe --config data\config\backend-config.json --probe --live
```

If step 2 fails, the problem is electrical or bit timing — not the ECU. A
blinking adapter LED and an OK controller status are both consistent with
completely miswired CAN-L and ground.

After step 3 passes, use `telemetry-validate` rather than calling the backend
directly; it owns batching, progress, and validation.

## Writing `backend-config.json`

Copy [`templates/backend-config.json`](https://github.com/4piu/70Y074-telemetry/blob/main/data/config/backend-config.json). The
fields that need real values:

| Field | How to obtain it |
|---|---|
| `transport.bitrate` | The vehicle's diagnostic CAN bitrate, commonly 500000. |
| `transport.tester_can_id` / `response_can_id` | The diagnostic endpoint for your target ECU. |
| `session_setup` | **The hard part — see below.** |
| `limits.did_min` / `did_max` | The DID range you are willing to schedule. |
| `device.pcan.channel` | `PCAN_USBBUS1` unless you have several adapters. |
| `device.j2534.dll` | Absolute path to the vendor PassThru DLL. |

`session_setup` is replayed in order before any DID is read, and each step's
response must start with its `expect` prefix. A step may not be a
ReadDataByIdentifier request; DIDs are supplied with `--did` so they appear in
the capture record.

### Recovering the session setup

There is currently **no automated way to derive the session-setup sequence for
a new platform.** This is the least-solved step in the whole workflow and the
one most likely to block a fork.

What worked here: capture a real tester conversation over J2534 while the
manufacturer's diagnostic application reads a live data list, then read the
setup requests off the trace. See
[capturing a J2534 conversation](https://github.com/4piu/70Y074-telemetry/blob/main/docs/03-locate/j2534-capture.md) and
[reconstructing the transport](https://github.com/4piu/70Y074-telemetry/blob/main/docs/04-reconstruct/diagnostic-transport.md),
and use [`tools/j2534/parse-j2534-log.py`](../tools/j2534/parse-j2534-log.py)
to turn the log into a request timeline.

The three-step tester-present / default-session / extended-session sequence in
the template is common on Toyota platforms and is a reasonable first guess, but
it is a guess. Do not assume it transfers. If your ECU answers a DID read
without any setup at all, an empty-looking session is still expressed as at
least one step — use tester present alone.

Better acquisition paths for this information are an open question; see
[open questions](https://github.com/4piu/70Y074-telemetry/blob/main/docs/open-questions.md).

## Adding a backend for another adapter

Implement [`ICanChannel`](Telemetry.Can.Core/ICanChannel.cs) — seven members —
and a `Main` that hands `CandidateRunner.Run` a factory. That is the entire
device-specific surface; ISO-TP, allowlists, capture logging, scheduling,
retries, and the CLI contract are all shared. `Telemetry.Backend.Pcan` is about
250 lines and is a reasonable model.

The behavioural requirements a channel must meet, and the acceptance test it
should pass, are in
[adding an adapter](../docs/adding-an-adapter.md).

## Layout

```text
Telemetry.Can.Core/       shared; no device or vehicle knowledge
  Json.cs                 strict minimal JSON reader
  BackendConfig.cs        config load and validation
  ICanChannel.cs          the device abstraction
  IsoTpTransport.cs       ISO-TP framing, UDS exchange, allowlists
  BoundedJsonlLog.cs      candidate-capture schema 1 writer
  CandidateRunner.cs      probes, qualification, batch capture
  Options.cs              the CLI contract
  SelfCheck.cs            offline byte-exact assertions
  Hex.cs                  byte/hex/JSON helpers

Telemetry.Backend.Pcan/   PCAN-Basic binding
Telemetry.Backend.J2534/  J2534 PassThru binding
```
