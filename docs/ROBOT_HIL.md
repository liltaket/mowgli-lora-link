# Robot LoRa transport HIL procedure

Status: planned, transport-only robot installation. This procedure is for an
ESP32-S3 LoRa modem that is already running the reviewed modem firmware. It
does not authorize flashing, radio retuning, GNSS configuration, correction
source changes, mower movement, blade operation, or changes to the local
safety system.

The initial goal is narrow: prove that the robot host can keep the USB modem
connected and expose the LoRa transport's loopback endpoints without changing
the existing Wi-Fi/NTRIP correction path.

## Preconditions and non-negotiable rules

- Coordinate with anyone working on the robot before modifying files or
  enabling units. Preserve unrelated dirty files and inspect `git status`
  before each local checkout operation.
- Identify the board with `ls -l /dev/serial/by-id/` and copy the exact,
  stable by-id path into `/etc/mowgli-lora/robot.yaml`. Do not use
  `/dev/ttyACM*`, `/dev/ttyUSB*`, or a guessed serial path.
- Confirm the intended ESP32-S3 USB device is the only device whose path is
  changed. Do not flash it, erase it, open a programming tool, or change its
  firmware/radio settings in either phase.
- Inspect the physical radio installation before enabling transport: the
  antenna must be fitted to the intended radio connector and its cable/strain
  relief must be intact. Do not transmit with a disconnected or uncertain
  antenna, and do not substitute antenna hardware during this procedure.
- This sidecar never controls mower actuation. Do not use `/cmd_vel`, motor or
  blade interfaces, emergency interfaces, or any radio control message. Keep
  the mower stationary and blades disabled for all checks.

## Phase 1: transport-only installation

Install the package and the `mowgli-lora-robot` unit/configuration using the
normal project user for package installation, as described in
[`host/README.md`](../host/README.md). Set the exact by-id device path in
`/etc/mowgli-lora/robot.yaml`; retain loopback defaults:

```yaml
rtcm:
  output:
    type: tcp
    bind: 127.0.0.1
    port: 2233
metrics:
  bind: 127.0.0.1
  port: 9609
```

Do **not** install or enable `mowgli-lora-mowgli-rtcm.service`, do not create
`mowgli-ros.env`, and do not source or alter a ROS environment. Enable only:

```bash
sudo systemctl enable --now mowgli-lora-robot
```

The existing Wi-Fi/NTRIP path remains the sole correction publisher in this
phase. LoRa transport may receive/reassemble validated RTCM frames for local
observation, but nothing forwards those frames to the receiver or ROS.

Required evidence before proceeding:

```bash
systemctl is-active mowgli-lora-robot
curl --fail http://127.0.0.1:9609/healthz
ss -ltn 'sport = :2233'
```

Record the exact by-id path, unit status, health response, listener evidence,
and relevant journal timestamps. If actual RTCM arrives, capture only bounded
transport evidence (validated frame/CRC metrics); do not call it receiver or
fix-quality proof.

## Phase 1 rollback

If the modem is not the intended device, health is degraded, antenna status is
uncertain, or coexistence is not understood, disable the transport unit and
leave the established Wi-Fi/NTRIP correction path untouched:

```bash
sudo systemctl disable --now mowgli-lora-robot
```

Do not delete configuration or alter the Wi-Fi/NTRIP service as part of this
rollback. Preserve logs and configuration for diagnosis. A stopped LoRa
transport service is the safe default until the discrepancy is understood.

## Phase 2: separately reviewed receiver integration

Phase 2 is not an extension of a successful TCP/health check. It needs a
reviewed decision identifying the actual ROS/GNSS deployment boundary and the
one permitted correction source.

The supplied systemd ROS adapter is usable only when ROS, the Mowgli overlay,
and `universal_gnss_ros2` exist on the host running the service. For a
containerized ROS/GNSS stack, the GPS container boundary must explicitly select
one source: `ntrip`, `tcp`, or `none`. It must not fail over automatically or
accept simultaneous publishers. That selector is owned by the GPS container
integration, not by `mowgli-lora-robot`.

Before a Phase 2 change, take and review evidence that the old correction
publisher has stopped, the chosen selector is explicit, and only the selected
source can reach the receiver. Afterward, prove separately: validated LoRa
transport, container/ROS ingress, receiver transport writes, and receiver
correction usage. See [`MOWGLI_INTEGRATION.md`](MOWGLI_INTEGRATION.md) for the
topic contract and proof boundaries.

## Proof boundaries

| Evidence | What it proves | What it does not prove |
| --- | --- | --- |
| USB by-id path and healthy service | Correct host-side device selection and modem readiness | Firmware identity, RF reachability, or receiver use |
| TCP 2233 listener and health 9609 | Local robot transport endpoint is available | RTCM delivery to ROS/GNSS or corrections at the receiver |
| Validated transport frame metrics | Host observed byte-valid RTCM transport frames | GNSS forwarding, fix quality, navigation, or RF compliance |
| A selected container source with receiver diagnostics | The selected integration path reaches the configured receiver transport | Safe automatic failover, field reliability, or mower safety approval |
