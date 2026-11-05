# Scope and safety

## Purpose

Establish a read-only experimental boundary before connecting a diagnostic requester. This guide is for telemetry acquisition, not control, coding, customization, DTC clearing, security access, memory access, or reflashing.

## Rules

- Begin stationary in Park with the parking brake applied; use IG-ON or READY only when the experiment requires it.
- Treat every diagnostic client as an active CAN participant, even when it only requests data.
- Permit exactly one tester to own an ECU diagnostic session. Close Techstream/PCAN-View or any competing client before transmitting.
- Transmit only through the two live operations, verification and
  collection (`telemetry verify` / `telemetry collect`, or the desktop
  application's Verify and Collect actions). Each shows its exact finite
  scope and needs explicit confirmation; the private worker resolves only
  launch-time transactions and applies the built-in telemetry guard to every
  request.
- Allow only tester present, default or extended session control, bounded
  dynamic-DID definition, read-by-identifier, and required ISO-TP flow control.
- Reject unknown vehicle profiles, unexpected arbitration IDs, negative responses not anticipated by the procedure, malformed ISO-TP, and timeouts.
- Send the next request after the previous transaction completes. Start at the observed OEM cadence.
- Do not keep sleeping ECUs awake or risk 12-V battery depletion.
- During road validation, use an unattended logger or a passenger. The driver must not operate a computer.
- Secure the adapter, cable, and computer so they cannot reach the pedals, steering, or driver.
- Never induce hazardous events to satisfy a target. ABS, stability control, collision avoidance, airbag, overheating, and similar signals are observed only when they occur naturally and safely.
- Never use reverse-engineered telemetry as an input to vehicle control or a safety-critical warning.

## Evidence produced

Copy [the experiment template](templates/experiment.yaml) and record the state, permitted transmissions, stop conditions, operator arrangement, and recovery plan. A result without this context is not reproducible. Keep the filled record with the private captures in the project workspace, and summarize it in the deliverable repository's evidence write-up (see [deliverables](deliverables.md)).

## Stop conditions

Disconnect the tester after repeated UDS `busyRepeatRequest`, prolonged `responsePending`, transport errors, CAN controller errors, receive overruns, unexplained warning lamps, ECU resets, or behavior outside the experiment plan.
