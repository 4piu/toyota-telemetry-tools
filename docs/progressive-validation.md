# Adaptive evidence collection

Collection is one target-driven loop. Collection settings explain a run; they
never decide whether that run's observations remain visible later. Each step
is available from `telemetry` and from the desktop application.

## Sources and derived state

Transport, policy, signal review, and catalog provenance are the curated
inputs; an export writes the result derived from them. Raw
captures, the candidate inventory, verification results, compiled scopes,
adapter settings, and the SQLite query cache stay private in the workspace
(`captures/` and `state/`). The cache can be deleted
and rebuilt without losing a human decision or a verification result.

```powershell
telemetry cache rebuild
telemetry recordings
```

## Define completion directly

A collection target says what evidence a signal still needs. There are no goal
classes; requirements are explicit:

| Requirement | Counts |
|---|---|
| Minimum samples | valid observations while the condition applies |
| Minimum runs | distinct runs that contributed |
| Minimum span | wall-clock coverage, including slow change across drives |
| Required states | raw states that must be observed |
| Minimum transitions | raw state changes |
| Minimum changes | consecutive decoded values differing by at least the minimum difference |
| Minimum applicable time | time observed while the condition is true |

A structured condition can depend on other collected signals and has explicit
true, false, and unknown states. A flat signal still earns samples and span; a
change requirement is not satisfied by repeating a flat value. Existing valid
observations contribute after policy edits without being invalidated by the
settings they were collected under.

Scheduler opportunity follows from what is unmet: unseen states and
transitions raise demand for state requirements, qualifying changes raise it
for change requirements, and samples, runs, and span accumulate at the shared
DID cadence. Complete targets stop contributing demand.

## Readiness and the run scope

```powershell
telemetry collect --dry-run
```

Readiness resolves each unfinished target to its endpoint+DID settings,
replays prior evidence, uses p95 latency and response size from verification
or earlier runs, checks storage and transport limits, and writes the compiled
scope under `state/plans/`. It refuses empty work, complete work, unresolved
identities, stale sources, and missing safety or cost facts, naming the command
that fixes each.

The deterministic scheduler orders one endpoint's work by:

1. overdue maximum interval;
2. desired cadence using evidence deficit, opportunity, priority, aging, and
   measured cost;
3. background work that fits available slack.

There is one outstanding request and no catch-up burst.

## Collect an anytime-valid prefix

```powershell
telemetry collect
```

The collector keeps one endpoint session open, rotates immutable segments
without replaying setup, and updates target deficits in memory. Completed
segments remain useful after an operator stop, storage limit, or transport
loss. A crashed partial earns no progress until explicit offline recovery
(`telemetry recordings recover RUN`) copies its valid prefix into a new
finalized segment.

Heartbeat, response-pending, disconnected, and transport-error states remain
distinct. A progress event reports elapsed time, reads, failures, and target
completion. Ctrl+C, `stop` on standard input, and the desktop Stop button all
make the same idempotent stop request.

## Review evidence and export the result

```powershell
telemetry recordings export --signal 372/0x1002/0..15 --output signal.csv
telemetry export PATH-TO-DELIVERABLES
```

Evidence review reads all valid observations across all runs. Filters change
presentation only. Saving review or policy is explicit and atomic. Export
creates a new immutable, privacy-minimized generation and never
reads a previously generated CSV as input.
