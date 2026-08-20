# Firmware

`telemetry-slcan` speaks the LAWICEL/slcan ASCII protocol over a CDC-ACM
virtual COM port. It works with any adapter whose firmware answers that
protocol; nothing here is required to *build* the backend. This directory
records what was actually flashed and verified, and how to reproduce it for
a CANable v1.0 nano (DykbRadio-style clone, STM32F072C8T6, 128 KiB flash).

## Firmware used

Fork: [`FlyingRC-Official/canable-fw-stm32f072cb`](https://github.com/FlyingRC-Official/canable-fw-stm32f072cb)
at commit `cff21e9`, itself a port of
[`normaldotcom/canable-fw`](https://github.com/normaldotcom/canable-fw) (MIT /
BSD / ST Liberty V2, see the fork's `LICENSE.md`) from STM32F042 to
STM32F072CB.

The fork's own `PORTING_STM32F072CB.md` documents a port done for a
different, unfamiliar schematic ("the schematic provided on 2026-05-08"), not
this board. Comparing its pin assumptions against candleLight_fw's
`DSD_TECH_SH_C30A` board profile — already verified correct on this exact
hardware via the `gsusb` backend — found two mismatches, fixed in
[`dsd-tech-sh-c30a.patch`](dsd-tech-sh-c30a.patch):

- **LEDs**: the fork assumed three LEDs on PA0/PA1/PA2. This board has two,
  on PB0/PB1 (confirmed against the verified `candleLight_fw` profile).
  `led.h`/`led.c` reverted to the original upstream `canable-fw` pinout, which
  already matched.
- **Transceiver standby pin**: the fork drives PC13 as a GPIO output for the
  TJA1051's S/standby pin. This board's verified profile defines neither
  `CONFIG_PHY_STANDBY` nor `CONFIG_PHY_SILENT` — there is no such line to
  drive here, and doing so risked contending with whatever PC13 actually
  connects to on this board. Removed entirely; `M1` (silent mode) still works
  at the CAN peripheral level, just not at the transceiver.

A third, unrelated bug surfaced during verification and is included in the
same patch: `TX_BUF_SIZE` (64 bytes) silently dropped the `V` (version)
response once `GIT_REMOTE` pointed at this fork's longer URL —
`CDC_Transmit_FS` refuses an over-size message outright rather than
truncating it. Raised to 128 bytes.

## Verification performed

Against the actual board, not just by reading the source:

1. Command-level: `V` (version, after the buffer fix), `S6`/`O`/`C`
   (bitrate/open/close), and unknown-command NACK all round-tripped
   correctly over the serial port.
2. Bus-level: a direct CAN_H/CAN_L short did **not** produce a loopback (a
   real short leaves zero differential voltage, which reads as recessive —
   it does not fake a dominant bit). A temporary build forcing
   `CAN_MODE_LOOPBACK` in `can.c` (STM32 bxCAN's internal self-test mode,
   reverted before real use) proved the CAN peripheral, bit timing, and the
   slcan encode/decode path all work: a transmitted frame came back
   byte-exact, both from a raw serial probe and through the compiled
   `SlcanChannel` C# implementation.
3. The spare unit's original `candleLight_fw` firmware was backed up (full
   128 KiB, DFU upload) before this firmware replaced it.

## Rebuilding

```sh
git clone https://github.com/FlyingRC-Official/canable-fw-stm32f072cb.git
cd canable-fw-stm32f072cb
git apply /path/to/dsd-tech-sh-c30a.patch
make
```

Needs `arm-none-eabi-gcc` and `make` on `PATH`; no installer required — the
[xPack](https://xpack.github.io/) GCC and Windows-build-tools releases are
portable zips. Flash with `dfu-util`, boot jumper/button held while
connecting:

```sh
dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D build/stm32f072cb-slcan-<rev>.bin
```

Back up whatever is currently on the device first — full 128 KiB, not the
32 KiB an F042-sized read would give:

```sh
dfu-util -d 0483:df11 -a 0 -s 0x08000000:0x20000 -U backup.bin
```
