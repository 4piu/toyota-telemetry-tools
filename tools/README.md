# Utilities

These tools are reference implementations for observed formats. Paths, database keys, endpoints, and signal profiles are arguments or configuration; vehicle defaults do not belong here.

| Tool | Purpose |
|---|---|
| `j2534/save-techstream-log.ps1` | Signal an already-active compatible Techstream logger to snapshot its circular buffer. |
| `j2534/parse-j2534-log.py` | Inventory/timeline a text log, learn dynamic DIDs, and decode a supplied signal profile. |
| `techstream/query-techstream-db.ps1` | Query observed Phase-5 catalog, protocol, monitor, physical, and unit records. |
| `techstream/import-ecu-catalog.py` | Narrow a `-Catalog` ECU dump to an `ecu-profile.json`, optionally querying each ECU's endpoint. |
| `techstream/inspect-session.py` | Print a privacy-minimized `.TSE` section/ECU inventory. |
| `catalog/build-catalog.py` | Join query results according to `templates/catalog-profile.json` and emit a candidate atlas plus provenance manifest. |
| `telemetry-build-plan` | Build a privacy-aware, tiered validation plan from a catalog and platform profile. |
| `telemetry-validate` | Reconstruct progress and run bounded qualification/road batches through a configured backend. |
| `telemetry-analyze-capture` | Independently validate and summarize one candidate-capture JSONL file. |
| `telemetry-review` | Aggregate every completed capture into a per-signal evidence ledger; list what still needs excitation; check a curated map against the observations. |
| `telemetry-map` | Emit a complete signal map from the catalog annotated with observations, plus a list of the DIDs a vehicle refused. |
| `telemetry-probe` | Probe every ECU endpoint the catalog knows, plus any given explicitly, and write configuration for those that answer. |
| `privacy-scan.ps1` | Scan prospective tracked files for common local/private markers. |

The programs that talk to a vehicle are not here; they live in [`backends/`](../backends/README.md).

The database and saved-session layouts are tied to examined Techstream versions. Tools must reject structures they cannot parse. Keep inputs outside the repository and review every output before publication.
