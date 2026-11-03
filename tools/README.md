# Supporting extraction tools

The installed Python command surface is the single `telemetry` dispatcher; see
the repository [README](../README.md) and [quickstart](../docs/quickstart.md).

This directory contains offline utilities, run from a toolkit checkout:

- `techstream/inspect-session.py` inspects saved Techstream sessions;
- `j2534/` parses recorded pass-through logs offline;
- `privacy-scan.ps1` checks a tree for private material before publishing.

Candidate lists are imported with `telemetry source import` (or the desktop
Setup page). The TIS database reader that import uses is shipped inside the
package as `src/toyota_telemetry/operator/query-techstream-db.ps1`; it can
also be run directly for investigation.

OEM databases, vendor libraries, raw logs, and complete derived atlases are
private inputs. Keep them in the workspace or another private folder, and
publish only what `telemetry export` writes plus reviewed written evidence.
