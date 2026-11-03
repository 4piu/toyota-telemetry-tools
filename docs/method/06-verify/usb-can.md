# Verify with USB-CAN

## Wiring and controller checks

At a standard DLC3/OBD-II connector, verify the exact vehicle documentation before assuming pinout. A commonly used high-speed CAN path is pin 6 CAN-H, pin 14 CAN-L, and pin 4 or 5 ground. Never guess from wire color. An isolated adapter may still require its signal ground as documented.

Do not enable an adapter's 120-ohm terminator when attaching as a stub to an already terminated in-vehicle bus. With vehicle power off, the established bus often measures about 60 ohms between CAN-H and CAN-L; investigate before transmitting if it instead resembles an open circuit or an extra termination.

## Procedure

1. Confirm bitrate and controller state with a listen-only observation where the gateway exposes traffic; an empty gateway-facing bus does not by itself prove failure.
2. Prove TX/RX using one standardized OBD-over-CAN request appropriate to the vehicle, with a strict expected response filter.
3. Probe the candidate OEM endpoint using one benign tester-present request.
4. Reproduce the captured session/setup and read transaction using application-managed ISO-TP.
5. Accept only expected response IDs; validate DLC, PCI type, declared length, sequence numbers, padding, and UDS prefix.
6. Send flow control promptly after a first frame. Do not issue a new request until completion or timeout.

The adapter activity LED proves only local transmit/receive activity, not correct wiring or an ECU response. Swapped CAN-L/ground can produce convincing LEDs and no messages; verify conductors electrically.

Start with single allowlisted probes. Once exact replay succeeds, expand one DID or dynamic definition at a time.
