# Utilities

These tools are reference implementations for observed formats. Paths, database keys, endpoints, and signal profiles are arguments or configuration; vehicle defaults do not belong here.

| Tool | Purpose |
|---|---|
| `j2534/save-techstream-log.ps1` | Signal an already-active compatible Techstream logger to snapshot its circular buffer. |
| `j2534/parse-j2534-log.py` | Inventory/timeline a text log, learn dynamic DIDs, and decode a supplied signal profile. |
| `j2534/J2534Api.cs` | Vehicle-agnostic C# PassThru API wrapper (open/connect/filter/read/write). Vendored (copied) into a platform project's compiled J2534 client; see [verify with J2534](../docs/06-verify/j2534.md). |
| `pcan/PcanBasic.cs` | Vehicle-agnostic C# PCAN-Basic API wrapper (initialize/status/filter/read/write). Vendored (copied) into a platform project's compiled PCAN client; see [verify with PCAN-Basic](../docs/06-verify/pcan.md). |
| `techstream/query-techstream-db.ps1` | Query observed Phase-5 catalog, protocol, monitor, physical, and unit records. |
| `techstream/inspect-session.py` | Print a privacy-minimized `.TSE` section/ECU inventory. |
| `catalog/build-catalog.py` | Join query results according to `templates/catalog-profile.json` and emit a candidate atlas plus provenance manifest. |
| `telemetry-build-plan` | Build a privacy-aware, tiered validation plan from a catalog and platform profile. |
| `telemetry-validate` | Reconstruct progress and run bounded qualification/road batches through a configured backend. |
| `telemetry-analyze-capture` | Independently validate and summarize candidate-capture JSONL. |
| `privacy-scan.ps1` | Scan prospective tracked files for common local/private markers. |

The database and saved-session layouts are tied to examined Techstream versions. Tools must reject structures they cannot parse. Keep inputs outside the repository and review every output before publication.
