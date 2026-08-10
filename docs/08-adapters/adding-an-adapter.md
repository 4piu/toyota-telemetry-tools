# Add an adapter backend

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
