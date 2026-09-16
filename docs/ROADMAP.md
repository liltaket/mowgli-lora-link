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

## Next Raspberry Pi work

1. Add a Linux/Python service that owns one modem by a stable
   `/dev/serial/by-id/...` path and publishes machine-readable link health.
2. On the base Pi, accept RTCM3 from the correction source, validate CRC24Q,
   enforce freshness, fragment it, and schedule it with STOP/command priority.
3. On the robot Pi, reassemble and validate RTCM3 before handing bytes to the
   existing GNSS correction ingress. Keep receiver brands outside this repo.
4. Map authenticated, allowlisted high-level requests onto Mowgli's existing
   control interface. Never emit motor PWM, blade PWM, or direct velocity.
5. Build telemetry from existing typed state rather than scraping logs.
6. Add reviewed authentication, replay protection, key provisioning, and
   rotation before accepting real remote commands.
7. Complete EU868 channel access, airtime, interference, and range review
   before continuous outdoor operation.

Local robot safety remains authoritative if LoRa, either ESP32, USB, either Pi,
RTK corrections, or the base station disappears.
