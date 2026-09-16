# Mixed-traffic test results

Date: 2026-09-17

Status: prototype evidence, not production, safety, GNSS, range, or regulatory
approval.

## What the test data means

The RTCM3 frames below are synthetic transport fixtures. They use valid RTCM3
framing, message-type fields, representative lengths, and CRC24Q, but their
payloads are not real observations or corrections from a triple-band GNSS
receiver. The tests therefore exercise scheduling, freshness, fragmentation,
reassembly, and error handling only.

`STOP_REQUEST` changes an in-memory mock robot state and returns a typed
acknowledgement. It is not connected to a mower, motor, blade, ROS 2 service,
or safety controller.

## Automated protocol checks

- 20 Python unit tests passed.
- The deterministic fault test passed reorder, duplicate, missing-fragment
  timeout, conflicting-fragment, CRC corruption, and next-frame recovery.
- The ESP32-S3 modem firmware built successfully with 22,496 bytes RAM (6.9%)
  and 295,125 bytes flash (8.8%).
- The same built image was flashed successfully to both boards. Its firmware
  binary SHA-256 is
  `beb679d1ece45e8482a997986de225ac2fb9a531cb108d9c06659ca97abe66e1`.

## Twelve-minute event-time simulation

The nominal scheduler profile completed 720 seconds of event time:

- 720/720 complete 1 Hz observation epochs.
- 3,024/3,024 logical RTCM3 messages reassembled.
- 1,440/1,440 telemetry messages delivered.
- 23/23 unique mock STOP requests applied and acknowledged; 11 duplicate
  attempts were idempotent.
- No errors, queue drops, stale fragments, reassembly timeouts, or conflicts.
- Estimated RF airtime was 478.52 seconds, about 66% of the interval. This is
  why a long physical nominal-rate run is intentionally prohibited.

## Physical tests

Both table-top endpoints ran the same current firmware with a 12-symbol
preamble. Device identifiers and host port names are intentionally omitted.

The first two 15-second nominal-rate trials did **not** pass the full acceptance
threshold. In both runs the modem counters showed every transmitted RF packet
received and zero RF, PHY, USB-frame, or modem-queue errors, while the host did
not correlate one event in the first trial and two in the retry. STOP remained
prioritized and passed in both trials: 2/2 unique requests were applied and
acknowledged, including the duplicate attempt. The second trial delivered
53/62 logical RTCM3 messages and 28/30 telemetry messages. This is retained as
a real capacity/host-event-handling limitation, not rewritten as a success.

The 600-second low-duty physical run passed:

- 60/60 logical RTCM3 messages completed: 15 each of types 1077, 1087, 1097,
  and 1127.
- 300/300 telemetry messages delivered.
- 9/9 unique mock STOP requests applied and acknowledged; 4 duplicate
  attempts were idempotent.
- STOP acknowledgement latency was at most 57.51 ms.
- No errors, queue drops, stale fragments, reassembly timeouts, reassembly
  conflicts, bad radio packets, bad USB frames, radio errors, or USB event
  drops.
- RSSI was -63 to -50 dBm and SNR was 8.0 to 10.0 dB on the table-top link.
- Estimated RF airtime was 15.72 seconds over the ten-minute interval.

This longer profile used 0.5 Hz telemetry and one rotating RTCM observation
message every 10 seconds. It proves the bounded mixed-message path at that
load; it does not turn the failed physical nominal-rate trials into a pass.
