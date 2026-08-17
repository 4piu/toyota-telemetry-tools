# Progressive catalog validation

For the pipeline end to end, including catalog construction and post-capture
review, see [the quickstart](../quickstart.md). This page is the reference for
the qualification and capture phases.

Once a platform project has a candidate catalog, a confirmed diagnostic
endpoint, and a backend capable of `0x22 ReadDataByIdentifier`, the reusable
toolkit can qualify and sample candidates without building a new orchestration
script. The platform supplies policy; the toolkit supplies bounded execution,
immutable progress, structural validation, and offline summaries.

This workflow is deliberately limited to diagnostic reads. It is not an ECU
service scanner. The catalog/profile selects exact DIDs before live execution,
and the backend must enforce its own byte-exact transmit allowlist.

## Bootstrap a platform project

1. Put `telemetry-project.toml` at the platform repository root.
2. Generate or import a catalog using the documented catalog CSV columns.
3. Copy `templates/validation-profile.json` into the platform repository.
4. Fill in the profile ID, ECU key, bitrate, tester/response IDs, paths, DID
   range, tier policies, privacy words, and backend command.
5. Build a backend from [`backends/`](../../backends/README.md) and fill in
   `backend-config.json`. For a PCAN or J2534 adapter this is configuration
   only; for another adapter, implement `ICanChannel` per the
   [validation backend contract](../08-adapters/adding-an-adapter.md).
6. Build and review the private plan before connecting to a vehicle.

All relative paths in the validation profile resolve from the directory
containing `telemetry-project.toml`, never from a checkout name or its parent.
Plans and captures default to the ignored repository-local `scratch/` tree.

## Install or run the toolkit

From a checkout of this field guide:

```powershell
uv sync
uv run telemetry-build-plan --help
uv run telemetry-validate --help
```

A platform repository should pin a reviewed toolkit release or Git revision.
For example, after replacing `<REVISION>` with an immutable commit:

```powershell
uvx --from "toyota-telemetry-tools @ git+https://github.com/4piu/70Y074-telemetry.git@<REVISION>" `
  telemetry-build-plan --config data/validation/validation-profile.json
```

The Git remote is not required to be a sibling directory, and the platform
repository does not merge field-guide history.

## Build and inspect a plan

```powershell
telemetry-build-plan --config data/validation/validation-profile.json
telemetry-validate status --config data/validation/validation-profile.json
telemetry-validate qualify --config data/validation/validation-profile.json
telemetry-validate road --config data/validation/validation-profile.json
```

These commands are offline unless `--live` is supplied. The plan records the
catalog hash, endpoint, selection rules, tier policy, candidates, and exclusions.

`selection.identity_words` and `selection.private_words` can withhold DIDs whose
catalog names match, with `--include-private` admitting the latter. Both ship
empty. Withholding a DID costs coverage without protecting much: captures land
in the ignored `scratch/` tree, and a generated map carries signal definitions
rather than readings. Populate them only if your project publishes something
those defaults would not cover.

## Stationary qualification

Run qualification in the vehicle state documented by the platform experiment:

```powershell
telemetry-validate qualify `
  --config data/validation/validation-profile.json `
  --live --max-items 25
```

Each DID gets one complete response file. Positive responses enter the road
queue. Negative responses retain their NRC and interpretation. NRC `0x31` and
`0x33` are terminal by default, but the platform profile may choose a different
set. A timeout or transport failure is not converted into "unsupported."

## Bounded road or extended-state sampling

```powershell
telemetry-validate road `
  --config data/validation/validation-profile.json `
  --live --max-items 4
```

The runner groups qualified DIDs by tier and uses the configured request period,
duration, minimum sample count, and maximum batch width. There is one backend
process per batch. A file counts as progress only after:

1. the backend exits successfully;
2. the capture has exactly one final `session_end` with `reason=completed`;
3. the round-robin DID order, UDS prefixes, counts, timings, and retry fields
   pass structural validation;
4. every DID reaches its configured minimum sample count;
5. a summary is written and the capture is atomically renamed to
   `.complete.jsonl`.

Interruption asks the backend to close its transport, waits a bounded grace
period, and then quarantines only the active file as incomplete. The command
exits with status 130 and a short message rather than a Python traceback.
Earlier completed files remain progress, and later runs reconstruct state from
those immutable captures rather than trusting a mutable checklist.

## Focused custom experiments

Progressive completion is not the right place to repeat already-complete DIDs
under a particular condition. Copy
[`validation-experiment.toml`](../../templates/validation-experiment.toml),
give every batch an explicit purpose and vehicle state, and run it through the
same bounded backend and analyzer:

```powershell
telemetry-validate experiment `
  --config data/validation/validation-profile.json `
  --experiment data/validation/experiments/selected-followup-v1.toml

# After reviewing the dry-run schedule:
telemetry-validate experiment `
  --config data/validation/validation-profile.json `
  --experiment data/validation/experiments/selected-followup-v1.toml `
  --live
```

The TOML document defines named DID batches, request-slot period, duration,
minimum samples per DID, retry bound, repetitions, and operator-facing context.
Every DID must already be in the reviewed plan and have a positive completed
qualification. The platform batch-width limit still applies, and an impossible
minimum-sample target is rejected before the backend is opened.

A custom experiment has two valid completion paths. It may reach its configured
positive duration, or the operator may press Ctrl+C after completing the
planned stimulus. An operator stop is promoted only when the backend writes a
clean final `user_stop` record, every DID reached `minimum_samples_per_did`, the
round-robin schedule and endpoint validate, and the capture contains no error.
An earlier or malformed stop remains incomplete. Set `duration_seconds = 0` for
an unbounded operator-completed batch; the byte and directory limits remain in
force, so “unbounded” never means unlimited disk use. After accepting a manual
stop, the command exits successfully without starting another selected batch.

Experiment runs are configuration-hashed and resumable. A completed run is
skipped on the next invocation. A valid operator-completed custom run counts as
complete; a stop below the sample threshold or any other interrupted run remains
incomplete and is retried from its beginning. The status pass can recover a
previously quarantined `user_stop` capture when it matches the exact experiment
hash and now satisfies this rule. Increase `repetitions`, change the batch
definition, or use a new experiment ID when another independent observation is
required.
Use `--experiment-batch <batch-id>` one or more times to select conditions that
must not run in the same session.

Custom experiment captures use the ordinary candidate-capture schema and
offline summary, but their `experiment-` batch IDs place them in an independent
evidence namespace. They never satisfy or alter progressive road completion.

## Offline analysis

Any conforming candidate capture can be checked independently:

```powershell
telemetry-analyze-capture scratch/captures/vehicle-a/example.complete.jsonl `
  --catalog data/catalog/generated/telemetry.csv `
  --ecu-key 0 --minimum-samples 20
```

The analyzer reports support/NRCs, counts, latency, retries, raw ranges, formula
ranges, and whether each field changed. These are review aids. A plausible or
varying value does not automatically become Validated, and the tool never edits
a curated signal map.

Across every capture rather than one at a time:

```powershell
telemetry-review --config data/validation/validation-profile.json --pending
telemetry-review --config data/validation/validation-profile.json --check data/validated/signals.csv
```

`--pending` lists signals that stayed constant or were seen in a single run —
the queue for the next custom experiment. `--check` holds a curated map to the
observations and exits nonzero when a row claims more than was seen.
