# Inventory a saved Techstream session

## Choose this route when

A `.TSE` session exists and you need to determine which ECU/database records were selected for that vehicle without exposing the session contents.

Use [the session inventory utility](../../../tools/techstream/inspect-session.py) to emit only format/version, database locale, ECU keys, and section names. The parser recognizes one observed format revision and must fail closed on an unknown header or truncated structure.

Run it from a toolkit checkout:

```powershell
python tools/techstream/inspect-session.py private-session.TSE
```

Treat ECU keys as vehicle-session evidence, not confirmed CAN endpoints. Feed them into the database route, then record how the profile was established. Keep the source `.TSE` private, in the project workspace and never in a deliverable repository: later sections may contain VIN, calibration, customization, health-check, or trip-related data even when this utility intentionally omits them.
