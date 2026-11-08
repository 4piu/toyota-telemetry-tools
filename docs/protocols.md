# Diagnostic protocols

What the toolkit knows about how Toyota (and Toyota-sold, partner-built)
ECUs are reached and read, and which of it the toolkit supports. Every
statement is marked by how it is known:

- **Seen on a vehicle**: answered as described during a bounded read-only
  verification.
- **Database**: read from Techstream's databases; not yet checked on a
  vehicle.
- **Inferred**: an interpretation of database fields that fits everything
  seen so far but is not proven.

## Overview

| Transport | Read | Status | Known by |
|---|---|---|---|
| CAN, normal addressing | `22 DID` or `21 LL` | Supported | Seen on a vehicle |
| CAN, extended addressing behind a gateway | `22 DID` or `21 LL` | Supported | Seen on a vehicle |
| CAN on partner-built models (Subaru, Mazda, Daihatsu) | `22 DID` or `21 LL` | Imported; session setup and response IDs unchecked | Database |
| K-line (KWP2000 and older Toyota DLC) | other | Not supported | Database, inferred |
| Diagnostics over Ethernet (DoIP) | `22 DID` | Not supported | Database |
| Peugeot-built models' own scheme | other | Not supported | Database, inferred |

The toolkit sends only tester present (`3E 00`), session control (`10 01`,
`10 03`), and the two reads. Everything else is refused by the workers'
transmit allowlist.

## CAN

Classical CAN at 500 kbit/s with 11-bit identifiers, on OBD-II pins 6
(CAN-H) and 14 (CAN-L); see [Wiring](how-it-works.md#wiring). Messages
travel as ISO 15765-2 (ISO-TP) frames, padded to 8 bytes with `00`.

### Normal addressing

Each ECU has its own request ID, and it answers on request + 8 (`0x7E0` →
`0x7E8`). Techstream's database stores only the request ID; the response ID
is a convention the toolkit takes as a candidate until an ECU answers on it.

| Frame | Layout |
|---|---|
| Single frame | `0L` + up to 7 message bytes |
| First frame | `1L LL` (12-bit length) + the first 6 bytes |
| Consecutive frame | `2n` (n = 1…F, 0, 1, …) + the next 7 bytes |
| Flow control (tester) | `30 00 00` (send the rest, no gap) |

### Extended addressing (gateway)

Many body, chassis, and driver-assistance ECUs sit behind a gateway that
shares one request ID, usually `0x750`, answered on `0x758`. The first byte
of every frame, in both directions, is the address of the ECU meant, and
the frame above follows it one byte shorter:

| Frame | Layout |
|---|---|
| Single frame | `AA 0L` + up to 6 message bytes |
| First frame | `AA 1L LL` + the first 5 bytes |
| Consecutive frame | `AA 2n` + the next 6 bytes |
| Flow control (tester) | `AA 30 00 00` |

Several ECUs may answer on `0x758` at once; frames with another address
byte belong to someone else and are ignored. The toolkit treats each address
as its own endpoint (`750-758-2a` is address `0x2A`). Candidate files give
the address as `target_address`, and `endpoint add ... --address 0x2A` (or
the Address column of Add candidates > Manual) adds one by hand.

### Sessions

An ECU must be in the right diagnostic session before it answers reads.
What opens it differs per ECU and is kept as a session recipe (see
[Finding the session setup](how-it-works.md#finding-the-session-setup)).
Seen on a vehicle:

| Request | Positive reply | Notes |
|---|---|---|
| `3E 00` | `7E 00` | Tester present. Older ECUs refuse it (`7F 3E 12`); the refusal is harmless. |
| `10 01` | `50 01` | Default session. Some older ECUs answer a bare `50`. |
| `10 03` | `50 03` | Extended session. Some older ECUs refuse it. |

Common recipes are tester present then `10 03`, tester present then
`10 01` then `10 03`, and, for some older ECUs, tester present alone. With
several recipes, verification tries each until one opens a session.
Reading every few seconds keeps a session open.

### Reads

| Request | Reply | Used by |
|---|---|---|
| `22 HH LL` | `62 HH LL` + data | Current ECUs: a two-byte data identifier (DID) |
| `21 LL` | `61 LL` + data | Older ECUs: a one-byte local identifier |

Data starts after the echo: three bytes for `62`, two for `61`. Bit 0 is
the most significant bit of the first data byte; see
[Decoding](how-it-works.md#decoding).

DIDs `0x0000`–`0x00FF` are not used for `22` reads, so the toolkit stores a
local identifier `LL` as DID `0x00LL` and reads every DID below `0x0100`
with `21 LL`.

An older ECU answers `22` with `7F 22 11` (service not supported); read it
with `21`. Older ECUs are often, but not only, behind the gateway.

### Negative replies

A refusal is `7F <service> <code>`. Seen on a vehicle:

| Code | Meaning | What the toolkit does |
|---|---|---|
| `78` | Response pending | Waits for the real reply (bounded). |
| `11` | Service not supported | Records it; for `22`, the ECU may need `21`. |
| `12` | Sub-function or identifier not supported | Records it; for `21 LL`, this ECU version has no such local identifier. |
| `31` | Request out of range | Records it; the DID is not in this calibration. |
| `13` | Wrong length or format | Records it. |
| `22`, `7E`, `7F` | Conditions not met, or not in this session | Records it; the session may need reopening. |

A database lists every version of an ECU it covers, so refusals for listed
items are normal.

## Techstream's databases

The [Techstream import](how-it-works.md#1-set-up) reads these through
Techstream's own library; nothing from them is bundled with the toolkit. A
region folder (`NA`, `EU`, `OT`, `JP`) holds one `.ddb` file per ECU
database, listed in a catalog keyed by an ECU key.

### Generations

| Generation | Data list | Read with | Import |
|---|---|---|---|
| Phase 5 | Table `0x023E`: each item has its DID, bit range, conversion group, and state set | `22 DID` | Yes |
| Phase 4 | Table `0x020A` (84-byte items): bit range at `0x3E`/`0x40`, local identifier at `0x4F`; table `0x0206` lists the local identifiers | `21 LL` | Yes |
| Phase 3 and DLC | K-line databases | not CAN | No |

The import tells the generations apart by which data list a database holds,
not by its file name. Names vary: `Engine_P5.ddb`, but also partner-built
`*_P5FHI`, `*_P5MMC`, `*_P4DK`, and variants such as `Engine_AdjustP4.ddb`
(inferred: FHI, MMC, and DK are the Subaru, Mazda, and Daihatsu builds).

Phase-4 caveats, seen on a vehicle:

- The length table `0x0206` lists for each local identifier is not the
  reply length. Real replies were both shorter and longer. Items past the
  listed length fit the longer replies of other ECU versions, so the import
  keeps every item whose identifier is listed and lets verification record
  the real length. An item past that reply is absent on the vehicle.
- The conversion (formula and unit) decoded for some phase-4 items is
  wrong, for example a switch with a temperature unit. Treat phase-4
  formulas as unconfirmed until values agree with a reference.

### Protocol records

Table `0x010D` holds one or more 28-byte protocol records per ECU key:

| Offset | Meaning | Known by |
|---|---|---|
| `0x06` | Functional request ID (e.g. `0x7DF`) | Database |
| `0x08` | Physical request ID | Seen on a vehicle |
| `0x0A` | Address byte, with extended addressing | Seen on a vehicle |
| `0x18` | Transport | See below |

The transport byte, with what each value appears to mean:

| Value | Transport | Known by | Import |
|---|---|---|---|
| `0x12` | CAN, normal addressing | Seen on a vehicle | Yes |
| `0x22` | CAN, extended addressing | Seen on a vehicle | Yes |
| `0x14` | Diagnostics over Ethernet (ECU names such as Ethernet switch and LiDAR) | Database | No |
| `0x11`, `0x17`, `0x21`, `0x51` | K-line variants; phase-3 ECUs list several, with one-byte addresses | Inferred | No |
| `0x15` | Older Toyota DLC (`*_DLC1`, `*_DLC2`) | Inferred | No |
| `0x1A`, `0x4A`, `0x5A` | Peugeot-built models (`Engine_PSA`, BSI on `0x752`) | Inferred | No |
| `0x62` | One Mazda-built ECU | Database | No |
| `0x16`, `0x31`, `0x32`, `0x38` | Generic OBD-II (`*_CARB`); `0x32` uses `0x7DF`/`0x7E8` | Inferred | No |

An ECU with several records is imported through its CAN record; one with no
CAN record is left out once, with its transport named. No ECU in the NA
database lists more than one CAN request ID or address.

## Not supported, and why

- **K-line** (OBD pin 7, roughly pre-2008 vehicles): different hardware,
  slow initialisation, and different framing. Out of scope.
- **DoIP** (Ethernet on the OBD port): reads are the same `22 DID`, but it
  needs an Ethernet adapter and a TCP/UDP session layer. Only the newest
  driver-assistance ECUs use it.
- **Peugeot-built models**: their own message scheme and IDs.
- **CAN FD and 29-bit identifiers**: rejected in configuration.
- **Generic OBD-II**: covered by many other tools.
