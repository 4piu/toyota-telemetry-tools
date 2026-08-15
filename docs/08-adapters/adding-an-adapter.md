# Add an adapter backend

Working backends for PCAN-Basic and J2534 ship in [`backends/`](../../backends/README.md).
Adding another transport means implementing `ICanChannel` — seven members — and a
`Main`; ISO-TP, allowlists, capture logging, and this CLI contract are shared.
Read this page for the behaviour a channel must provide.

An adapter changes transport, not signal meaning. Preserve a small boundary so the same acquisition state machine and decoder can be tested through multiple devices.

## Required backend behavior

- Open/close a configured channel and report actual bitrate/controller state.
- Send an 11/29-bit CAN frame with monotonic host timestamp.
- Receive frames with ID, DLC/data, timestamp, overflow/error indications.
- Apply the narrowest available hardware/software filters.
- Support listen-only where the controller permits it.
- Expose cancellation and deterministic cleanup.

ISO-TP can be above this boundary or supplied by the OS/adapter, but the choice and padding/addressing configuration must be explicit. A serial bridge protocol should be length-delimited with versioning, timestamps, error frames, and bounded buffers; SLCAN is useful for interoperability but can be less precise for timing/error reporting.

## Acceptance test

1. Dry-run produces no device open or frames.
2. Wrong profile, bitrate, endpoint, or non-allowlisted payload fails closed.
3. Known OBD request proves physical TX/RX where applicable.
4. One benign OEM tester-present validates gateway routing.
5. A known multi-frame response validates ISO-TP and flow control.
6. Raw payload and decoded values match another proven backend.
7. A stability run reports loss, timing, cancellation, and cleanup.

Document electrical isolation, transceiver voltage, termination, connector mapping, firmware/driver version, and whether it has been tested or is only planned.

## Progressive-validation executable contract

The generic `telemetry-validate` runner can invoke any backend described by the
platform's `validation-profile.json`. The configured executable is never opened
during dry-run or status commands. Live mode invokes it without a shell and
appends structured arguments.

Qualification invocation:

```text
<executable> <fixed args> <qualification mode args>
  --batch-id <id> --period-ms <n> --max-log-mib <n>
  --output <create-new.partial.jsonl> --did 0xNNNN
```

Road/extended-state invocation:

```text
<executable> <fixed args> <road mode args>
  --batch-id <id> --period-ms <n> --duration-seconds <n>
  --transport-retries <n> --max-log-mib <n>
  --output <create-new.partial.jsonl> --quiet
  --did 0xNNNN [--did 0xNNNN ...]
```

For a custom operator-completed experiment, `--duration-seconds 0` means no
time deadline. The log-size bound still applies, and the process is expected to
run until cancellation or a real error. A positive value remains a hard maximum.

The backend owns device access, CAN/ISO-TP, diagnostic-session setup, filters,
timeouts, response-pending handling, cancellation, and cleanup. It must reject
unconfigured endpoints and services independently of the runner. A successful
process writes candidate-capture schema 1:

- first record: `session_start`, `schema_version: 1`, matching `profile_id`,
  mode `candidate_qualification` or `candidate_batch`, ordered `dids`, transport
  metadata, and scheduling bounds;
- sample records: contiguous `sequence`, round-robin `did`, request/response
  monotonic milliseconds, latency, `request_hex`, `response_hex`, boolean
  `supported`, qualification status/NRC, and optional nonnegative `retry_count`;
- final record: exactly one `session_end`, reason `completed` or `user_stop`,
  total sample count, and exact per-DID counts.

Flush each JSONL record. On Ctrl+C, use `reason: user_stop`, flush the final
record, close the CAN interface, and return nonzero. The generic runner accepts
that reason as completion only for a custom experiment that meets its minimum
sample and integrity checks; qualification and progressive road runs remain
incomplete. On other errors, write an error/final record when possible and
return nonzero. Never claim a timeout is a negative UDS reply. The runner
performs independent structural validation before it promotes the partial
capture to complete.
