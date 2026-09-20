# LoRa USB modem prototype firmware

This Phase 2 image is identical for both Seeed XIAO ESP32-S3 + Wio-SX1262 B2B
boards. It is an opaque binary modem: it has no base/robot role, no Mowgli
commands, RTCM, ROS2, motor, blade, or mower-control integration.

Its USB and air contracts are documented in
[USB_MODEM_PROTOCOL.md](../../docs/lora/USB_MODEM_PROTOCOL.md). USB CDC carries
only COBS-delimited binary frames with CRC16; do not attach a line-oriented
serial monitor while a host test owns the modem port. `RADIO_TX_RESULT` proves
only local SX1262 TX completion, never remote delivery.

`GET_DIAGNOSTICS` exposes additive per-stage counters for USB requests, radio
TX/RX, RX-event queueing, and complete USB writes without changing the original
`LINK_STATUS` payload. This is intended for loss localization, not application
control.

## Hardware and RF boundary

Use an antenna on each radio before any transmission. Kit wiring is SCK=7,
MISO=8, MOSI=9, NSS=41, DIO1=39, RESET=42, BUSY=40. The bench profile is
868.3 MHz, 500 kHz, SF5, CR4/5, a 12-symbol preamble, private sync word, PHY
CRC, and +10 dBm.
It is prototype-only and regulatory-unverified; it is not a range, EU868,
coexistence, duty-cycle, or production-link claim.

`modem_lab_17dbm` is a separately named lab build which sets the SX1262
conducted-power configuration to +17 dBm at compile time. It leaves the
normal `modem` profile at +10 dBm. Building that profile is not permission to
transmit: the operator remains responsible for licence conditions, frequency,
ERP (including antenna gain), duty cycle, and all applicable rules.

## Build and supervised flash

Build as the normal user:

```bash
uvx --with intelhex platformio run -e modem
```

Build the explicitly lab-only +17 dBm variant without flashing:

```bash
uvx --with intelhex platformio run -e modem_lab_17dbm
```

Only intentionally supervised table tests should flash the two boards, using
the same image on both:

```bash
uvx --with intelhex platformio run -e modem -t upload --upload-port <BASE_PORT>
uvx --with intelhex platformio run -e modem -t upload --upload-port <ROBOT_PORT>
```

macOS port suffixes can change; re-identify hardware before upload. Use the
mock host under `tools/lora_usb/` for the bounded physical test. A failed
firmware protocol self-test leaves the radio unavailable; `INFO` then reports
`radio_ready=0` and link-status counters expose the failure.

Keep the explicit `board_build.flash_mode = dio` setting. On the tested target,
rewriting offset `0x0` with a QIO image header left the board in a
`TG0WDT_SYS_RST` loop before USB modem code ran. If an installed board shows
that exact boot log, reflash only the generated bootloader at `0x0` with
esptool's `--flash-mode dio`, then flash the application at `0x10000`. An
app-only flash at `0x10000` cannot repair a bootloader header already stored at
`0x0`. Verify the bootloader input hash and the esptool write verification; do
not erase flash or rewrite partitions as part of this recovery.

The first two-board mock result is recorded in
[`PHASE2_BENCH.md`](../../docs/lora/PHASE2_BENCH.md), including the observed
host-side prototype caveat and the boundaries that remain unproven.

## Radio startup recovery

The USB protocol starts before SX1262 initialisation. Radio startup runs in a
separate bounded FreeRTOS task: it pulses reset, waits at most 150 ms for BUSY
to go low, and performs at most three attempts with a 75 ms RadioLib BUSY/SPI
timeout and 250 ms backoff. This keeps `HELLO`, `INFO`, and status requests
responsive if the radio is absent or wedged. `radio_ready` becomes `1` only
after configuration and the first receive transition succeed; otherwise it
stays `0`, `radio_errors` increases, and `RADIO_SEND` returns `E_NOTREADY`.
The recovery path does not send any air traffic autonomously.

For a wedged installed board that cannot be power-cycled, build the USB-only
recovery image with `uvx --with intelhex platformio run -e
modem_usb_recovery`. It intentionally skips all SX1262 pin and SPI activity,
keeps `radio_ready=0`, and rejects `RADIO_SEND`; use it only to regain USB
diagnostics before flashing a repaired normal image. First establish whether
the application is running: a ROM log ending after its first `load:` line and
repeating `TG0WDT_SYS_RST` is a bootloader/flash-mode failure, not an SX1262
failure. A responsive USB-only image rules the boot path in and isolates later
radio recovery tests from RF activity.
