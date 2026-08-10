# Example: XA50 Hybrid Control

This sanitized example shows how the evidence model joins static discovery and independent replay. It is illustrative, not a universal XA50 definition.

- A saved Techstream session associated ECU key `397` with Hybrid Control and `HV_P5.ddb`.
- Static records supplied candidate source DIDs, MSB-first bit ranges, and Phase-5 conversion coefficients.
- A focused J2534 capture observed tester `0x7D2`, ECU response `0x7DA`, diagnostic sessions, and a dynamic DID `F301` assembled from source slices.
- PCAN raw-CAN replay independently reproduced setup and repeated `22 F3 01` polling through application-managed ISO-TP.
- MG1, MG2, and accelerator agreed with Techstream at multiple stationary points and are Validated/Replayed for the tested profile.
- Wheel speeds were replayed at stationary zero but rolling validation remains pending; they are only Observed/Replayed.

The key lesson is the separation of claims: the Hybrid endpoint and transport are live-confirmed, a small curated signal set carries live evidence, and thousands of other database rows remain static candidates.

`fixtures/j2534-dynamic-did.txt` is a synthetic, identifying-data-free excerpt used to exercise dynamic-definition parsing and external signal-profile decoding. It is not a raw vehicle capture.
