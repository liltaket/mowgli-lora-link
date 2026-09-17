# MowgliNext integration contract

Status: code-level integration analysis against the current MowgliNext checkout
and its pinned Universal GNSS dependency. No physical mower or GNSS receiver
was connected for this analysis.

The LoRa sidecar remains outside Mowgli's motor, blade, STM32, and local safety
paths. Receiver-specific behavior stays in Universal GNSS.

## RTCM ingress

The robot-side service must publish validated, complete RTCM3 frames to:

| Field | Value |
| --- | --- |
| Topic | `/_gps_internal/universal/rtcm` |
| Type | `universal_gnss_ros2/msg/RtcmFrame` |
| QoS | reliable, keep-last 50 |
| Message fields | `stamp`, `message_type`, `data` |

Mowgli's GPS launcher remaps Universal GNSS `receiver_node` topic `rtcm` to
this internal name. The receiver node writes `message.data` byte-for-byte to
the configured GNSS transport.

Do not inject corrections through public `/rtcm`. That topic uses
`rtcm_msgs/msg/Message` and is an observer/UI projection published by
`mowgli_gnss_bridge` from the internal Universal GNSS stream.

A generic TCP output remains the default integration point so the radio link
can be verified without ROS 2. The optional `mowgli-lora-mowgli-rtcm` adapter
then reads that validated localhost stream and publishes `RtcmFrame` with
reliable keep-last-50 QoS. It imports ROS dependencies lazily, so the base and
non-ROS verification tools do not require a ROS installation.

## Typed telemetry sources

The eventual robot telemetry adapter should subscribe to typed state rather
than parse logs:

| Data | Topic and type | Source field |
| --- | --- | --- |
| Battery voltage | `/hardware_bridge/power`, `mowgli_interfaces/Power` | `v_battery` |
| Battery percent | `/battery_state`, `sensor_msgs/BatteryState` | `percentage` |
| Latitude/longitude | `/gps/fix`, `sensor_msgs/NavSatFix` | `latitude`, `longitude` |
| Speed | `/wheel_odom`, `nav_msgs/Odometry` | `twist.twist.linear.x` |
| Fix, satellites, correction age | `/gps/status`, `mowgli_interfaces/GnssStatus` | typed fields |
| Emergency state | `/hardware_bridge/emergency`, `mowgli_interfaces/Emergency` | typed fields |
| Blade state | `/hardware_bridge/status`, `mowgli_interfaces/Status` | typed fields |
| Localization mode | `/mowgli/localization/mode_id`, `std_msgs/Int32` | `data` |

`GnssStatus` capability/value flags are authoritative. Satellite count and
correction age are unknown unless their respective value bits are present.
The current compact LoRa telemetry frame has no defined fields for emergency,
blade, or localization state. They remain local metrics until a versioned
protocol extension is reviewed; undefined bits must not be repurposed.

## Control boundary

Current high-level behavior requests use
`/behavior_tree_node/high_level_control` with
`mowgli_interfaces/srv/HighLevelControl`:

- `COMMAND_STOP=8` stops motion and blade and remains in place. Current PAUSE
  has the same behavior-level semantics.
- `COMMAND_HOME=2` is a distinct request that sends the mower to dock.

The firmware emergency latch is a separate local safety interface and is not
a radio-control target. Never map LoRa directly to `/cmd_vel`, PWM, wheel
motors, the blade motor, or `/hardware_bridge/emergency_stop`.

The current radio application protocol is unauthenticated and lacks replay
protection. Consequently all real control integration remains disabled by
default. RTCM-only lab operation may proceed with an explicit warning, but a
mock `STOP_REQUEST` must not be represented as a safety-rated emergency stop.

## Current source evidence

The integration contract was checked against these MowgliNext paths:

- `sensors/gps/start_gps.sh`
- `sensors/gps/mowgli_gnss_bridge/src/universal_gnss_topic_bridge.cpp`
- `ros2/src/mowgli_interfaces/msg/GnssStatus.msg`
- `ros2/src/mowgli_interfaces/srv/HighLevelControl.srv`
- `ros2/src/mowgli_hardware/src/hardware_bridge_node.cpp`
- `ros2/src/mowgli_behavior/trees/main_tree.xml`
- `ros2/src/mowgli_bringup/launch/mowgli.launch.py`

The pinned Universal GNSS revision observed during the review was
`3495ffb9...`. Re-check these interfaces whenever MowgliNext updates that
dependency.

## Hardware proof still required

Publishing a valid `RtcmFrame` proves only the software ingress. Final HIL
must also show Universal GNSS forwarding diagnostics, bytes reaching the
configured receiver transport, and the receiver actually using corrections.
