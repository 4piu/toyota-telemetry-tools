# Add an adapter backend

PCAN-Basic, J2534, gs_usb, and slcan workers ship in [`backends/`](../backends/README.md).
An adapter changes transport, not project policy or signal meaning.

## Channel boundary

A new backend implements the small `ICanChannel` interface and a `Main` that
delegates to `BackendEntry.Run`. That entry exposes only offline self-test and
the private worker and probe protocols. Python remains the public collector.

The channel must:

- open and close one configured classical-CAN channel;
- send standard 11-bit frames with exactly eight data bytes;
- receive frames without interpreting UDS payloads;
- apply the narrowest available response-ID filter;
- surface controller, overflow, and transport errors promptly;
- refuse detectable concurrent ownership;
- support cooperative stop and deterministic cleanup.

The shared layer owns ISO-TP framing, session setup, response-pending handling,
timeouts, and the built-in telemetry guard. Device-specific code must not add
a second scheduler, capture writer, project parser, or fallback binding.

## Private process contract

The executable accepts:

```text
<executable> --self-test
<executable> --worker --config <temporary-exact-schema.json>
<executable> --probe --config <temporary-exact-schema.json>
```

Unknown arguments fail. The worker form is launched only by
`telemetry collect`. It reads exact JSON-Lines `exchange` and `stop` commands
from stdin and emits exact `ready`, `response_pending`, `result`, and `stopped`
events on stdout. There is one in-flight transaction. An `exchange` command
names a `transaction_id`, never request bytes; the worker resolves it against
its own immutable launch-time transaction table, and the resolved request must
pass the built-in telemetry guard before any frame is sent.

The probe form is launched only by `telemetry discover endpoints` and
`telemetry preflight dids`. It takes no stdin commands: it opens a channel,
attempts its configured session-setup steps and at most one
ReadDataByIdentifier, prints exactly one `probe_result` JSON line, and exits.
Every attempted request passes the same telemetry guard as the worker form.
Session steps carry their exact `tolerate_negative` policy; a non-tolerated
negative response prevents the optional DID read. The Python parent enforces a
derived wall-clock deadline and terminates the child on timeout or interruption.

Machine-local adapter settings come from the vehicle project's ignored local
settings and are embedded in the temporary worker config. Portable endpoint,
session, and safety facts come from tracked transport authority. No backend
auto-detection or cross-adapter fallback is permitted.

## Acceptance

1. `--self-test` opens no device and checks shared framing, the telemetry
   guard, and protocol schema behavior.
2. Wrong/unknown worker fields and protocol versions fail closed.
3. A request the telemetry guard does not recognize cannot reach
   `ICanChannel.Write`.
4. One known single-frame and one known multi-frame response match a proven
   backend byte-for-byte.
5. Timeout, response pending, cancellation, channel failure, and cleanup are
   exercised without losing a valid completed prefix in the Python capture.
6. Adapter selection and channel binding remain private and exact.

Document electrical isolation, transceiver voltage, termination, connector
mapping, firmware/driver version, and whether hardware behavior is measured or
only planned. Hardware tests are explicit and separate from offline self-test.
