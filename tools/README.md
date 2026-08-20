# Utilities

These tools are reference implementations for observed formats. Paths, database keys, endpoints, and signal profiles are arguments or configuration; vehicle defaults do not belong here.

| Tool | Purpose |
|---|---|
| `j2534/save-techstream-log.ps1` | Signal an already-active compatible Techstream logger to snapshot its circular buffer. |
| `j2534/parse-j2534-log.py` | Inventory/timeline a text log, learn dynamic DIDs, and decode a supplied signal profile. |
| `techstream/query-techstream-db.ps1` | Query observed Phase-5 catalog, protocol, monitor, physical, and unit records. |
| `techstream/build-ecu-profile.py` | Narrow the installed ECU catalog to an `ecu-profile.json`, with each ECU's endpoint from its protocol table. |
| `techstream/inspect-session.py` | Print a privacy-minimized `.TSE` section/ECU inventory. |
| `catalog/build-catalog.py` | Join query results according to `templates/catalog-profile.json` and emit a candidate atlas plus provenance manifest. |
| `telemetry-build-plan` | Build a privacy-aware, tiered validation plan from a catalog and platform profile. |
| `telemetry-validate` | Reconstruct progress and run bounded qualification/road batches through a configured backend. |
| `telemetry-analyze-capture` | Independently validate and summarize one candidate-capture JSONL file. |
| `telemetry-review` | Aggregate every completed capture into a per-signal evidence ledger; list what still needs excitation; check a curated map against the observations. |
| `telemetry-map` | Emit a complete signal map from the catalog annotated with observations, plus a list of the DIDs a vehicle refused. |
| `telemetry-probe` | Probe every ECU endpoint the catalog knows, plus any given explicitly, and write configuration for those that answer. |
| `setup-platform-project.ps1` | Create a platform project from `templates/platform-project/` (`-Initialize`), then check the field guide out at its pinned revision, build its backends, and install the toolkit into its `.venv` from that checkout. |
| `privacy-scan.ps1` | Scan prospective tracked files for common local/private markers. |

## Text encoding in the Techstream toolchain

Database text reaches a catalog row through three decodes, and until each was
stated explicitly the result depended on how the machine happened to be
configured. On the development machine the system ANSI code page is 932, the
console output code page is 932, and the user culture is `en-US` — three
answers to "what is this computer's code page", which is why guessing does not
work.

| Hop | Was | Is |
|---|---|---|
| Native string → .NET string | `PtrToStringAnsi`, i.e. whatever `GetACP()` returns | `-TextCodePage`, default 932 |
| PowerShell → stdout | the console output code page | UTF-8, no BOM |
| stdout → Python | decoded as UTF-8 | decoded as UTF-8, BOM tolerated |

The databases examined store text in code page 932 even in the English NA set:
the degree-Celsius sign arrives as bytes `0x81 0x8B`, its Shift-JIS encoding.
Reading those bytes as cp1252 — which is what an ordinary Western Windows
install would have done — yields two characters that are not a unit and cannot
be turned back into one. Pass `--text-code-page` (or `-TextCodePage`) if your
database differs; `0` means the system ANSI code page, reproducing the old
behaviour. The value used is recorded in the catalog's `manifest.json`
alongside the input hashes, and `build-catalog.py` warns if any generated row
still contains a replacement character.

## Console output

Every `telemetry-*` command prints a human summary to stdout and sends
diagnostics to stderr, and they share four options:

| Option | Effect |
|---|---|
| `--json` | Emit the machine-readable document instead of the summary. Pipe this; do not parse the human form. |
| `-v` / `-vv` | More detail, then debug. |
| `-q` | Warnings and errors only. |
| `--no-progress` | Never draw a progress line, even on a terminal. |

A live capture draws a progress line with the record count as the backend
writes it. When stderr is not a terminal — a scheduled run, or output kept in a
file — that degrades to a log line at intervals rather than carriage returns
into a file.

The programs that talk to a vehicle are not here; they live in [`backends/`](../backends/README.md).

The database and saved-session layouts are tied to examined Techstream versions. Tools must reject structures they cannot parse. Keep inputs outside the repository and review every output before publication.
