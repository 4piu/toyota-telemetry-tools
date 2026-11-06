# How it works

The toolkit turns a list of *candidate* diagnostic signals (names, identifiers,
bit positions, and formulas that some source claims a vehicle supports) into a
list of signals you have seen working on your own vehicle. It does that in
four steps, which are also the four pages of the desktop application:

```text
 candidate list ─► 1 Set up ─► 2 Verify ─► 3 Collect ─► 4 Review and export
 (Techstream or    workspace,   probe ECUs,  drive while   which signals
  your own file)   adapter,     read each    targets are   changed, which
                   candidates   DID once,    being met     never moved;
                                set targets                signal list
```

Everything the tool sends is read-only diagnostics: tester present, session
control, and ReadDataByIdentifier. Commands and desktop actions for each step
are in the [quickstart](quickstart.md).

## What you need to find for your vehicle

The toolkit holds no vehicle facts. These are what you research, and where
each one ends up:

| You need | Example | Kept in | How to find it |
|---|---|---|---|
| A CAN adapter and its wiring | PCAN-USB on OBD-II pins 6 (CAN-H), 14 (CAN-L), 5 (ground) | Connector settings (this computer only) | The adapter's and vehicle's documentation; see [wiring checks](method/06-verify/usb-can.md) |
| Bus speed | 500 kbit/s, 11-bit identifiers | `config/transport.json`, `network` (500 kbit/s by default) | Service information; one answered OBD-II request proves it |
| How an ECU opens a diagnostic session | `3E 00`, then `10 03` | `config/transport.json`, a session recipe, written by hand | Watch what the OEM scan tool sends: [J2534 capture](method/03-locate/j2534-capture.md), then [reconstruct it](method/04-reconstruct/diagnostic-transport.md) |
| ECU addresses | Request `0x7E0`, response `0x7E8` | The candidate list, then confirmed endpoints | The OEM database's protocol records, or your own notes; Verify proves which ones answer |
| Signals | DID `0x1234`, bits 0..15, `raw / 1000`, V | The candidate list | [Techstream import](method/03-locate/techstream-database.md), or a file you write |
| When each signal moves | Cruise on, reverse, parking brake | Collection targets | What the signal means; plan drives around it |

Most of a Toyota's signal facts come from Techstream's own databases, so with
a licensed installation the candidate list and the ECU addresses are one
import. The session recipe is the one fact you always supply yourself.

## 1. Set up

**Workspace.** A project is one private folder, created by the tool and kept
outside any repository. It holds the transport settings, the candidate list,
your review, recordings, and a local cache. Nothing leaves it except by
export.

**Connector.** Choose the adapter type (PCAN, J2534, gs_usb, or slcan) and its
channel or device. Checking the connector runs the worker self-test without
opening the device.

**Session recipe.** Add one recipe to `config/transport.json`. Only session
control (`10`) and tester present (`3E`) are accepted:

```json
"session_recipes": [{
  "id": "extended",
  "steps": [
    {"request_hex": "3E 00", "expected_prefix_hex": "7E 00",
     "label": "tester present", "tolerate_negative": true},
    {"request_hex": "10 03", "expected_prefix_hex": "50 03",
     "label": "extended session", "tolerate_negative": false}
  ]
}]
```

**Candidate list.** Import it from one or more sources. Importing previews
first, showing counts, conflicts, and rejected rows, then applies.

- *Techstream* (Windows): reads the installed databases through Techstream's
  own library. It takes each Data List item's name, DID, bit range, formula,
  unit, state text, and its ECU's CAN addresses. You need your own licensed
  installation; nothing from it is bundled here.
- *A file*: a JSON array of rows, for any other source:

  ```json
  [{"ecu_key": 1, "ecu_name": "Engine",
    "tester_can_id": "0x7E0", "response_can_id": "0x7E8",
    "data_id": "0x1234", "first_bit": 0, "last_bit": 15,
    "name": "Battery voltage", "formula": "raw / 1000", "unit": "V",
    "states": {}}]
  ```

  `ecu_key` is any number that groups one ECU's signals. Bits count from the
  most significant bit of the first data byte after the `62 DID` echo.
  Formulas use `raw`, numbers, `+ - * / **`, and `signed(raw, WIDTH)`.
  `states` maps raw values to text, for example `{"0": "OFF", "1": "ON"}`.

## 2. Verify

Verification is the first step that transmits. You select a finite set of
endpoints or DIDs from the candidate list (never a range or a sweep), review
the exact scope, and confirm it.

- An endpoint that has not answered yet is probed first: the session recipe,
  sent to its request address. You then confirm each one that responds,
  which adds it to the transport settings.
- Each selected DID is read once. Its answer, response size, and latency are
  recorded; collection needs them to plan safe request rates.
- A negative response is recorded too, which tells you that this vehicle's
  calibration does not support the DID even though the database lists it.

Then decide what to collect. Set how often each DID is read, and give each
signal a **target**: what it still has to show, for example enough samples
across several runs, each listed state, or a number of value changes. You can
correct a signal's name, formula, unit, or states here as well.

## 3. Collect

Collection reads one endpoint at a time over one held diagnostic session,
within the request rate and bus load limits in the transport settings. It
skips signals whose targets are met and stops by itself once every target
is. With several endpoints it rotates, either in time slices or one after
another, and a run on each endpoint is recorded separately.

Plan drives around the targets: a signal that only moves in reverse, with
cruise control on, or while the parking brake moves will not change on an
ordinary drive. Keep the computer on mains power with its lid open. The tool
keeps it awake, and a passenger or an unattended logger operates it, never the
driver.

## 4. Review and export

Each signal ends up in one of four states, based on every valid recording:

| State | Meaning |
|---|---|
| Changed | The value moved while recorded. The DID and bits are live on this vehicle. |
| Constant | The ECU answered, but the value never moved. Its condition never happened, the signal is unused, or it is a stored snapshot. |
| Beyond response | The bit range lies past the end of the ECU's answer: this vehicle's calibration does not carry it. |
| Not recorded | Not read yet. |

"Changed" proves that the location is live. It does not prove the formula:
compare decoded values against a reference, such as the instrument cluster,
a known physical value, or a related signal (see
[verification](method/06-verify/README.md)). The Recordings page plots runs
for that, and a sample export gives CSV for other tools.

Exporting the result writes the signal list (JSON, CSV, and a readable
Markdown table per ECU) with each signal's state and the transport settings.
Recorded values never leave the workspace, because readings such as the
odometer identify a vehicle.

## Going deeper

The [method](method/README.md) collects the research notes behind each step:
[scope and safety](method/00-scope-safety.md) (read before connecting
anything), [locating metadata](method/03-locate/README.md),
[transport](method/04-reconstruct/diagnostic-transport.md),
[formulas and bitfields](method/05-decode/formulas-and-bitfields.md), and
[verification routes](method/06-verify/README.md).
[Progressive validation](progressive-validation.md) details targets and
collection.
