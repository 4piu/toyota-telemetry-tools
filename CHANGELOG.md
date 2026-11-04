# Changelog

The toolkit is installed on its own; a workspace moves when its owner updates
the installed toolkit. This file exists so that decision can be made without
reading the diff.

**Breaking** entries require a change in the workspace before its commands
will run. Everything else is safe to take.

## Unreleased

### Breaking

- The toolkit is standalone. `telemetry-project.toml` is schema 3 with only
  `schema_version` and `project_id`; the `[toolkit]` pin and `[paths]` table
  are gone, and the workspace layout is fixed: `config/transport.json`,
  `config/collection-policy.json`, `config/catalog-manifest.json` (moved from
  `result/`), `review/signals.json`, `captures/` (was `scratch/captures/`), and
  `state/` for the rest of the former `scratch/` (local settings, database,
  plans, inventory, verification facts, previews, quarantine).
- `telemetry result generate` and Recordings > Generate result are replaced by
  `telemetry export FOLDER` and Recordings > Export result. The export writes
  the result generation and `result/current.json` plus exact copies of the
  curated inputs into a deliverable folder outside the workspace, and refuses
  a folder holding another project's results. The manifest's
  `toolkit_commit` is the source checkout's commit (`-dirty` when modified)
  or null for an installed package.
- `tools/setup-platform-project.ps1` and `docs/updating-the-pin.md` are
  removed. Install with `uv tool install`; `backends/build.ps1` now builds the
  workers into `src/toyota_telemetry/workers/`, which wheels bundle, and a
  connector's `executable` may be null to use them.
- Endpoint and dynamic-DID provenance and review evidence references name
  documents in the deliverable repository; they are no longer resolved in the
  workspace and may not point into `captures/` or `state/`.

### Changed

- Desktop: a step bar replaces the page list and banner, showing per page
  what is done, next, or needs attention, including unsaved Signals edits.
- Desktop Signals page: standard row selection (Shift/Ctrl) replaces
  checkboxes; DID collection settings are table columns; a side panel edits
  collection settings, targets, and a signal's definition for the whole
  selection, showing "mixed" where rows differ and applying only changed
  fields. Selecting or filtering thousands of rows no longer stalls (it was
  quadratic in the row count).
- Desktop Signals page is a collection plan: endpoints are named by ECU and
  address (the endpoint ID, session, and hash move to details); one
  "Readable" column (Readable, Not checked, Refused) replaces the mixed
  status column; columns Every, Late after, Priority, Target; settings of
  DIDs that are not collected are dimmed. A Filter dropdown (Readable,
  Collect, Target, ECU groups; any within a group, all across groups)
  replaces the toggle buttons and the ECU key field. Target progress moved
  off this page: a met target shows only a tick.
- Recordings are grouped into drives (a pause of over a minute starts a new
  one; a drive is named by its start minute). `telemetry recordings` lists
  drives (`--runs` for individual runs), `show|recover|trash|restore DRIVE`
  accept a drive or any of its run IDs, `empty-trash` deletes permanently,
  and `export --drive` limits an export. A restorable trash replaces
  quarantine.
- Desktop Recordings page: a drives table (local start, length, ECUs,
  samples, status, note) replaces the runs table; selecting drives lists the
  signals they recorded and plots them on their own clocks. Delete moves
  drives to the trash, shown as a collapsible section with Restore and Empty
  trash. Export opens a dialog: selected or all signals and drives, and a
  format (CSV). Run and endpoint IDs are no longer shown.
- Desktop: the Next/Go row is gone; the next step is marked on the step bar
  and named in its tooltip. Export result moved to the Project menu. Pages no
  longer jump to other pages. Collapsible sections use ▸/▾ disclosure
  headers. Tooltips are a few words.
- Desktop Collect page: a Targets table shows every target's recorded
  samples and runs and what it still needs; double-click opens the signal on
  Signals. Endpoints are named by ECU and address; the ID stays in the
  confirmation's technical details.
- Desktop plot: dashboard interaction (hover reads the nearest sample with
  its run, click pins it, drag zooms into a time range, double-click shows
  all, sideways or Shift+scroll pans, pinch or Ctrl+scroll zooms time); the
  value axis fits the visible samples; run boundaries are marked and the
  selected run is shaded. Plain vertical scrolling no longer zooms.

### Added

- `telemetry init FOLDER` and Project > New project create an empty
  workspace. A transport may now have no endpoints or session recipes yet.
- The vehicle-independent method moved here from the retired template
  repository, under `docs/method/`.

### Fixed

- A collection run now completes on its own endpoint's targets. Previously
  its live ledger held every target in the plan, so with targets on two
  endpoints a run never stopped by itself, and readiness and the progress
  bar counted the other endpoint's targets.
- Signal series, plots, and CSV export order runs by recorded start time
  instead of run ID.
- Run summaries count a recovered segment once (the interrupted source it
  was made from no longer appears beside it), and interrupted runs report
  the reads they hold.

### Changed

- Desktop usability fixes from a fresh-user review: each signal row has its
  own status (Not checked, Responds, Recorded, Reviewed, or Refused) with the
  review evidence state chosen from a fixed list; refusals are explained in
  words; filters untick the rows they hide; endpoints without a known CAN
  address cannot be ticked for verification; search expands to its matches
  and edits keep the tree expanded; DID intervals have their own column; the
  endpoint dialogs check their fields before closing and the confirmation
  checks that the evidence document exists; the next-step Go button selects
  the item it refers to; Recordings lists interrupted drives first, gives the
  runs table more room, shows readable times and plain end reasons, asks
  before Quarantine, and words outcomes as drives; the Start dialog names
  the signals to be recorded with raw details collapsed; Stop no longer
  carries the transmit icon; the run note clears after a run; and Help
  explains statuses, sessions, refusal codes, transitions vs changes, and
  interrupted drives.
- The desktop application no longer blocks while it reads the project.
  Every service call runs on one background worker; stale requests are
  skipped, pages refresh only when shown and out of date, plotted signal data
  is cached, and window activation refreshes only when project files changed.
  Selecting a new signal resets the plot to fit it; resizing keeps the zoom.
  The placeholder quality markers left from the mock are removed.
- Faster project reads for large capture trees: capture-file hashes are
  reused while size and modification time are unchanged (the capture-set hash
  is identical), finished-run summaries are cached, the observation summary is
  one SQL aggregate, and status counts interrupted runs without reading
  segments. Saving review/targets, confirming an endpoint, and recovering or
  quarantining a run no longer rebuild the cache inline; the next read
  refreshes it. Cache rebuilds use unique temporary files.
- The desktop application no longer shows command-line commands or CLI help
  anywhere. Buttons use short labels; descriptions and disabled reasons moved
  into tooltips, and explanatory paragraphs were removed from the pages. Live
  buttons keep the warning icon; their tooltip and confirmation dialog state
  that they transmit.
- Readiness problems carry a neutral `hint` and a separate CLI `command`
  (replacing `fix`); service error messages are interface-neutral, and
  verification raises `RecipeRequired` when a session recipe must be chosen.

## 0.5.0

### Breaking

- `telemetry` is now a complete noun-verb interface over the shared
  `OperatorService`, equal to the desktop application. Removed without
  aliases: `catalog import` (use `source import file|tis`), `discover
  endpoints` and `preflight dids` (one `verify` probes endpoints, then DIDs),
  `plan` (readiness is automatic; see `collect --dry-run`), `capture inspect`
  (`recordings`), and `export` (`result generate`). `cache rebuild` no longer
  takes `--catalog-rows`; it reads the candidate inventory written by
  `source import --apply`. Platform repositories must run
  `telemetry source import file scratch/catalog-rows.json --apply` once.
- Live commands now confirm: in a terminal they ask for `yes`; scripts must
  pass `--yes`, accepted only for the scope last printed by `--dry-run`.
  Exit code 3 means refused.
- Collection policy schema 2 replaces objective classes, `class_defaults`,
  `deadband`, and per-objective cadence with one `targets` entry per signal
  holding explicit `requirements` (`minimum_samples`, `minimum_runs`,
  `minimum_span_seconds`, `required_states`, `minimum_transitions`,
  `minimum_changes` with `minimum_difference`, `minimum_applicable_seconds`)
  and an optional `condition`. Cadence lives only on the shared
  `acquisitions` (endpoint+DID settings). `minimum_runs` counts every
  contributing run. Platform policies must be rewritten; empty policies only
  change `schema_version` to 2 and rename `objectives` to `targets`.
- Verification results move from the SQLite cache to ignored
  `scratch/verification-facts.json`, so cache rebuilds keep them. Cache schema
  is 3; older caches are rebuilt automatically.
- `tools/catalog/build-catalog.py` and `tools/techstream/build-ecu-profile.py`
  are replaced by the TIS source adapter; the query script moves into the
  package. Result `signals.json` reports `target_progress` instead of
  `objective_progress`.
- The desktop preview's seven mock pages are replaced by four real pages
  (Setup, Signals, Collect, Recordings).

- The unpublished `telemetry-tui` prompt prototype and console entry point are
  removed. The toolkit retains only the `telemetry` executable surface while a
  separate cross-platform desktop operator application is designed over
  library services. There is no compatibility alias.
- The private worker protocol's `exchange` command now names a
  `transaction_id` instead of resupplying `request_hex`; the worker resolves
  the identifier against its own immutable launch-time transaction table
  (itself unchanged in shape apart from a new required `transaction_id` field
  per entry). The transmit-time byte-exact allowlist (`EnforceUdsAllowlist`,
  `EnforceFrameAllowlist`, `BackendConfig.WorkerAllowlist`) is replaced by the
  built-in, vehicle-agnostic `TelemetryGuard`, which validates the shape of
  every resolved session-setup step and runtime transaction independently of
  the transaction table or plan that produced it. There is no compatibility
  fallback for the old wire shape.

### Added

- `OperatorService` with candidate inventory, file and TIS sources with
  previewed reconciliation, an endpoint/DID/signal tree with one verification
  vocabulary, a shared selector grammar, orchestrated bounded verification,
  manual endpoint candidates, endpoint confirmation, review/DID/target edits
  with impact previews, automatic collection readiness with actionable fixes,
  a project-wide live lock, run recovery/quarantine, CSV export, and an
  operations table with CLI and desktop parity tests.
- `telemetry status`, `check`, `connector`, `signals`, `endpoint`,
  `signal set`, `did set`, `target set|clear`, and `recordings`.
- A collector `progress` event with overall target completion.
- Desktop application pages over real project data, with drafts, external-edit
  detection, exact-scope live confirmation, and live progress.
- Made the offline operator mock truthful and coherent after an independent
  usability pass: one labeled example inventory across pages, required-field
  preview checks without false validation claims, selection-bound target
  details and session-local drafts, functional example filters and finite
  verification checkboxes, direct readiness issue navigation with return path,
  first-use endpoint/DID help, and synthetic recording selectors that visibly
  change the plot. Real imports, adapter operations, project saves, and plan
  compilation remain disconnected.
- Reworked the offline interaction preview around user tasks: Connect hardware,
  Build signal list, Verify with vehicle, Set collection targets, Collect data,
  and Analyze recordings. Connector setup no longer contains endpoint/DID
  work; source selection and hierarchical verification are explicit; target
  controls separate definition, completion requirements, and recorded
  progress; and collection readiness replaces a top-level Plan page. Inclusion
  is directly checkable in the grouped tree, technical threshold jargon
  are replaced by an explicit threshold sentence, the unused global inspector
  is removed, and deferred source/diagnostic placeholders are absent.
  Navigation and harmless mock interactions work while live and
  project-mutating actions remain disabled.
- Added the optional `gui` dependency extra and `telemetry-ui` Foundation
  prototype. It starts offline with no project or adapter, establishes the
  PySide6 shell/navigation/status regions, exercises a virtual
  10,000-row signal table, and renders a synthetic million-point evidence
  source only after viewport-bounded first/minimum/maximum/last reduction.
  Latest-generation background jobs cancel superseded work and prevent stale
  results from repainting. Core installation and `telemetry` remain free of
  GUI imports and dependencies.
- `telemetry discover endpoints` and `telemetry preflight dids`: explicitly
  live, bounded commands completing the accepted Phase-F preparation
  workflow. Discovery probes operator-named candidate tester/response CAN ID
  pairs by replaying an already-reviewed session recipe; preflight probes
  operator-named candidate DIDs against a confirmed endpoint or a responding
  discovery candidate. Neither sweeps an ID range automatically, and neither
  edits tracked transport authority or authors a collection objective;
  results are provisional SQLite facts (`ProjectRepository
  .record_discovery_candidate` / `.record_did_preflight`, `.discovery_candidates()`
  / `.did_preflight_facts()`) with no durable source, so `telemetry cache
  rebuild` does not repopulate them. Both share the collect worker's
  built-in telemetry guard through a new bounded, single-shot `--probe
  --config PATH` process form (`toyota_telemetry.discovery`,
  `backends/Telemetry.Can.Core/ProbeProtocol.cs`). Promoting a responding
  discovery candidate into a tracked confirmed endpoint is a separate,
  explicit review action (`toyota_telemetry.discovery
  .promote_confirmed_endpoint`) that atomically writes
  `config/transport.json` through the existing authored-document writer.
- Discovery candidate IDs now include the session recipe and are also the
  eventual promoted endpoint IDs, preserving identity and preflight cost facts
  across curation. The endpoint-promotion library service remains the explicit
  tracked review boundary.
- DID preflight canonicalizes and enforces the tracked project DID range,
  correlates worker results with the requested candidate/backend, carries
  `tolerate_negative` into the probe worker, supplies positive latency/response
  facts to admission, and has a derived parent-process wall-clock deadline.
- The query-cache schema is version 2 after adding provisional discovery and
  preflight tables; older caches fail clearly and require `telemetry cache
  rebuild`. The generic dynamic-DID guard now validates every complete
  source-DID/position/size tuple.

### Fixed

- Corrected fresh-user defects in the offline operator prototype: minimum-size
  pages now scroll and dense panes stack; live actions and disabled reasons are
  explicit; DID-state and target columns are consistent; deep-link selection
  is visible; the idle run monitor is unambiguous; file fields browse; Help and
  draft state are visible; and analysis uses the shared example inventory with
  clock time, units, cursor detail, and a quality overlay. Project creation and
  vehicle bitrate remain in their existing template and transport authorities.

## 0.4.0

### Breaking

- Replaced the schema-1 project marker and `data/` configuration tree with the
  exact schema-2 marker, tracked `config/` and `review/` authorities, generated
  `result/` generations, and private derived state under `scratch/`. There is
  no old-schema reader or runtime migration fallback.
- Replaced all standalone console scripts with the single `telemetry`
  dispatcher. Offline intent uses `catalog import`, `cache rebuild`, `plan`,
  `capture inspect`, `review`, and `export`; live collection is the `collect`
  verb. The live-mode flag and fixed-tier batch commands were removed rather
  than aliased.
- Replaced short-lived backend batches with a Python-owned adaptive collector
  and a private long-lived C# transaction worker. Capture files now use
  immutable schema-1 run directories and segments; old capture files are not
  accepted.

### Changed

- The one-time legacy converter now preserves collection settings only in each
  run's non-authoritative snapshot and creates an empty active collection
  policy. Planning refuses an empty policy, while current objective replay and
  review queries continue to use all structurally valid observations across
  runs regardless of the settings under which they were recorded.

### Added

- A deterministic single-endpoint mixed-cadence scheduler core with hard
  maximum-deadline precedence, deficit/opportunity/cost scoring, aging,
  background slack filling, bounded failure backoff, stable ties, one
  outstanding request, and no catch-up bursts.
- Create-new native run/segment capture writing with plan snapshots, monotonic
  run-global sequencing, time rotation without session state, storage/disk
  bounds, clean valid-prefix finalization, crash quarantine, explicit prefix
  recovery, and incremental-ingestion equivalence with a clean cache rebuild.
- Offline objective/applicability replay, cost-based safety admission, exact
  capture inspection, and immutable generation-based CSV/JSON export for the
  schema-v2 project repository. These services do not open an adapter.
- Exact schema-v2 project repository primitives. The temporary one-time XA50
  converter was removed after its reconciliation output was accepted.
- `telemetry-gsusb`: a backend for candleLight_fw and its clone family (the
  gs_usb protocol) — CANable boards flashed with `candleLight_fw`, the
  reference candleLight board, cantact, and their many board-specific
  derivatives. Opens over native WinUSB first and falls back to libusb only if
  that fails, since one verified unit's automatic WinUSB binding refused
  `libusb_open`-style access for no diagnosable reason while a Zadig rebind to
  libusbK worked immediately; libusb's own Windows backend then handles either
  driver transparently. Verified end to end against real hardware: protocol
  handshake, computed bit timing, and a byte-exact loopback receive, not just
  that it opens.
- `telemetry-slcan`: a backend for the LAWICEL/slcan ASCII protocol over a
  USB CDC-ACM virtual COM port — no vendor driver, just
  `System.IO.Ports.SerialPort`. Verified against a CANable v1.0 nano
  (STM32F072) reflashed with board-adapted slcan firmware (see
  `backends/Telemetry.Backend.Slcan/firmware/README.md`): command
  handshake, ACK/NACK, and a byte-exact frame round trip through the CAN
  peripheral's internal loopback mode, exercised through the actual
  `SlcanChannel` implementation and not just a protocol probe.

### Fixed

- `telemetry-map` now counts matched curation identities rather than reviewed
  output rows. One curation identity can legitimately apply to duplicate OEM
  catalog records with different internal IDs; this no longer produces a
  nonsensical negative "matched no catalog signal" warning.

## 0.3.0

### Breaking

- **`curation.csv` requires an `ecu_key` column.** A DID and bit range identify
  a signal only within one ECU: `0x10B0` bits 16..31 is a boost voltage on one
  controller and a vapour pressure on another. Before this, one curated row
  claimed both, and `telemetry-map` then refused to write because the second
  claim was unsupported. Add `ecu_key` as the first column and set it on every
  row; `load_curation` rejects a file without it rather than guessing.
- **`telemetry-review --json` and `--csv` are now `--json-out` and
  `--csv-out`.** `--json` became the shared machine-output flag on every
  command, and the two collided. Scripts calling either need the new names.

### Added

- Human-readable output on every command, with `--json` for the machine
  document, `-v`/`-q` for detail, and `--no-progress` for unattended runs.
  Results go to stdout and diagnostics to stderr, so a pipeline gets only the
  result.
- A live progress line during capture: elapsed time and record count while the
  backend runs, instead of silence for the length of a batch. Off a terminal it
  becomes a periodic log line rather than carriage returns into a file.
- `--version` on every command.
- `setup-platform-project.ps1`: checks this repository out at a platform
  project's pinned revision, builds the backends, and installs the toolkit
  into the project's `.venv` from that same checkout. One revision fixes the
  Python tools, the backends, and the Techstream scripts together.
- `--text-code-page` on `build-catalog.py`, and the code page used is recorded
  in the catalog manifest.
- Generated plans, capture summaries, and the map document now record the
  `toolkit_version` that produced them.

### Fixed

- **Catalog text was decoded three different ways and none of them was
  stated.** Units containing a degree or micro sign arrived as replacement
  characters — 415 rows on the reference vehicle — and could not be recovered
  from the output. The database's code page, PowerShell's stdout encoding, and
  the caller's decoding are now each explicit. Rebuild the catalog to recover
  them; nothing else in the atlas changes.
- `telemetry-review --check` no longer reports rows belonging to other ECUs as
  unobserved, so one map file can be checked ECU by ECU.
- A deliberate stop during a multi-ECU sequence is no longer counted as one
  failed item and followed by the next ECU.

## 0.2.1 and earlier

Not recorded here. `git log` is the history; this file starts at the first
release that a platform repository could pin by tag.
