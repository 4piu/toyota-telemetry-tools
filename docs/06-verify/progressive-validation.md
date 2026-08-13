# Progressive catalog validation

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
5. Implement or configure a backend that follows the
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
Identity-bearing candidates are never scheduled. Privacy-sensitive candidates
require an explicit plan-build opt-in.

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

Interruption or failure quarantines only the active file as incomplete. Earlier
completed files remain progress, and later runs reconstruct state from those
immutable captures rather than trusting a mutable checklist.

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

