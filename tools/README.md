# Utilities

These tools are reference implementations for observed formats. Paths, database keys, endpoints, and signal profiles are arguments or configuration; vehicle defaults do not belong here.

| Tool | Purpose |
|---|---|
| `j2534/save-techstream-log.ps1` | Signal an already-active compatible Techstream logger to snapshot its circular buffer. |
| `j2534/parse-j2534-log.py` | Inventory/timeline a text log, learn dynamic DIDs, and decode a supplied signal profile. |
| `techstream/query-techstream-db.ps1` | Query observed Phase-5 catalog, protocol, monitor, physical, and unit records. |
| `techstream/inspect-session.py` | Print a privacy-minimized `.TSE` section/ECU inventory. |
| `catalog/build-catalog.py` | Join query results according to `templates/catalog-profile.json` and emit a candidate atlas plus provenance manifest. |
| `privacy-scan.ps1` | Scan prospective tracked files for common local/private markers. |

The database and saved-session layouts are tied to examined Techstream versions. Tools must reject structures they cannot parse. Keep inputs outside the repository and review every output before publication.
