# Decode formulas and bitfields

## Field layout

Define the data region after diagnostic service/DID bytes. Record inclusive first/last bit, bit numbering, width, byte order, and raw signedness. The examined Techstream database numbers bit zero as the most significant bit of the first source byte; verify this for every schema/version.

For an MSB-first inclusive field:

```text
width = last_bit - first_bit + 1
shift = payload_bits - 1 - last_bit
raw = (payload_as_big_endian_integer >> shift) & ((1 << width) - 1)
```

## Numeric conversion

Preserve source coefficients and a normalized expression. For the observed Phase-5 numeric records:

```text
physical = ((raw * A / B) + C) / (10 ** N)
```

Evaluate using sufficient precision; reject zero divisors. Do not infer two's-complement signedness from a negative displayed range when an unsigned field plus offset fits.

## Validation of an interpretation

Test against an entire controlled capture. Reject candidates that produce a wrong zero, discontinuities, impossible range, incorrect direction, or inconsistent steps. Check boundaries and negative/reverse conditions where safe. Preserve raw bytes alongside decoded values so a later formula correction does not require repeating the experiment.

Record whether byte order and signedness came from a database schema, a static code path, or live agreement; they are distinct evidence claims.
