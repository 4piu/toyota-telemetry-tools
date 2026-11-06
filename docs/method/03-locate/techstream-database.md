# Query the local Techstream database

> This route needs your own licensed Techstream installation. The project is
> not affiliated with Toyota, ships none of its software or data, and describes
> only what was observed in one installed version, for interoperability.

## Choose this route when

Techstream is installed but live capture is unavailable, or a broad candidate map is wanted before targeted validation.

## Procedure

1. Inventory the master ECU catalog and identify database keys/files evidenced by the vehicle profile.
2. Query monitor records for the exact Data List name and synonyms.
3. Preserve acquisition type, source identifier, first/last bit, and record IDs.
4. Join physical-conversion and unit records by their database keys.
5. Query protocol metadata separately and label response IDs as candidates until observed.
6. Export provenance: product/database version and region, filenames and hashes, query-tool revision, profile, time, row counts, and schema version. Never copy the database itself.

For the Phase-5 records studied so far, numeric conversion is:

```text
physical = ((raw * A / B) + C) / (10 ** N)
```

Preserve `A`, `B`, `C`, and `N` as well as a rendered expression. Negative physical ranges do not prove the raw field is signed; an unsigned field plus a negative offset is common.

[The database helper](../../../backends/Telemetry.TisDb/) (`telemetry-tisdb.exe`, built with the workers) exposes observed Techstream structures through Techstream's own controller DLL, one JSON record per line; it is a 32-bit program because that DLL is 32-bit. Run it with `--help` to query tables directly for investigation. Its table layouts are version-specific implementation findings, not an official schema.

## Output and limits

A static catalog contains Candidate rows. Database files can aggregate multiple calibrations; direct DID metadata does not prove support; mapped/dynamic items require reconstruction; byte order, enums, invalid sentinels, and availability may need more evidence. Validate selected rows through [USB-CAN](../06-verify/usb-can.md) or [J2534](../06-verify/j2534.md).
