# Reconstruct diagnostic transport

## Purpose

Turn a trace into an ordered, independently reproducible conversation before decoding values:

```text
connect -> configure -> filter -> session -> definition -> poll -> response -> keep-alive
```

## Record the layers separately

- Physical/network: CAN/CAN FD, nominal bitrate, 11/29-bit identifiers.
- Addressing: tester TX ID, ECU RX ID, functional ID, normal versus extended addressing.
- Transport: adapter-managed ISO-15765 complete payloads or application-managed ISO-TP frames; flow-control block size and separation time.
- Diagnostics: service, positive/negative response, session, timing parameters, tester present, security state.
- Acquisition: direct source DID, mapped record, or composite/dynamic DID.

J2534 ISO15765 messages commonly begin with a four-byte big-endian CAN ID in the API buffer. Raw CAN instead exposes ISO-TP PCI bytes. Do not copy an API buffer byte-for-byte onto the bus.

## Procedure

1. Sort calls and complete messages by host timestamp; retain bus timestamps when provided.
2. Separate ISO-TP start indications from complete payloads.
3. Pair tester and response endpoints using filters and request/response timing, not only `request + 8` convention.
4. Identify session-control (`10`), tester-present (`3E`), read-data (`22`), dynamic-definition (`2C`), positive responses, and negative response code (`7F`) behavior.
5. For raw CAN, reconstruct first/consecutive frame sequence numbers and flow control in both directions.
6. Replay only after every transmitted payload is explained and placed in an allowlist.

## Output

Store a transport profile plus a byte-exact setup/poll state machine. Maintain separate flags for endpoint confirmation and signal validation. An observed endpoint can be Replayed while a contained field remains only Candidate.
