# LoRa USB modem Phase 2 bench result

Date: 2026-09-16

Status: prototype bench evidence, not production approval.

This historical bring-up used the earlier 8-symbol-preamble image identified
by the SHA-256 below. The current repository profile uses a 12-symbol preamble;
see `TEST_RESULTS.md` for current mixed-traffic evidence.

## Scope

Two identical Seeed XIAO ESP32-S3 + Wio-SX1262 B2B boards ran the same
`modem` firmware. BASE and ROBOT existed only as mock roles in one Mac host
process. No Raspberry Pi, ROS2 node, RTCM source, mower control, motor, or
blade path was connected.

The exercised path in both directions was:

```text
mock host -> USB CDC -> ESP32-S3 -> SX1262 LoRa -> SX1262 -> ESP32-S3 -> USB CDC -> mock host
```

Hardware identity at the final test:

| Mock role | USB port | Device identity |
| --- | --- | --- |
| Base | `<BASE_PORT>` | board A |
| Robot | `<ROBOT_PORT>` | board B |

The port suffix is topology-dependent and is not a stable identity.

## Build and host checks

- `uvx --with intelhex platformio run -e modem`: passed.
- Firmware size: 22,496 bytes RAM (6.9%) and 295,125 bytes flash (8.8%).
- Flashed firmware binary SHA-256:
  `838d67bc6e59bfccb47d3fad1b968aa59b66d6509d93b9412c6c60c8eced977a`.
- `python3 -m unittest tools.lora_usb.test_protocol -v`: 7/7 passed.
- `uvx ruff check tools/lora_usb`: passed.
- `python3 -m py_compile tools/lora_usb/*.py`: passed.
- `git diff --check`: passed.

The pure tests cover the CRC check vector, COBS round trips and recovery,
length/CRC corruption, signed RSSI/SNR values, typed response payloads, the
mock application envelope, wrong-session filtering, and preservation of a
radio event that arrives before a correlated TX result.

## Physical protocol probes

A bounded raw-protocol probe passed these checks against the two flashed
boards:

- Repeating the same completed `RADIO_SEND` request returned the identical
  cached `RADIO_TX_RESULT` and caused no second RF delivery.
- Reusing that sequence with different bytes returned `SEQUENCE_REUSED`.
- An older sequence returned `STALE_SEQUENCE`.
- A CRC-corrupted USB frame incremented `usb_bad` once and caused no RF TX.
- A 201-byte radio payload returned `BAD_PAYLOAD` and caused no RF TX.
- A fresh host session replaced the old session. A command from the old
  session returned `WRONG_SESSION`, while the current session remained usable.
- Resetting each ESP produced a new boot ID and clean counters, making restart
  visible to the host.

## Final two-way mock result

Command:

```bash
python3 -m tools.lora_usb.bench \
  --base-port <BASE_PORT> \
  --robot-port <ROBOT_PORT> \
  --count 10 --interval 1.0
```

Every iteration completed both exchanges:

```text
mock BASE -> modem -> LoRa -> modem -> mock ROBOT -> reply -> mock BASE
mock ROBOT -> modem -> LoRa -> modem -> mock BASE -> reply -> mock ROBOT
```

Final counters from a clean ESP restart:

| Modem | TX OK | RX OK | RX bad | USB bad | Radio errors | USB event drops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | 20 | 20 | 0 | 0 | 0 | 0 |
| Robot | 20 | 20 | 0 | 0 | 0 | 0 |

Observed mock request/reply RTT was 209.7-219.6 ms for BASE-initiated
exchanges and 208.5-220.5 ms for ROBOT-initiated exchanges. Receive metrics
were -55 to -54 dBm with 8.50-9.25 dB SNR at the base-side receiver and -60
to -58 dBm with 8.25-8.75 dB SNR at the robot-side receiver.

## Remaining observation and boundary

One earlier 10-iteration host run reported a single final reverse-ACK timeout,
even though both modem counters showed all 20 TX and 20 RX packets and zero
bad frames, radio errors, or USB event drops. It did not reproduce in the next
5-iteration run, the next 10-iteration run, or the clean-restart final
10-iteration run. The final result proves the intended table-top path, but the
unexplained host-side observation is a reason to keep this explicitly at
prototype status and add longer soak/fault-injection coverage before using the
transport for real base-station or robot integration.

This result does not prove Raspberry Pi USB behavior, ROS2 integration, RTCM
throughput, range, interference tolerance, EU868 regulatory suitability,
authentication, replay protection, or mower safety behavior.
