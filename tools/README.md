# Supporting extraction tools

The installed Python command surface is the single `telemetry` dispatcher; see
the repository [README](../README.md) and [quickstart](../docs/quickstart.md).

This directory contains offline utilities, run from a toolkit checkout:

- `techstream/inspect-session.py` lists the ECU keys and database locale of a
  saved Techstream session (`.TSE`) without exposing its contents;
- `j2534/save-techstream-log.ps1` asks Techstream's J2534 logger to save its
  buffer, and `j2534/parse-j2534-log.py` turns that log into the ordered
  request/response conversation, from which the session setup is read
  (`--signals` decodes reads with a profile like
  `j2534/signal-profile.example.json`);
- `privacy-scan.ps1` checks a tree for private material before publishing.

Candidate lists are imported with `telemetry source import` (or the desktop
Setup page). The TIS database reader that import uses is the bundled
`telemetry-tisdb.exe` helper (source in `backends/Telemetry.TisDb/`); run it
with `--help` to query the database directly for investigation.

OEM databases, vendor libraries, raw logs, and complete derived atlases are
private inputs. Keep them in the workspace or another private folder, and
publish only what `telemetry export` writes.
