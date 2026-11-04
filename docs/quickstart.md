# Toolkit quickstart

The toolkit is installed once and works on a project **workspace**: one
folder, outside any repository, that holds every working file. Results reach
a **deliverable** folder (usually the vehicle's Git repository) only by
export.

There are two complete, interchangeable interfaces over the same library:

- `telemetry`, a command-line interface for terminals and scripts; and
- `telemetry-ui`, the desktop application (install the `gui` extra).

Every step below names the command and the desktop page that does the same
thing. Use either, or both; they read and write the same workspace.

## Install

```powershell
git clone https://github.com/4piu/70Y074-telemetry-tools.git
.\70Y074-telemetry-tools\backends\build.ps1
uv tool install --editable ".\70Y074-telemetry-tools[gui]"
telemetry --version
```

`build.ps1` compiles the adapter workers into the package, where every
workspace finds them. After pulling a new toolkit revision, rebuild the
workers if `backends/` changed; the editable install picks up Python changes
directly.

## 0. Create a workspace (Project > New project)

```powershell
telemetry init C:\work\my-car-workspace --profile-id my-car-profile
cd C:\work\my-car-workspace
telemetry status
```

`telemetry status` (or the desktop banner) always shows the next useful step;
the CLI also prints the command for it. Inside the workspace, commands find it
by searching upward; elsewhere pass `--project-root`.

## Workspace files

Curated inputs, exported as copies:

- `config/transport.json`: confirmed endpoints, session recipes, dynamic-DID
  definitions, and measured safety limits;
- `config/collection-policy.json`: shared endpoint+DID settings and signal
  collection targets;
- `review/signals.json`: reviewed signal definitions and evidence states;
- `config/catalog-manifest.json`: privacy-minimized provenance of the
  candidate list.

Private working data, never exported: `captures/` (recordings) and `state/`
(connector settings, the candidate inventory, verification results, the
compiled collection scope, and the disposable SQLite cache). The JSON files
may also be edited by hand; `telemetry check` validates them, and the desktop
application notices outside edits.

A new workspace has no endpoints or session recipes. Add the reviewed session
recipe to `config/transport.json` by hand (only diagnostic session control and
tester present are accepted), then add or import endpoints. To continue an
existing vehicle, copy its exported `config/` and `review/` from the
deliverable repository into the new workspace.

## 1. Set up (Setup page)

```powershell
telemetry connector set pcan --channel PCAN_USBBUS1
telemetry connector check
telemetry source import file C:\private\catalog-rows.json            # preview
telemetry source import file C:\private\catalog-rows.json --apply
telemetry source import tis --database "C:\...\Techstream\NA" --phase 5 --apply
```

`connector check` validates settings and runs the worker self-test without
opening the device. `source import` previews first: counts, conflicts,
rejected rows, and what changes compared with the current list; `--apply`
replaces that source's rows, updates the catalog manifest, and
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
kept in `state/verification-facts.json`. `endpoint confirm` writes a
responding endpoint into the transport settings; `--source` names where the
evidence is written up in the deliverable repository. `endpoint add
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
telemetry recordings                     # drives, newest last
telemetry recordings show 2026-09-01T08:15
telemetry recordings recover 2026-09-01T08:15   # interrupted: keep what was saved
telemetry recordings trash 2026-09-01T08:15     # to the system trash (Recycle Bin)
telemetry recordings export --signal 372/0x1002/0..15 --drive 2026-09-01T08:15 --output battery.csv
telemetry export C:\work\my-car-deliverables
```

Recordings are grouped into drives: a pause of more than a minute starts a
new drive, and a drive is named by its start (UTC). Imported recordings
often arrive as thousands of short runs; `--runs` lists those individually.
Deleting a drive moves its run folders to the operating system's trash;
restore them there to bring the drive back.
All valid runs are included. Export writes UTC, run, elapsed time, raw bytes,
decoded value, and condition state for external plotting; the desktop
application plots the same data with run boundaries kept apart. `export`
writes the privacy-minimized result generation and copies of the curated
inputs into the deliverable folder (Recordings > Export result). It refuses a
folder inside the workspace and one that holds another project's results.
The manifest records the toolkit version, and its commit when it runs from a
Git checkout.

Every command accepts `--json` for one machine-readable document (JSON Lines
for live commands). Exit codes: 0 success, 1 failure, 2 usage error, 3
refused by confirmation or safety rules.
