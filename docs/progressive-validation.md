# Adaptive evidence collection

The schema-2 workflow replaces fixed qualification and road batches with one
objective-driven loop. Collection settings explain a run; they never decide
whether that run's observations remain visible later.

## Sources and derived state

Tracked transport, policy, signal review, and catalog provenance are the
portable authorities. Raw captures, normalized catalog rows, admitted plans,
adapter settings, and the SQLite query cache remain private under the project's
ignored `scratch/` tree. The cache can be deleted and rebuilt without losing a
human decision.

```powershell
telemetry cache rebuild --catalog-rows scratch/catalog-rows.json
telemetry capture inspect
```

## Define objective-specific completion

Use `telemetry-tui` to curate acquisitions and describe what evidence a
signal actually needs. Available counters cover total or changed observations,
distinct values, transitions, required states, applicable duration, elapsed
span, and distinct reviewed runs. Continuous change objectives require a
reviewed deadband. Structured applicability can depend on other collected
signals and has explicit true, false, and unknown states.

Descriptive or slow-changing values need not vary to earn the counters their
objective declares. Dynamic values do not complete merely by repeating a flat
sample. Existing structurally valid observations contribute after policy edits
without being invalidated by their old settings.

## Compile and inspect the run plan

```powershell
telemetry plan
```

The offline planner resolves each signal objective into a DID acquisition,
replays prior evidence, estimates p95 transaction cost, checks storage and
transport limits, and writes a self-contained admitted plan. It refuses empty
work, already-complete work, unresolved identities, stale sources, and missing
safety/cost facts.

The deterministic scheduler orders one endpoint's work by:

1. overdue maximum interval;
2. desired cadence using evidence deficit, opportunity, priority, aging, and
   measured cost;
3. slow/background work that fits available slack.

There is one outstanding request and no catch-up burst.

## Collect an anytime-valid prefix

```powershell
telemetry collect
```

Collection is a live verb, not a mode of an offline command. The collector
keeps one endpoint session open, rotates immutable 60-second/16-MiB segments
without replaying setup, and updates objective deficits in memory. Completed
segments remain useful after an operator stop, storage limit, or transport
loss. A crashed partial earns no progress until explicit offline recovery
copies its valid prefix into a new finalized segment.

Heartbeat, response-pending, disconnected, transport-error, and stale-state
remain distinct. The interface's button, hotkey, and Ctrl+C all send the same
idempotent stop request.

## Review evidence and export

```powershell
telemetry-tui
telemetry export
```

Evidence review reads all valid observations across all runs. Presentation may
filter by run, time, or settings, but those filters do not mutate stored data
or current objective replay. Saving review or policy is explicit and atomic.
Export creates a new immutable, privacy-minimized generation and never reads a
previous generated CSV as input.
