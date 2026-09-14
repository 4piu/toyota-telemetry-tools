# Changelog

A platform repository decides when to move: `toolkit.revision` in its
`telemetry-project.toml` is the only pin, and nothing here reaches downstream
until its owner changes it. This file exists so that decision can be made
without reading the diff.

**Breaking** entries require a change in the platform repository before its
commands will run. Everything else is safe to take.

Pin a tag rather than a branch — `revision = "v0.3.0"` — and
`setup-platform-project.ps1` resolves it to the commit it names. A branch is
not a pin: it moves under you.

## Unreleased

### Breaking

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
