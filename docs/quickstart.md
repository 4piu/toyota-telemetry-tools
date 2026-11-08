# Toolkit quickstart

The toolkit is installed once and works on a project **workspace**: one
folder, outside any repository, that holds every working file. Only the
result leaves it, by export.

There are two complete, interchangeable interfaces over the same library:

- `telemetry`, a command-line interface for terminals and scripts; and
- `telemetry-ui`, the desktop application (install the `gui` extra).

Every step below names the command and the desktop page that does the same
thing. Use either, or both; they read and write the same workspace. For what
each step is for, read [how it works](how-it-works.md) first.

## Install

```powershell
uv tool install "toyota-telemetry-tools[gui] @ git+https://github.com/4piu/toyota-telemetry-tools"
telemetry --version
uv tool upgrade toyota-telemetry-tools      # later: update to the latest commit
```

Installing compiles the adapter workers into the package, where every
workspace finds them. Until releases begin, every snapshot has the same
version number; `telemetry --version` also shows the commit, and exported
results record it. To work on the toolkit itself, see
[development](development.md).

## Getting help

- `telemetry --help` lists the commands; `telemetry COMMAND --help` explains one.
- `telemetry status` (or the desktop step bar) shows the next useful step.
- In the desktop application, hover over a button for what it does, or over
  a disabled one for why it is unavailable.

## The desktop application

```powershell
telemetry-ui [PROJECT]
```

A step bar across the top is both the navigation and the progress view: each
page shows what is done, what is next, and what needs attention.

- **Setup**: project check, connector, and candidate sources.
- **Signals**: the collection plan, an ECU → DID → signal table showing what
  is readable, what is collected and how often, and each signal's target. A
  Filter menu and search narrow it; a side panel edits the settings, targets,
  and definition of everything selected (showing "mixed" where rows differ)
  as a draft until Save.
- **Collect**: automatic readiness, start and stop, live progress, and every
  target's progress.
- **Recordings**: runs by start time; selecting runs lists and plots their
  signals. Recovery, Delete (to the system trash), and sample Export are here.

New project and Export result are in the Project menu. The plot follows
dashboard conventions: hover reads the nearest sample, click pins it, drag
zooms into a time range, double-click shows everything, sideways scroll pans,
and pinch or Ctrl+scroll zooms time.

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

Curated inputs:

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
tester present are accepted), then add or import endpoints. Back up the
workspace yourself; it is the only copy of your review and recordings.

## 1. Set up (Setup page)

```powershell
telemetry connector set pcan --channel PCAN_USBBUS1
telemetry connector check
telemetry source import file C:\private\catalog-rows.json            # preview
telemetry source import file C:\private\catalog-rows.json --apply
telemetry source import tis --apply
telemetry source export C:\private\candidate-rows.json   # Save rows
```

`connector check` validates settings and runs the worker self-test without
opening the device. `source import` previews first: counts, conflicts,
rejected rows, and what changes compared with the current list; `--apply`
replaces that source's rows, updates the catalog manifest, and
rebuilds the cache. The TIS source needs Windows and installed Techstream
databases; it finds them in the standard install folder (NA, then EU, OT,
JP) unless `--database FOLDER` names one, and the desktop Setup page fills
the same folder in. It reads every installed ECU database, current
(phase 5) and older (phase 4) alike, including models built by partner
makers (see [protocols](protocols.md#techstreams-databases)),
and leaves out ECUs not on CAN; `--ecu KEY` limits it to chosen ECUs.
Candidate rows
never become reviewed authority by being imported.

`source export` (Save rows on the Setup page) writes the imported rows as a
row file that `source import file` reads back unchanged, for example to
start another workspace on a computer without Techstream; `--source ID`
saves one source only. Rows from Techstream carry its names and formulas:
keep the file private, like the workspace.

## 2. Verify (Signals page, LIVE)

```powershell
telemetry signals --state not-verified
telemetry verify 7a1-7a9 --dry-run
telemetry verify 7a1-7a9
telemetry endpoint confirm 7a1-7a9-extended --ecu 405 --note "answered at IG-ON"
```

Selectors are `ENDPOINT`, `ENDPOINT/0xNNNN`, `ENDPOINT/0xNNNN/FIRST..LAST`, or
a signal reference `ECU/0xNNNN/FIRST..LAST`, narrowed by `--ecu`, `--state`,
and `--search`. They choose only from the candidate list; there are no ranges
or sweeps. An endpoint that is not verified yet is probed first and its DIDs
are read only if it responds. Results (baseline bytes, latency, outcome) are
kept in `state/verification-facts.json`. `endpoint confirm` writes a
responding endpoint into the transport settings; `--note` keeps an optional
remark with it. `endpoint add
TESTER:RESPONSE:RECIPE --ecu KEY` adds an address the source did not provide;
`--address 0x2A` adds an ECU behind a gateway (extended addressing).

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
the command that fixes it. One run collects one endpoint and stops itself
when its targets are complete. When targets span several endpoints, collect
one (`--endpoint E`), or several in turn (`--rotate`, optionally with
`--endpoint E` repeated to choose and order them; on the Collect page, check
the ECUs and drag to order). You confirm every chosen endpoint's reads once;
each turn is then a run on one endpoint, endpoints whose targets are met are
skipped, and the collection ends when every target is met or you stop it.
Two strategies decide how long a turn lasts:

- `--strategy slices` (default, "Switch every few minutes"): at most one time
  slice (`--slice`, default 5 minutes), so every endpoint gets time even when
  some target cannot be met yet; a hand-over ends the run as "slice_end".
- `--strategy sequential` ("One after another"): each endpoint until its
  targets are met, in order. Fewer switches, but a target that cannot be met
  keeps the later endpoints waiting until you stop.

Only one endpoint is read at a time.

While a collection or verification runs, the computer and its display are kept awake,
because a sleeping computer stops reading the vehicle (on many laptops the
display turning off is what starts sleep). Lower the brightness if you like,
but keep the lid open: closing it still sleeps. Run on mains power: an empty
battery still ends the run.

Do not operate a computer while driving; use a passenger or an unattended
logger.

## 5. Analyze (Recordings page)

```powershell
telemetry recordings
telemetry recordings show RUN
telemetry recordings recover RUN          # interrupted run: keep its valid prefix
telemetry recordings trash RUN            # to the system trash (Recycle Bin)
telemetry recordings export --signal 372/0x1002/0..15 --run RUN --output battery.csv
telemetry export C:\work\my-car-result
```

Deleting a run moves its folder to the operating system's trash; restore it
there to bring the run back.
All valid runs are included. Export writes UTC, run, elapsed time, raw bytes,
decoded value, state text, and condition state for external plotting; the
desktop application plots the same data with run boundaries kept apart, and
draws a state signal as steps on an axis named by its states. `signals show`
lists a signal's states. `export`
writes the privacy-minimized result generation into a folder outside the
workspace (Project > Export result). A generation
holds `signals.json`, `signals.csv`, and `signals.md` (a readable reference,
one table per ECU) plus the transport and a manifest. Recordings appear only
as counts and a status: changed, constant, beyond response, or not recorded.
Recorded values never leave the workspace, because they include readings
such as the odometer. It refuses a
folder inside the workspace and one that holds another project's results.
The manifest records the toolkit version, and its commit when it runs from a
Git checkout.

Every command accepts `--json` for one machine-readable document (JSON Lines
for live commands). Exit codes: 0 success, 1 failure, 2 usage error, 3
refused by confirmation or safety rules.
