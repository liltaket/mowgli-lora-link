# Roadmap and integration boundary

## Current boundary

The ESP32 firmware is a symmetric, opaque radio modem. It owns SX1262 state,
packet framing, local TX completion, receive metrics, and bounded USB queues.
It does not understand ROS, GNSS models, mower commands, motors, or blades.

The host layer owns message meaning:

```text
application policy
  -> versioned application packet
  -> USB modem command
  -> ESP32/SX1262 radio link
```

## Implemented software boundary

The repository now includes:

1. A common base/robot Linux service using a stable
   `/dev/serial/by-id/...` modem path, reconnect/session recovery, bounded
   queues, stage counters, Prometheus metrics, and JSON health.
2. Generic base ingress from stdin, TCP, or serial with incremental RTCM3
   framing, CRC24Q validation, freshness, and strict single-outstanding USB
   scheduling.
3. Robot-side RTCM3 reassembly and validation with a bounded localhost TCP
   output, plus an optional ROS 2 bridge to the verified Universal GNSS
   ingress.
4. RTCM recording, machine-readable inspection, timed/accelerated replay, and
   deterministic-loss replay without fabricating a real capture.
5. Example configs and systemd units for base, robot, and the optional Mowgli
   RTCM adapter.

These paths are software-tested. They have not yet completed Raspberry Pi HIL
or a real GNSS correction capture.

## Remaining gated work

1. Verify both Pi services against the real USB devices: disconnect/reconnect,
   ESP restart without USB removal, idle-source recovery, and byte-exact real
   RTCM comparison.
2. Measure the maximum stable, legally usable physical rate. The current
   synthetic 1 Hz nominal profile is above effective transaction capacity and
   is not an approved continuous RF operating point.
3. Feed a real base capture through record/replay and prove Universal GNSS
   forwarding plus receiver correction use on the robot.
4. Add real telemetry from the verified typed Mowgli topics. Emergency, blade,
   and localization state require a versioned telemetry extension; undefined
   status bits must not be reused.
5. Implement the reviewed OSCORE authentication/replay design before mapping
   any radio request to Mowgli behavior. Then allowlist high-level semantics
   only; never emit motor PWM, blade PWM, `/cmd_vel`, or bypass local safety.
6. Complete occupied-bandwidth/ERP/emission measurements and the selected
   EU868/RED path before continuous outdoor operation.

Local robot safety remains authoritative if LoRa, either ESP32, USB, either Pi,
RTK corrections, or the base station disappears.
