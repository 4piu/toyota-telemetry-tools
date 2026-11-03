# Capture Techstream through J2534

## Choose this route when

Techstream can display the value and a working J2534 VIM is available. J2534 is an application-to-adapter API: it can hide vendor USB initialization and adapter firmware, but it still exposes the diagnostic payloads, channel parameters, filters, addressing, replies, and host-observed timing needed for an independent client.

## Minimal experiment

1. Create a custom Data List containing one target item. Keep only a required baseline item.
2. Establish a steady 10–30 second baseline.
3. Change one physical input through a safe, deliberate range; hold reference points; return to baseline.
4. Save the logger's current buffer and a synchronized note/video of displayed values.
5. Repeat after adding exactly one item. Diff setup and repeated traffic.

Record `PassThruConnect` protocol/flags/bitrate, configuration IOCTLs, masks/filters/flow control, session setup, tester-present cadence, complete messages, statuses, and timestamps. A `START_OF_MESSAGE` record may be an indication rather than a duplicate payload.

The Techstream logger examined so far uses named Windows events and a circular buffer. Signaling save creates an immediate snapshot; waiting for the logger only waits for availability and is not a recording duration. Use [the save utility](../../../tools/j2534/save-techstream-log.ps1) only after confirming event names and output location for the installed version.

## Output and confidence

Store raw captures privately in the project workspace. Promote a sanitized chronological exchange plus experiment metadata. A focused request/response raises a signal to Observed; controlled stimulus can raise it to Correlated. Continue with [transport reconstruction](../04-reconstruct/diagnostic-transport.md).
