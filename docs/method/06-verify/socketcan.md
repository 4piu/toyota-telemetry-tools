# Verify with SocketCAN and ISO-TP

Use this route for a Linux SBC/PC whose CAN controller and transceiver are supported by the maintained kernel/device tree.

## Boundary

Keep application acquisition independent of the adapter. A backend should expose complete diagnostic request/response payloads plus monotonic timestamps and transport errors; the signal decoder should not know whether frames came from J2534, PCAN, SocketCAN, or an MCU bridge.

## Procedure

1. Configure CAN bitrate and bring up the interface without automatic restart hiding initial faults.
2. Confirm controller error state and termination/wiring.
3. Bind an ISO-TP socket to the exact tester/ECU IDs and addressing mode; configure padding/flow-control only from captured evidence.
4. Use the same offline admission, allowlist, response validation, timeout, and
   stop rules used by USB-CAN/J2534.
5. Compare raw payloads and timing with the already Replayed implementation.
6. Record kernel, driver, interface, controller/transceiver, clock, and configuration in the experiment.

Only after equivalence is shown should the vehicle catalog mark the transport Ported. Keep display rendering in another process/module so acquisition failures produce explicit stale data rather than a frozen plausible value.
