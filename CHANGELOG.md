# Changelog

A platform repository decides when to move: `upstream.revision` in its
`telemetry-project.toml` is the only pin, and nothing here reaches downstream
until its owner changes it. This file exists so that decision can be made
without reading the diff.

**Breaking** entries require a change in the platform repository before its
commands will run. Everything else is safe to take.

Pin a tag rather than a branch — `revision = "v0.3.0"` — and
`setup-platform-project.ps1` resolves it to the commit it names. A branch is
not a pin: it moves under you.

## Unreleased

### Added

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
