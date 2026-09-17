# Mixed-traffic test results

Date: 2026-09-17

Status: prototype evidence, not production, safety, GNSS, range, or regulatory
approval.

## What the test data means

Unless a section explicitly says live RTCM, the RTCM3 frames below are
synthetic transport fixtures. They use valid RTCM3 framing, message-type
fields, representative lengths, and CRC24Q, but their payloads are not real
observations or corrections from a triple-band GNSS receiver. Those tests
therefore exercise scheduling, freshness, fragmentation, reassembly, and
error handling only.

`STOP_REQUEST` changes an in-memory mock robot state and returns a typed
acknowledgement. It is not connected to a mower, motor, blade, ROS 2 service,
or safety controller.

## Automated protocol checks

- 103 Python unit and integration tests passed, covering the USB/application
  protocols, modem reconnect/session/late-result handling, TCP and serial RTCM
  source recovery, health state, randomized RTCM3 parsing, the two Pi services,
  bounded TCP output, systemd units, and the optional Mowgli ROS 2 adapter.
- The real localhost TCP integration starts with no server, reconnects after it
  appears, discards a partial frame across EOF, reconnects after restart, and
  recovers the next complete frame byte-for-byte.
- The simulated end-to-end service test covers latency, packet loss,
  duplication, reordering, disconnect/reconnect, sender-session restart, and a
  60-epoch nominal stream without unbounded queues.
- The deterministic fault test passed reorder, duplicate, missing-fragment
  timeout, conflicting-fragment, CRC corruption, and next-frame recovery.
- The ESP32-S3 modem firmware built successfully with 22,520 bytes RAM (6.9%)
  and 295,445 bytes flash (8.8%).
- The same built image was flashed successfully to both boards. Its firmware
  binary SHA-256 is
  `7236f033d66a3e7eb534ddcac649ed112814843ca427f455ef89943e8f5e540b`.

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

## In-house live-RTCM range and capacity check

A later bounded test put the two endpoints on separate hosts across a house and
used the RTK base station's live RTCM3 TCP output rather than generated test
fixtures. Both modems ran 868.3 MHz, 500 kHz, SF5, CR4/5, a 12-symbol preamble,
and +10 dBm configured conducted output power.
The firmware binary used for the final profile has SHA-256
`d4cdda188efbd898e5257c23d65c8f0d37a8fe0a839f082e375f89634464ea3d`.

The 20-second receive capture contained 269 complete RTCM3 frames, 40,790
bytes, and all 14 message types offered by the receiver: 1005, 1019, 1020,
1042, 1044, 1045, 1046, 1077, 1087, 1097, 1107, 1117, 1127, and 1230. The
timed part of the capture lasted 19.069 seconds and averaged 2,139 bytes/s,
with a peak one-second bucket of 2,219 bytes/s. Every delivered frame passed
CRC24Q validation.

Across this run the robot host accepted 421/421 radio fragments reported by
the robot modem, with zero modem event drops, reassembly conflicts, or
reassembly timeouts. At the 18-second base snapshot, 379 fragments had
completed transmission and the live source was producing 14 frames/s; only
five complete frames, one fragment, and one in-flight fragment remained in
the bounded scheduler. The capture contained 19 consecutive instances of
every RTCM type, plus one additional instance of types 1005, 1019, and 1020 at
the capture boundary.

This proves that this particular indoor link and asynchronous USB host path
kept pace with the base station's live 1 Hz multi-constellation RTCM stream
during the bounded run. It does not validate correction contents, rover fix
quality, outdoor range, interference tolerance, continuous operation, antenna
ERP, or regulatory compliance.

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

## Instrumented nominal-rate diagnosis

A later 15-second nominal run used additive modem and host stage counters. It
remained an expected failed capacity test and was not repeated after the trace
was conclusive:

- 173 radio transmissions completed.
- 173 valid radio packets were received.
- 173 `RADIO_RX` events were queued and written to USB.
- 173 radio events were decoded by the host.
- 172 were matched by the synchronous per-send bench correlation.
- The scheduler offered 182 RTCM fragments, but only 169 were transmitted
  before freshness deadlines; transaction p95 was 72.7 ms.
- Estimated RF airtime was 9.32 seconds in 15 seconds, roughly 62% occupancy.
- STOP still applied and acknowledged 2/2 unique requests.

The original symptom therefore combines two effects. One decoded event became
stale or out-of-order relative to the synchronous bench wait, but the larger
logical RTCM loss is offered load above the effective single-packet transaction
capacity. There is no evidence of RF loss or ESP USB-queue loss in this run.
The current 1 Hz synthetic profile is not a stable or regulatory-approved
physical operating point. The reusable Pi service uses asynchronous receive
processing; a compliant lower-airtime PHY or channel-access design must be
selected before continuous deployment.

## Reduced-rate synchronous and Pi-service checks

A 20-second synchronous mixed-bench run at 0.75 observation epochs per second
still failed its strict acceptance threshold. The radios and modem queues did
not lose packets: the base completed 178 transmissions, the robot received all
178 valid packets, all 178 `RADIO_RX` events were written to USB and decoded by
the host, but only 177 were matched by the synchronous per-send wait. The
one-second wait then delayed the scheduler enough to expire later work. The
result was 58/62 logical RTCM3 messages, 14/15 complete epochs, 38/40 telemetry
messages, and 3/3 acknowledged mock STOP requests. Estimated airtime was 9.74
seconds. This is a host-test correlation/backpressure failure, not measured RF
loss.

The production-shaped asynchronous base and robot services were then exercised
once for 20 seconds at the same 0.75 Hz synthetic RTCM profile. That bounded
physical run passed:

- 62/62 complete RTCM3 frames arrived byte-for-byte in source order.
- 182/182 RTCM fragments were transmitted, received, decoded, accepted, and
  reassembled.
- Delivery ratio was 1.0 with zero missing or unexpected frames.
- There were no CRC failures, stale drops, reassembly conflicts/timeouts,
  rejected frames, or modem errors.

This result proves the asynchronous Pi-service transport on the two table-top
USB modems at that bounded synthetic load. It does not prove 1 Hz capacity,
real GNSS correction quality, Raspberry Pi deployment, RF range, or continuous
EU868 compliance.

The later host-hardening regression retained two failed runs rather than
masking them. The first delivered 61/62 RTCM3 frames: the base completed all
182 transmissions, while the robot modem received and wrote 181 radio events
to USB with no USB queue drop. The second delivered 43/62 frames after two
host response deadlines expired; the modem later reported all 126 accepted
transmissions complete and both delayed `RADIO_TX_RESULT` frames arrived. That
second run exposed a real accounting bug: an expired host deadline was being
reported as a proven transmission failure. The hardened transport now records
that state as uncertain, never retransmits the possibly-sent payload, abandons
the rest of that RTCM frame, and observes any late terminal response through a
bounded expiring correlation set without disturbing a newer send.

After that fix, one final bounded 20-second regression at 0.75 synthetic
epochs per second passed the strict hardened acceptance gate:

- 62/62 complete RTCM3 frames arrived byte-for-byte in source order.
- 182/182 RTCM fragments were transmitted, received, decoded, accepted, and
  reassembled.
- There were zero host response timeouts or uncertain outcomes, stale drops,
  CRC failures, reassembly conflicts/timeouts, rejected fragments, and modem
  errors.

No further RF run was made. The failed runs remain part of the evidence, and
the final pass remains only a short table-top transport regression under the
same limitations stated above.
