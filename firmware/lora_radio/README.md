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
CRC, and +17 dBm.
It is prototype-only and regulatory-unverified; it is not a range, EU868,
coexistence, duty-cycle, or production-link claim.

## Build and supervised flash

Build as the normal user:

```bash
uvx --with intelhex platformio run -e modem
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

The first two-board mock result is recorded in
[`PHASE2_BENCH.md`](../../docs/lora/PHASE2_BENCH.md), including the observed
host-side prototype caveat and the boundaries that remain unproven.
