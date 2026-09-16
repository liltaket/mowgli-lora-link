# Mowgli LoRa Link

Standalone prototype firmware and host tooling for a symmetric USB-to-LoRa
link built from two Seeed XIAO ESP32-S3 boards with Wio-SX1262 B2B radios.
Both boards run the same modem image. BASE and ROBOT are host-side roles.

```text
base host -> USB -> ESP32-S3/SX1262 )) LoRa (( SX1262/ESP32-S3 -> USB -> robot host
```

The repository currently contains the radio modem, its binary USB contract,
mock hosts, and a versioned application-protocol test layer for remote STOP
requests, compact telemetry, and freshness-aware RTCM3 fragmentation. Future
Raspberry Pi adapters will live here, but no Pi, ROS 2, GNSS receiver, or real
mower-control integration is included yet.

> [!WARNING]
> This is prototype communication software. It is unauthenticated, has no
> replay protection, and is not a safety system. `STOP_REQUEST` is a mock
> high-priority software request, not a safety-rated emergency stop. Never use
> this repository to bypass local mower safety, motor, or blade controls.

## Hardware

- 2 × Seeed XIAO ESP32-S3
- 2 × Seeed Wio-SX1262 B2B radio boards
- 2 × suitable connected 868 MHz antennas
- 2 × USB data cables

See [hardware and pinout](docs/HARDWARE.md).

## Quick start

Install the pinned development tools:

```bash
python3 -m pip install -r requirements-dev.txt
```

Build the identical modem image:

```bash
cd firmware/lora_radio
pio run -e modem
```

Identify both USB devices again before flashing; port names are not stable.

```bash
pio run -e modem -t upload --upload-port <BASE_PORT>
pio run -e modem -t upload --upload-port <ROBOT_PORT>
```

Run the basic bounded two-way bench:

```bash
python3 -m tools.lora_usb.bench \
  --base-port <BASE_PORT> \
  --robot-port <ROBOT_PORT> \
  --count 10 --interval 1.0
```

Run the pure tests:

```bash
python3 -m unittest discover -v
ruff check tools
python3 -m py_compile tools/lora_usb/*.py
```

The mixed STOP/telemetry/RTCM runner and its safe test modes are documented in
[`tools/lora_usb/README.md`](tools/lora_usb/README.md).

## RF and regulatory boundary

The current shared bench profile is 868.3 MHz, 250 kHz bandwidth, SF5,
CR 4/5, +2 dBm, private sync word, LoRa PHY CRC, and a 12-symbol preamble.
It is technically exercised on a table only; it is not an approved EU868
channel-access design.

The nominal synthetic multi-constellation RTCM profile is about 1.7 kB/s and
would occupy roughly 66% of RF airtime with this profile. Do **not** run a long
over-the-air full-rate soak merely because the modem can encode it. Continuous
operation requires a separate EU868 duty-cycle/LBT/AFA and channel-plan review.

## Documents

- [Hardware and pinout](docs/HARDWARE.md)
- [USB modem protocol](docs/lora/USB_MODEM_PROTOCOL.md)
- [Application protocol](docs/lora/APPLICATION_PROTOCOL.md)
- [Initial physical bench evidence](docs/lora/PHASE2_BENCH.md)
- [Current mixed-traffic test results](docs/TEST_RESULTS.md)
- [Roadmap and integration boundary](docs/ROADMAP.md)

## Repository status and license

Protocol layouts and test vectors are versioned, but the design remains a
prototype and may change incompatibly before a release.

No open-source license has been selected yet. Public visibility does not by
itself grant permission to copy, modify, or redistribute the code. Add an
explicit license before inviting external reuse or contributions.
