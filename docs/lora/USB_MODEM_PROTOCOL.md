# LoRa USB modem protocol v1

Status: Phase 2 prototype contract. The framing, strict validation, session
boundary, correlated responses, and local-TX-versus-peer-receipt distinction
are intended to survive. Message IDs, the 200-byte radio payload ceiling, the
one-command window, and the unauthenticated radio envelope remain prototype
choices until later protocol, scheduler, regulatory, and security review.

## Question under test

Can the two identical ESP32-S3/SX1262 boards act as symmetric binary modems so
that mock host roles can exchange opaque data through this complete path?

```text
mock base -> USB -> ESP -> LoRa -> ESP -> USB -> mock robot
```

The firmware has no BASE/ROBOT role and no knowledge of Mowgli commands or
RTCM. Host software owns all application semantics.

## Common integer and checksum rules

- All multibyte integers are big-endian.
- Signed metric fields use two's-complement representation.
- CRC is CRC16-CCITT-FALSE: polynomial `0x1021`, initial value `0xffff`, no
  reflection, XOR-out zero.
- CRC covers every decoded byte before the CRC field. The CRC itself is stored
  as a big-endian `u16`.
- Check vector: ASCII `123456789` has CRC `0x29b1`.

## USB framing

One USB frame is `COBS(decoded frame) || 0x00`. Empty delimiters are ignored.
Receivers discard an oversized or malformed frame through the next delimiter
and must recover on the next valid frame.

Decoded frame:

| Offset | Field | Size |
| ---: | --- | ---: |
| 0 | magic, ASCII `MU` | 2 |
| 2 | version, `1` | 1 |
| 3 | message type | 1 |
| 4 | flags, must be zero | 1 |
| 5 | reserved, must be zero | 1 |
| 6 | host session | 4 |
| 10 | request sequence | 4 |
| 14 | payload length | 2 |
| 16 | payload | 0-256 |
| 16 + length | CRC16 | 2 |

Maximum decoded size is 274 bytes. Length must match exactly. Bad COBS, CRC,
magic, version, flags, reserved, or length is counted and silently discarded;
untrusted noise must not trigger response floods.

### USB message types

Responses echo the request's session and sequence. Unsolicited events use the
active session and sequence zero.

| ID | Name | Direction | Payload |
| ---: | --- | --- | --- |
| `0x01` | `HELLO` | host -> modem | empty |
| `0x81` | `INFO` | modem -> host | `device_id[6], boot_id:u32, capabilities:u32, max_air_payload:u16, radio_ready:u8, profile_id:u8, uptime_ms:u32` |
| `0x02` | `RADIO_SEND` | host -> modem | 1-200 opaque bytes |
| `0x82` | `RADIO_TX_RESULT` | modem -> host | `boot_id:u32, air_sequence:u32, tx_duration_us:u32` |
| `0x90` | `RADIO_RX` | modem -> host | `sender_boot_id:u32, air_sequence:u32, rssi_centi_dbm:i16, snr_centi_db:i16, receive_uptime_ms:u32, opaque_payload[]` |
| `0x03` | `GET_LINK_STATUS` | host -> modem | empty |
| `0x83` | `LINK_STATUS` | modem -> host | `boot_id:u32, uptime_ms:u32, radio_state:u8, reserved[3], tx_ok:u32, rx_ok:u32, rx_bad:u32, usb_bad:u32, radio_errors:u32, usb_event_drops:u32` |
| `0xff` | `ERROR` | modem -> host | `code:u16, driver_detail:i16, rejected_type:u8` |

Capability bits are `OPAQUE_RADIO=1<<0`, `RX_METRICS=1<<1`,
`LOCAL_TX_RESULT=1<<2`, and `BENCH_PROFILE=1<<3`.

Error codes are `UNSUPPORTED_TYPE=1`, `BAD_PAYLOAD=2`, `NOT_READY=3`,
`WRONG_SESSION=4`, `BUSY=5`, `STALE_SEQUENCE=6`, `SEQUENCE_REUSED=7`,
and `RADIO_FAILURE=8`. Only an integrity-validated request may receive a
correlated error.

`RADIO_TX_RESULT` proves only that the local radio finished transmitting. It
is not a peer delivery acknowledgement. A host timeout leaves the transmission
outcome unknown and must not cause an automatic retransmission.

## Radio envelope

LoRa already preserves packet boundaries, so no COBS is used over the air.
The PHY CRC remains enabled in addition to the application CRC.

| Offset | Field | Size |
| ---: | --- | ---: |
| 0 | magic, ASCII `ML` | 2 |
| 2 | version, `1` | 1 |
| 3 | type, `OPAQUE=1` | 1 |
| 4 | flags, must be zero | 1 |
| 5 | reserved, must be zero | 1 |
| 6 | sender boot ID | 4 |
| 10 | air sequence | 4 |
| 14 | payload length | 2 |
| 16 | opaque payload | 1-200 |
| 16 + length | CRC16 | 2 |

Maximum radio packet size is 218 bytes. A new air sequence is allocated for
each new transmission attempt and is independent of the host request sequence.
Boot IDs distinguish restarts probabilistically; they are not device identity,
authentication, or replay protection.

## Session, retry, and restart contract

1. At boot the modem generates a nonzero boot ID, initializes the radio, and
   listens. It performs no autonomous transmission.
2. A host chooses a random nonzero session and sends `HELLO` with a nonzero
   sequence. `INFO` establishes the session even when `radio_ready` is false.
3. A new session clears request history and pending host work. The host may
   have only one command outstanding.
4. Request sequences strictly increase. An identical retransmission of the
   most recently completed request returns its cached response without a new
   radio transmission. Reusing that sequence with different bytes yields
   `SEQUENCE_REUSED`; an older sequence yields `STALE_SEQUENCE`.
5. `RADIO_SEND` is not queued in Phase 2. A request received while the radio or
   another command is busy yields `BUSY`.
6. After TX completion or failure the modem returns to continuous RX.
7. Serial reopen starts with a delimiter and a fresh `HELLO` session. Delayed
   messages from older sessions cannot satisfy current requests.
8. ESP or host restart makes any outstanding send outcome unknown. Neither
   side automatically resends an opaque payload.
9. Radio events are held only in a bounded USB output queue. Events are dropped
   rather than blocking radio work when USB is unavailable or backpressured;
   drops are observable in `LINK_STATUS`.

## Phase 2 acceptance boundary

- Shared golden frames and corruption/recovery tests agree across C++ and
  Python implementations.
- Wrong length, CRC, version, flags, session, stale sequence, and oversized
  send cannot invoke radio transmission.
- Duplicate completed `RADIO_SEND` causes one physical transmission and the
  cached result is returned.
- Mock BASE and ROBOT exchange tagged opaque request/reply payloads in both
  initiator directions through the two physical modems.
- USB reconnect and device restart are visible and cannot turn a stale result
  into success.
- Hardware tests remain bounded and report request RTT, both RX metrics, modem
  counters, and all errors without claiming Raspberry Pi, ROS, RTCM, range, or
  production EU868 proof.
