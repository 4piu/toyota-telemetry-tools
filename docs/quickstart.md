# Toolkit quickstart

The toolkit runs inside a schema-2 vehicle project. A project owns vehicle
facts and evidence; this repository owns reusable programs.

There are two complete, interchangeable interfaces over the same library:

- `telemetry`, a command-line interface for terminals and scripts; and
- `telemetry-ui`, the desktop application (install the `gui` extra).

Every step below names the command and the desktop page that does the same
thing. Use either, or both; they read and write the same project files.

## Install

From the vehicle project root:

```powershell
..\telemetry-tools\tools\setup-platform-project.ps1
.\.venv\Scripts\Activate.ps1
telemetry --version
```

Setup reads `[toolkit]` and `[paths]` from `telemetry-project.toml`, builds all
private adapter workers into the project's ignored `scratch/bin/`, and installs
the Python package from that same checkout.

`telemetry status` (or the desktop banner) always shows the next useful step
and the command for it.

## Project files

Tracked authorities:

- `config/transport.json`: confirmed endpoints, session recipes, dynamic-DID
  definitions, and measured safety limits;
- `config/collection-policy.json`: shared endpoint+DID settings and signal
  collection targets;
- `review/signals.json`: reviewed signal definitions and evidence states;
- `result/catalog-manifest.json`: privacy-minimized provenance of the
  candidate list.

Ignored machine-local state under `scratch/`: connector settings, the candidate
inventory, verification results, captures, the compiled collection scope, and
the disposable SQLite cache. The tracked JSON files may also be edited by hand;
`telemetry check` validates them, and the desktop application notices outside
edits.

## 1. Set up (Setup page)

```powershell
telemetry connector set pcan --channel PCAN_USBBUS1
telemetry connector check
telemetry source import file scratch/catalog-rows.json            # preview
telemetry source import file scratch/catalog-rows.json --apply
telemetry source import tis --database "C:\...\Techstream\NA" --phase 5 --apply
```

`connector check` validates settings and runs the worker self-test without
opening the device. `source import` previews first: counts, conflicts,
rejected rows, and what changes compared with the current list; `--apply`
replaces that source's rows, updates the tracked catalog manifest, and
rebuilds the cache. The TIS source needs Windows and installed Techstream
databases. Candidate rows never become reviewed authority by being imported.

## 2. Verify (Signals page, LIVE)

```powershell
telemetry signals --state not-verified
telemetry verify 7a1-7a9 --dry-run
telemetry verify 7a1-7a9
telemetry endpoint confirm 7a1-7a9-extended --ecu 405 --source docs/evidence/probe.md
```

Selectors are `ENDPOINT`, `ENDPOINT/0xNNNN`, `ENDPOINT/0xNNNN/FIRST..LAST`, or
a signal reference `ECU/0xNNNN/FIRST..LAST`, narrowed by `--ecu`, `--state`,
and `--search`. They choose only from the candidate list; there are no ranges
or sweeps. An endpoint that is not verified yet is probed first and its DIDs
are read only if it responds. Results (baseline bytes, latency, outcome) are
kept in ignored `scratch/verification-facts.json`. `endpoint confirm` writes a
responding endpoint into tracked transport. `endpoint add
TESTER:RESPONSE:RECIPE --ecu KEY` adds an address the source did not provide.

### Live confirmation

`verify` and `collect` are the only commands that transmit. Both print the
exact scope first. In a terminal they ask you to type `yes`. In a script,
`--yes` is accepted only when the scope is identical to the one last printed by
`--dry-run` for that command; the preview is recorded automatically, so run
`--dry-run`, review it, then repeat with `--yes`. Ctrl+C or the word `stop`
on standard input stops safely. Only one live job may run per project.

## 3. Review and set targets (Signals page)

```powershell
telemetry signal set 372/0x1002/0..15 --name "Battery voltage" --formula "raw / 1000" --unit V
telemetry did set 700-708-extended/0x1002 --interval 500 --max-interval 1000
telemetry target set 372/0x1002/0..15 --min-samples 200 --min-runs 3
telemetry target set 372/0x1003/0..7 --required-states 1,2,3 --when "372/0x1002/0..15 > 11"
telemetry target set 372/0x1003/8..15 --min-changes 20 --min-difference 0.5
telemetry target clear 372/0x1003/8..15
```

A target lists completion requirements directly: minimum samples, runs, span,
required states, transitions, changes (with a minimum difference in the
signal's unit), and applicable time. A value of 0 removes one requirement.
`--when` limits which observations count. DID settings are shared by every
signal on that endpoint and DID, and edits report the unselected sibling
signals they affect. Every editing command accepts `--dry-run`. Removing a
target never removes recordings.

## 4. Collect (Collect page, LIVE)

```powershell
telemetry collect --dry-run
telemetry collect --note "highway, passenger operating"
```

Readiness is compiled automatically. It skips complete targets, requires
verified latency/size facts, checks request rate, bus and session load,
storage, decode, and condition dependencies, and reports each problem with
the command that fixes it. One run collects one endpoint (`--endpoint` when
targets span several) and stops itself when its targets are complete.

Do not operate a computer while driving; use a passenger or an unattended
logger.

## 5. Analyze (Recordings page)

```powershell
telemetry recordings
telemetry recordings show RUN
telemetry recordings recover RUN          # interrupted run: keep its valid prefix
telemetry recordings quarantine RUN       # move an unrecoverable run aside
telemetry recordings export --signal 372/0x1002/0..15 --output battery.csv
telemetry result generate
```

All valid runs are included. Export writes UTC, run, elapsed time, raw bytes,
decoded value, and condition state for external plotting; the desktop
application plots the same data with run boundaries kept apart. `result
generate` writes the tracked, privacy-minimized result generation.

Every command accepts `--json` for one machine-readable document (JSON Lines
for live commands). Exit codes: 0 success, 1 failure, 2 usage error, 3
refused by confirmation or safety rules.
