# LoRa USB modem host tools (prototype)

These tools exercise two identical USB LoRa modems and the versioned
application protocol. They are not base-station, robot, ROS, safety, or
production-control software. The firmware remains symmetric; BASE and ROBOT
are host-side roles.

Install the only external dependency needed for physical serial use:

```sh
python3 -m pip install pyserial
```

Run the bounded bench with the two modem ports:

```sh
python3 -m tools.lora_usb.bench \
  --base-port <BASE_PORT> \
  --robot-port <ROBOT_PORT> \
  --count 10 --interval 2.0
```

Omit `--count` for interactive mode; press Enter to start a two-way exchange
and `q` or `quit` to stop. Each iteration exercises both BASE -> ROBOT -> BASE
and ROBOT -> BASE -> ROBOT with tagged acknowledgements. Output includes
application sequence, RTT, RSSI/SNR, modem counters, and failures. A serial
timeout means the transmission outcome is unknown; the tool does not
automatically resend it.

Run pure protocol tests without hardware or pyserial:

```sh
python3 -m unittest tools.lora_usb.test_protocol -v
```

The recorded physical result and its remaining prototype caveat are in
[`docs/lora/PHASE2_BENCH.md`](../../docs/lora/PHASE2_BENCH.md).

## Mixed application bench

The application-level runner exercises prioritized mock STOP requests,
robot-to-base telemetry, and base-to-robot RTCM3 fragmentation. STOP only
changes the in-memory `StopCache` state. The RTCM generator creates
transport-valid frames with representative message types and sizes; it does
not generate real satellite corrections.

Run a 12-minute event-time capacity simulation of the nominal profile (RTCM
1077, 1087, 1097, and 1127 at 1 Hz; 1005 and 1230 every 10 seconds; telemetry
at 2 Hz):

```sh
python3 -m tools.lora_usb.mixed_bench \
  --mode simulate --profile nominal --duration-s 720 \
  --status-interval 60 --stop-interval 30 \
  --report reports/sim-nominal-720s.json
```

Run deterministic duplicate, reorder, missing-fragment, conflict, CRC, and
recovery checks with:

```sh
python3 -m tools.lora_usb.mixed_bench \
  --fault-test --report reports/fault.json
```

Physical nominal runs are hard-limited to 20 seconds because the current RF
profile has not completed its EU868 channel-access review:

```sh
python3 -m tools.lora_usb.mixed_bench \
  --mode physical --profile nominal --duration-s 15 \
  --base-port <BASE_PORT> --robot-port <ROBOT_PORT>
```

The longer physical profile is hard-limited to 900 seconds. It reduces
telemetry to 0.5 Hz and sends one rotating synthetic observation message every
10 seconds:

```sh
python3 -m tools.lora_usb.mixed_bench \
  --mode physical --profile low-duty --duration-s 600 \
  --base-port <BASE_PORT> --robot-port <ROBOT_PORT>
```

Neither physical mode connects to mower control. JSON reports are ignored by
Git because they include local device identifiers; publish only reviewed,
anonymized summaries.
