# Verify timing, rate, and load

For each focused request record request-start interval, request-to-complete-response latency, timeout/negative-response/error counts, underlying CAN frames, and approximate bus load.

## Rate test

1. Begin at the observed OEM cadence.
2. Wait for complete response or timeout before starting another transaction.
3. Collect a meaningful stability run; report count plus min/median/mean/p95/max, not only best latency.
4. Reduce the minimum period incrementally only if the application needs it.
5. Do not “catch up” by sending bursts after a slow response.
6. Back off on receive overruns, CAN errors, malformed ISO-TP, timeouts, UDS `busyRepeatRequest`, or repeated `responsePending`.

Estimate wire time using actual frames, bit stuffing margin, arbitration, inter-frame space, and flow-control frames. For a dashboard, freshness requirements often matter more than maximizing rate. Poll values sharing a dynamic DID together; crossing ECUs adds session/transport overhead and should be measured separately.

Record adapter/driver and host latency separately when possible. A raw-CAN backend may be much faster than adapter-managed J2534 even through the same gateway, but only measured distributions justify that conclusion.
