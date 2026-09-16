# Mowgli Application Protocol v1

Status: prototype host-layer contract. It is carried as the opaque payload of
the [USB/radio modem protocol](USB_MODEM_PROTOCOL.md). The ESP32 firmware does
not interpret these messages.

This protocol currently has no authentication, encryption, or replay
protection. `STOP_REQUEST` is a mock high-priority software request used to
test ordering and idempotency. It is not a safety-rated emergency stop and is
not connected to a physical robot.

## Frame

All multibyte values are big-endian. Signed values use two's complement.

| Offset | Field | Size |
| ---: | --- | ---: |
| 0 | magic, ASCII `MA` | 2 |
| 2 | version, `1` | 1 |
| 3 | message type | 1 |
| 4 | flags, zero | 1 |
| 5 | reserved, zero | 1 |
| 6 | sender session, nonzero | 4 |
| 10 | message ID, nonzero and increasing | 4 |
| 14 | source age in milliseconds | 2 |
| 16 | body length | 2 |
| 18 | body, 0-180 bytes | variable |
| 18 + length | CRC16-CCITT-FALSE | 2 |

Maximum encoded application frame size is 200 bytes. CRC16 uses polynomial
`0x1021`, initial value `0xffff`, no reflection, and XOR-out zero. The CRC
covers every preceding application-frame byte.

The sender chooses a random nonzero session when its host process starts and
changes session before message-ID wrap. Sessions are correlation/restart
markers, not identities or authentication.

## Message types

| ID | Name | Body |
| ---: | --- | --- |
| `0x01` | `HELLO` | `role:u8, capabilities:u16` |
| `0x10` | `STOP_REQUEST` | `target_session:u32, command:u8, reason:u8` |
| `0x11` | `COMMAND_ACK` | `request_session:u32, request_id:u32, result:u8, state:u8` |
| `0x20` | `TELEMETRY` | fixed 24-byte schema below |
| `0x30` | `RTCM_FRAGMENT` | `rtcm_type:u16, total_length:u16, index:u8, count:u8, data[]` |

HELLO roles are `BASE=1` and `ROBOT=2`. The only command currently defined is
`STOP_REQUEST=1`.

ACK result values are:

| Value | Meaning |
| ---: | --- |
| 0 | applied |
| 1 | already stopped |
| 2 | expired |
| 3 | wrong target session |
| 4 | unsupported command |
| 5 | ID conflict |
| 6 | stale ID |

Mock state values are `UNKNOWN=0`, `RUNNING=1`, and `STOPPED=2`.

## STOP idempotency

The receiver keys a request by `(sender_session, message_id)` and also retains
the exact command body:

- The first current, correctly targeted STOP changes mock state once.
- An identical retry returns the cached ACK result without applying twice.
- The same key with a different body returns `ID_CONFLICT`.
- IDs below the retained high-water mark return `STALE_ID`, including an old
  ID whose cache entry has been evicted.
- `target_session` must match the current mock robot host session, preventing
  an old command from applying after that process restarts.
- STOP expires after 2,000 ms of source age.
- The prototype retains 64 recent results per process.

A future real host may retry at 250 and 500 ms with the same application ID
but a new USB request sequence. A local modem TX result is never a peer ACK.
There is deliberately no radio command that re-arms or restarts a mower.

## Telemetry body

```text
uptime_ms:u32
latitude_e7:i32
longitude_e7:i32
speed_mm_s:u16
battery_mv:u16
last_rtcm_transport_age_ms:u16
status_flags:u16
fix_type:u8
satellites:u8
reserved:u16 = 0
```

Status bit 0 means mock stopped and bit 1 means fresh RTCM is available. Fix
values are `0 unknown`, `1 standalone`, `2 differential`, `3 RTK float`, and
`4 RTK fixed`. Unknown signed coordinates use `INT32_MIN`; unknown unsigned
measurements use `0xffff`.

The nominal test rate is 2 Hz with a latest-only queue. An unsent older sample
is replaced rather than accumulated. `source_age_ms` lets the eventual Pi
receiver apply an explicit staleness policy before publishing the sample.

## RTCM3 fragmentation and freshness

Each fragment contains 6 bytes of metadata and up to 174 bytes of the complete
RTCM3 frame, including its `D3` header and CRC24Q. Frames are limited to 1,029
bytes and therefore at most six fragments.

- The key is `(sender_session, message_id)`.
- Every non-final fragment is exactly 174 data bytes.
- All metadata must agree and the final assembled length must be exact.
- Identical duplicate fragments are ignored; a conflicting duplicate
  invalidates the assembly.
- At most eight assemblies are held.
- The hard deadline is 600 ms from the first fragment and duplicates never
  extend it.
- A fragment with source age of 1,000 ms or more is stale.
- Reassembled data must pass RTCM3 reserved-bit, length, CRC24Q, and message
  type validation before delivery.
- There is no RTCM fragment ACK or retry. Fresh correction data takes
  precedence over an old incomplete frame.

CRC24Q uses initial value zero, polynomial `0x1864CFB`, no reflection, and no
XOR-out. It covers the RTCM header and payload; its three bytes are big-endian.

## Synthetic 1 Hz transport profile

The test generator creates deterministic RTCM3 frames with correct transport
framing, message number, length, and CRC24Q. Their GNSS observation fields are
not semantically valid corrections and must never be sent to a receiver.

Every synthetic epoch contains:

| Type | Intended constellation/bands | Frame size |
| ---: | --- | ---: |
| 1077 | GPS MSM7, three-band load surrogate | 446 B |
| 1087 | GLONASS MSM7, two-band load surrogate | 226 B |
| 1097 | Galileo MSM7, three-band load surrogate | 446 B |
| 1127 | BeiDou MSM7, three-band load surrogate | 586 B |

Types 1005 (25 B) and 1230 (18 B) are added every tenth epoch. The nominal
load is 1,704 bytes per observation epoch and 1,708.3 bytes/s on average with
the static messages. This is a repeatable transport load, not proof that an
actual triple-band base emits the same distribution.

## Scheduling and RF boundary

The prototype scheduler chooses one packet at a time in this order:

1. command ACK;
2. STOP request/retry;
3. latest telemetry;
4. earliest-deadline RTCM fragment.

At SF5/BW250/CR4/5 with a 12-symbol preamble, the nominal RTCM plus 2 Hz
telemetry profile consumes roughly 66% of RF airtime before interference,
retries, or distributed-host contention. This may be technically measurable
in a short bounded test, but it is not approved for continuous EU868 use.
Long full-rate tests therefore run in the simulator; physical full-rate tests
are duration-limited, and longer physical tests use a reduced-duty profile.
