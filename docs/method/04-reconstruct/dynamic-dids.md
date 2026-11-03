# Reconstruct dynamic data identifiers

Some Toyota Data Lists assemble several source slices into one repeatedly polled DID. This improves cadence but means the composite response has no meaning until the definition is known.

An observed UDS DefineByIdentifier form is:

```text
2C 01 <target-DID-hi> <target-DID-lo>
   <source-DID-hi> <source-DID-lo> <one-based-byte-position> <byte-count>
   ...
```

Clearing one observed definition uses `2C 03 <target-DID>`. Treat both as calibration/version-specific until captured.

## Procedure

1. Find a transmitted definition before repeated `22 <target-DID>` reads.
2. Parse each source tuple in order. Position is within the source DID; it is not the destination offset.
3. Concatenate the selected source slices to map composite response bytes.
4. Account for the `62 <target-DID>` response prefix before applying the composite map.
5. Diff a minimal Data List against one with one added item; the new tuple/slice is strong association evidence.
6. If a later snapshot contains polls but not setup, load definitions from an earlier snapshot from the same session.

Reject zero position/count, truncated tuples, incomplete responses, or a definition learned from another ECU/session without evidence. Preserve the source mapping in the signal dossier; direct source-DID support must be tested separately.
