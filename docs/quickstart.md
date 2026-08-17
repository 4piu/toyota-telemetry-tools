# Quickstart: from a forked template to a telemetry map

This is the whole pipeline in order, on one page. Each step says what you fill
in, what runs, and how you know it worked.

It assumes you have a vehicle you are authorised to work on, a CAN adapter, and
somewhere safe to park. Read [scope and safety](00-scope-safety.md) before you
transmit anything.

The loop this builds toward:

```text
build plan -> qualify -> capture batch -> analyze -> review ledger -> map
                  ^                                        |
                  +------- custom experiment <-------------+
```

Discovery runs left to right. Everything after the first drive is the loop:
review tells you which signals are still unproven, a custom experiment excites
exactly those, and you analyze again.

---

## 1. Create the platform repository

Fork or copy this repository's structure — not its history. Your vehicle
project is its own repository that *consumes* this one.

```powershell
mkdir my-platform-project; cd my-platform-project; git init
```

Copy [`templates/telemetry-project.toml`](../templates/telemetry-project.toml)
to the root as `telemetry-project.toml` and set `project_id`, the field-guide
URL, and the revision you reviewed. Every relative path in every config
resolves from this file's directory — never from a checkout name or a sibling
directory.

Pin the toolkit in `toolkit-requirement.txt`:

```text
toyota-telemetry-tools @ git+https://github.com/4piu/70Y074-telemetry.git@<REVISION>
```

Create `scratch/` and ignore it entirely. Raw captures, plans, and the backend
binary live there and never enter Git. See the
[platform-project contract](09-platform-projects.md).

**Done when:** `telemetry-validate --help` runs from your project directory.

---

## 2. Build a candidate catalog

You need a list of DIDs worth trying, with names and bit ranges. Where that
comes from depends on what you have — see the [location routes](03-locate/README.md).
With a local Techstream installation:

```powershell
# Discover ECU keys
.\tools\techstream\query-techstream-db.ps1 -Catalog

# Join monitor, protocol, conversion, and unit records into a catalog
python .\tools\catalog\build-catalog.py --profile data\catalog\ecu-profile.json
```

Fill in [`templates/catalog-profile.json`](../templates/catalog-profile.json)
first: the ECU keys, their database files, and what evidence you have for each.

**Done when:** `data/catalog/generated/telemetry.csv` exists with
`ecu_key,data_id,name,first_bit,last_bit,formula,unit` columns. Rows at this
point are *Candidate* evidence and nothing more.

---

## 3. Find your diagnostic endpoint and session setup

This is the step that has no tool, and the one most likely to stop you.

You need four things for your target ECU: the CAN bitrate, the tester CAN ID,
the response CAN ID, and the **sequence of UDS requests that opens a session
in which DID reads are answered**.

The route that worked here: capture a real tester conversation over J2534 while
the manufacturer's diagnostic application displays a live data list, then read
the setup requests off the trace.

```powershell
python .\tools\j2534\parse-j2534-log.py path\to\j2534.log --timeline
```

See [capturing a J2534 conversation](03-locate/j2534-capture.md) and
[reconstructing the transport](04-reconstruct/diagnostic-transport.md).

There is currently no better path, and no way to derive this automatically.
See [open questions](open-questions.md).

Copy [`templates/backend-config.json`](../templates/backend-config.json) to
`data/validation/backend-config.json` and fill it in.

**Done when:** `backend-config.json` has real values and no `replace-me`.

---

## 4. Build a backend and prove the wiring

```powershell
# From a checkout of this field guide, built straight into your project
.\backends\build.ps1 -OutputDirectory C:\path\to\my-platform-project\scratch\bin
```

Verify offline first — this opens no device:

```powershell
.\scratch\bin\telemetry-pcan.exe --self-test --output scratch\analysis\selftest.jsonl
```

Then, with the vehicle stationary in Park and the parking brake on, in order:

```powershell
# Config parses and the endpoint reads back; nothing transmitted
.\scratch\bin\telemetry-pcan.exe --config data\validation\backend-config.json --probe

# Standardized OBD query: proves bit timing, TX, RX, filtering, wiring
.\scratch\bin\telemetry-pcan.exe --config data\validation\backend-config.json --obd-probe --live

# One tester-present to your endpoint: proves gateway routing
.\scratch\bin\telemetry-pcan.exe --config data\validation\backend-config.json --probe --live
```

If the OBD probe fails, the problem is electrical or bit timing, not the ECU.
A blinking adapter LED and an OK controller status are both entirely consistent
with swapped CAN-L and ground. Details in [backends/README](../backends/README.md).

**Done when:** both probes return the expected response.

---

## 5. Build and review the plan

Copy [`templates/validation-profile.json`](../templates/validation-profile.json)
to `data/validation/validation-profile.json`. Its `endpoint` must match
`backend-config.json`. Set the DID range, tier policies, privacy word lists,
and point `backend.executable` at `scratch/bin/telemetry-pcan.exe` with
`arguments: ["--config", "data/validation/backend-config.json"]`.

```powershell
telemetry-build-plan --config data\validation\validation-profile.json
telemetry-validate status --config data\validation\validation-profile.json
```

Read the plan before connecting to anything. It records the catalog hash, the
selection rules, every candidate, and every exclusion with a reason.
Identity-bearing candidates are never scheduled; privacy-sensitive ones need an
explicit opt-in.

**Done when:** the candidate count and tier split look deliberate.

---

## 6. Qualify, stationary

```powershell
telemetry-validate qualify --config data\validation\validation-profile.json          # preview
telemetry-validate qualify --config data\validation\validation-profile.json --live --max-items 25
```

Each DID gets one read and its own completed file. Positive responses enter the
road queue. Negative responses keep their NRC — `0x31` and `0x33` are terminal
by default. A timeout is never recorded as "unsupported".

**Done when:** `status` shows a qualified set. Expect most candidates to be
negative; a static database describes equipment your vehicle may not have.

---

## 7. Capture on the road

Read the [road-test protocol](06-verify/timing-and-load.md) and arrange a
passenger or an unattended logger. **Do not operate the computer while driving.**

```powershell
telemetry-validate road --config data\validation\validation-profile.json --live --max-items 4
```

Batches group qualified DIDs by tier, one outstanding request at a time. A
capture counts as progress only after the backend exits cleanly, the file ends
with `session_end reason=completed`, structural validation passes, and every
DID reaches its minimum sample count.

**Done when:** `status` shows road-complete DIDs. Interrupting is safe —
completed batches remain; only the active file is quarantined.

---

## 8. Review across every capture

```powershell
telemetry-review --config data\validation\validation-profile.json
telemetry-review --config data\validation\validation-profile.json --pending
telemetry-review --config data\validation\validation-profile.json --csv scratch\analysis\ledger.csv
```

This aggregates every completed capture into one per-signal ledger: how many
runs saw it, total samples, raw and decoded range across all runs, and whether
it ever changed.

`--pending` is the important one. It lists signals that stayed constant or were
seen in only one run — the ones you cannot yet claim anything about, and
therefore exactly the queue for the next experiment.

**Done when:** you can name which signals still need work without opening a
single capture file.

---

## 9. Excite the doubtful ones

A signal that never moved is not wrong; it may simply be event-driven and the
event did not occur. Classify before scheduling:

- **Safe controlled** — a parked or ordinary-driving action gives a clear
  reference (gear positions while stationary, turn signals, door locks).
- **Passive extended observation** — the state occurs naturally but must not be
  forced; capture longer across normal trips.
- **Do not induce** — activation would require unsafe or abusive operation.
  ABS, traction/stability intervention, collision and airbag states stay
  untested unless a naturally occurring event provides evidence.

Copy [`templates/validation-experiment.toml`](../templates/validation-experiment.toml),
give every batch an explicit purpose and vehicle state, then:

```powershell
telemetry-validate experiment --config data\validation\validation-profile.json `
  --experiment data\validation\experiments\selected-followup-v1.toml --live
```

Experiment captures live in their own evidence namespace and never alter
progressive completion. Then go back to step 8. That is the loop.

---

## 10. Curate the map

The ledger is evidence, not conclusions. Promotion stays manual and deliberate:
transport support, formula plausibility, enum meaning, and behavioural
correlation are separate claims, and the [evidence states](README.md) exist so
you do not collapse them into one "known" flag.

Write `data/validated/signals.csv` yourself, then hold it to the observations:

```powershell
telemetry-review --config data\validation\validation-profile.json --check data\validated\signals.csv
```

This flags any row claiming more than was observed — a `Correlated` state on a
field that never changed, or a bit range no capture ever covered. It exits
nonzero when something needs review, so it fits in CI.

**Done when:** `telemetry-map` writes without complaint and every reviewed
claim is one you would defend.

A signal that stayed constant still belongs in the map. A constant value does
not mean the formula is wrong — it means the field was not exercised, and
someone else's vehicle may exercise it.

---

## What you have now

A reviewed map of your vehicle's diagnostic telemetry with explicit evidence
states, reproducible from immutable captures, and an application repository can
consume it without ever touching a CAN bus.

Keep raw captures, the generated plan, and reverse-engineering state in
`scratch/`. Publish the map, the configs, and the findings.
