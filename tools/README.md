# Supporting extraction and setup tools

The installed Python command surface is the single `telemetry` dispatcher; see
the repository [README](../README.md) and [quickstart](../docs/quickstart.md).

This directory contains repository/setup scripts and offline utilities:

- `setup-platform-project.ps1` selects the toolkit revision declared by a
  schema-2 project, builds private adapter workers, and installs Python;
- `techstream/inspect-session.py` inspects saved Techstream sessions;
- `j2534/` parses recorded pass-through logs offline;
- `privacy-scan.ps1` checks a tree for private material before publishing.

Candidate lists are imported with `telemetry source import` (or the desktop
Setup page). The TIS database reader that import uses is shipped inside the
package as `src/toyota_telemetry/operator/query-techstream-db.ps1`; it can
also be run directly for investigation.

OEM databases, vendor libraries, raw logs, and complete derived atlases are
private inputs. Keep them under the vehicle project's ignored `scratch/`
boundary and commit only reviewed provenance and privacy-minimized results.
