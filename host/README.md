# Raspberry Pi host services

This package bridges validated RTCM3 bytes through the existing USB/LoRa
modem. It has two roles, selected only by configuration: `base` reads an RTCM3
byte stream and transmits application fragments; `robot` validates/reassembles
them and publishes complete frames to a TCP server. An optional ROS 2 adapter
publishes that TCP stream to MowgliNext's verified Universal GNSS ingress. It
does not implement remote mower control or a safety stop.

Install on each Pi into an isolated environment:

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin mowgli || true
sudo apt-get update
sudo apt-get install --yes python3-venv python3-dev
sudo install -d -o root -g mowgli -m 0750 /etc/mowgli-lora
sudo install -d -o "$USER" -g "$USER" -m 0755 /opt/mowgli-lora-link
python3 -m venv /opt/mowgli-lora-link/.venv
/opt/mowgli-lora-link/.venv/bin/pip install .
sudo chown -R root:root /opt/mowgli-lora-link
sudo install -D -m 0644 host/systemd/mowgli-lora-base.service /etc/systemd/system/mowgli-lora-base.service
sudo install -D -m 0644 host/config/base.example.yaml /etc/mowgli-lora/base.yaml
sudo systemctl daemon-reload
sudo systemctl enable --now mowgli-lora-base
```

Run the venv and `pip install .` commands as the normal project user from a
release checkout. The temporary user ownership makes that non-root build
possible; the final `chown` makes the installed runtime root-owned and
read-only to the service. The installed package does not depend on the checkout
being present on `PYTHONPATH`. Keep configuration files owned by root and
readable by the `mowgli` group. The units deliberately retain device access
through `dialout`, network access for the local TCP/ROS bridge, and read-only
access to the OS and application trees.

Use the robot unit/configuration analogously. Before enabling either service,
identify the correct board on that Pi:

```bash
ls -l /dev/serial/by-id/
```

Use the resulting `/dev/serial/by-id/...` path in the configuration. Do not
use unstable `/dev/ttyACM0` names. The service reconnects USB automatically;
an ESP boot-ID change clears pending host state and starts a new session.
The example units add the service user to the normal `dialout` device group.

For the first robot installation, follow the two-stage procedure in
[`docs/ROBOT_HIL.md`](../docs/ROBOT_HIL.md). Stage 1 enables only
`mowgli-lora-robot`, with its localhost TCP output (`127.0.0.1:2233`) and
health endpoint (`127.0.0.1:9609`). It deliberately does **not** install or
start the ROS adapter, so the existing Wi-Fi/NTRIP path remains the sole
correction publisher. Do not flash the ESP32-S3, alter radio configuration,
or perform any mower actuation as part of this installation.

The base ingress supports `stdin`, client `tcp`, and `serial`. The robot output
is a localhost TCP server, default port 2233. A basic non-ROS end-to-end check
looks like:

```bash
cat capture.rtcm3 | mowgli-lora-base --config /etc/mowgli-lora/base.yaml
nc 127.0.0.1 2233 > robot-output.rtcm3
mowgli-lora-rtcm-inspect robot-output.rtcm3
mowgli-lora-rtcm-compare capture.rtcm3 robot-output.rtcm3
```

TCP and serial inputs reconnect inside the daemon with bounded exponential
backoff. Each successful reopen starts a new input generation: parser state,
queued complete frames, and unsent fragments from the previous source session
are discarded. Source read timestamps travel with queued bytes so scheduler
freshness is measured from actual ingress rather than from when the main loop
eventually consumes them. A serial read timeout is treated as idle, not as a
disconnect.

For a deliberately bounded bench with two directly attached modems, use the
same asynchronous services with generated RTCM3 transport fixtures:

```bash
mowgli-lora-service-bench \
  --base-port <BASE_PORT> --robot-port <ROBOT_PORT> \
  --duration-s 20 --rtcm-hz 0.75 \
  --report reports/physical-pi-service-075hz-20s.json
```

The command refuses durations above 30 seconds and rates above 1 Hz. Its input
is synthetic and must never be described as real GNSS corrections.

`mowgli-lora-rtcm-record` reads raw RTCM3 from stdin and stores only validated
frames with their arrival timestamps in the `MLR1` format. `mowgli-lora-rtcm-replay`
preserves timings by default; use `--speed 10` for acceleration and
`--drop-every N` for deterministic loss. Never label a synthetic fixture as a
real correction capture. `mowgli-lora-rtcm-inspect` emits one JSON object with
frame/type/size/CRC statistics. MLR1 captures additionally report duration and
average/peak bytes per second; raw streams explicitly report timing as
unavailable.
`mowgli-lora-rtcm-compare` exits nonzero unless both validated streams contain
the same complete frames in byte-exact order, and reports missing/unexpected
frame counts plus the delivery ratio as JSON.

On the robot, first verify the TCP stream independently. The optional host ROS
adapter is for hosts which have a matching ROS installation **and** the
`universal_gnss_ros2` environment locally available, together with compatible
DDS discovery and network configuration. A containerized deployment does not
provide those host dependencies automatically; prefer an explicit
container-boundary integration and correction-source selection. When the
matching host environment is available, publish the same validated stream to
the receiver ingress with:

```bash
source /opt/ros/$ROS_DISTRO/setup.bash
source /path/to/mowgli/install/setup.bash
mowgli-lora-mowgli-rtcm \
  --host 127.0.0.1 --port 2233 \
  --topic /_gps_internal/universal/rtcm
```

The bridge uses reliable keep-last-50 QoS and preserves each complete RTCM3
frame byte-for-byte. Public `/rtcm` is an observation mirror, not the receiver
ingress. See [`docs/MOWGLI_INTEGRATION.md`](../docs/MOWGLI_INTEGRATION.md).

Only after a separately reviewed integration decision, install
`host/systemd/mowgli-lora-mowgli-rtcm.service` and copy
`host/config/mowgli-ros.example.env` to `/etc/mowgli-lora/mowgli-ros.env`,
adjusting the two setup paths. This is Stage 2, not part of the initial robot
transport check. Enable both units only on a host-based ROS/GNSS deployment:

```bash
sudo systemctl enable --now \
  mowgli-lora-robot mowgli-lora-mowgli-rtcm
```

For containerized ROS/GNSS, choose exactly one correction source at the GPS
container boundary: `ntrip`, `tcp`, or `none`. Do not add automatic failover
and do not run simultaneous correction publishers. The selector belongs to the
GPS container integration, not to this LoRa host service; see
[`docs/MOWGLI_INTEGRATION.md`](../docs/MOWGLI_INTEGRATION.md).

Metrics are local HTTP endpoints: `/metrics` is Prometheus text and `/healthz`
is JSON. The base default is `127.0.0.1:9608`, robot `127.0.0.1:9609`.
Examples include USB/air stages, RTCM parser errors, reassembly expiry, stale
queue drops, message types, bytes and output deliveries. Slow TCP readers have
frame-aware, byte-, count-, and freshness-bounded per-client queues and are
dropped with explicit metrics rather than receiving old corrections or
blocking other readers. The server also caps concurrent clients at 16 and
rejects excess connections. The client must reconnect after such a drop.

`/healthz` returns HTTP 200 with `status: ok` only when the modem is connected,
the USB handshake is complete, and the firmware reports its radio ready. The
base additionally requires its RTCM source to be connected. A temporary source
outage therefore returns HTTP 503 with `status: degraded` while the process
stays alive and reconnects. `/metrics` remains HTTP 200 independently.

Metrics such as `usb_reconnects`, `rtcm_frames_ingested`, parser failures, and
source connect/disconnect events are cumulative counters. Current-state values
such as `modem_connected`, `modem_handshake_complete`, `modem_radio_ready`,
`rtcm_source_connected`, queue depths, oldest queued age, and source last-data
age are gauges. All exported Prometheus names have the `mowgli_lora_` prefix.
Diagnostics polling is idle-only and is deferred during both pending RTCM work
and recent radio receive activity.

A missing `RADIO_TX_RESULT` after the bounded host deadline is an uncertain
outcome: the packet may already have gone over the air. The service never
retransmits it automatically. It abandons the remaining fragments of that RTCM
frame, continues with later fresh corrections, and keeps a small expiring
correlation set so a late terminal response is measured without completing or
disturbing a newer send.

The optional ROS bridge applies the same RTCM freshness deadline and discards
queued corrections across a robot-side TCP reconnect. Its systemd unit uses
`/run/mowgli-lora` for ROS logs so the service remains writable where required
while `ProtectSystem=strict` is active.

Set `logging.level` in the configuration to a standard Python level such as
`INFO` or `DEBUG`. View service logs via `journalctl -u mowgli-lora-base -f` or
`journalctl -u mowgli-lora-robot -f`.

## Safety and current limit

The modem protocol remains unauthenticated. This package only carries RTCM3;
it intentionally does not turn the prototype STOP message into mower control.
The nominal-rate failure is now traced to one stale synchronous correlation
plus offered load above the current packet transaction capacity. The new
service uses asynchronous RX and a strict one-command USB window. A bounded
20-second table-top run at 0.75 synthetic epochs per second delivered 62/62
RTCM3 frames byte-exactly, but it does not turn the failed physical 1 Hz result
into a pass. Use long physical tests only at the documented low-duty profile.
Continuous operation still requires the separate RF design and compliance work
in `docs/SECURITY_AND_EU868.md`.
