"""Mowgli Application Protocol v1 prototype.

The STOP message implemented here changes only an in-memory mock state. It is
not a physical mower action or a safety-rated emergency stop.
"""

from __future__ import annotations

import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAGIC = b"MA"
VERSION = 1
HEADER_SIZE = 18
CRC_SIZE = 2
MAX_BODY_SIZE = 180
MAX_FRAME_SIZE = 200

HELLO = 0x01
STOP_REQUEST = 0x10
COMMAND_ACK = 0x11
TELEMETRY = 0x20
RTCM_FRAGMENT = 0x30

ROLE_BASE = 1
ROLE_ROBOT = 2
STOP_COMMAND = 1

ACK_APPLIED = 0
ACK_ALREADY_STOPPED = 1
ACK_EXPIRED = 2
ACK_WRONG_TARGET_SESSION = 3
ACK_UNSUPPORTED_COMMAND = 4
ACK_ID_CONFLICT = 5
ACK_STALE_ID = 6

STATE_UNKNOWN = 0
STATE_RUNNING = 1
STATE_STOPPED = 2

RTCM_FRAGMENT_DATA_SIZE = 174
MAX_RTCM_FRAME_SIZE = 1029
MAX_RTCM_FRAGMENTS = 6
MAX_REASSEMBLIES = 8
REASSEMBLY_TIMEOUT_S = 0.600
RTCM_TRANSPORT_TTL_MS = 1000

UNKNOWN_I32 = -(2**31)
UNKNOWN_U16 = 0xFFFF

RTCM_OBSERVATION_SIZES = {1077: 446, 1087: 226, 1097: 446, 1127: 586}
RTCM_STATIC_SIZES = {1005: 25, 1230: 18}
SYNTHETIC_RTCM_SIZES = RTCM_OBSERVATION_SIZES | RTCM_STATIC_SIZES


class ApplicationError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    typ: int
    sender_session: int
    message_id: int
    source_age_ms: int
    body: bytes


@dataclass(frozen=True)
class TelemetrySample:
    uptime_ms: int
    latitude_e7: int
    longitude_e7: int
    speed_mm_s: int
    battery_mv: int
    last_rtcm_transport_age_ms: int
    status_flags: int
    fix_type: int
    satellites: int


@dataclass(frozen=True)
class StopRequestBody:
    target_session: int
    command: int
    reason: int


@dataclass(frozen=True)
class CommandAckBody:
    request_session: int
    request_id: int
    result: int
    state: int


@dataclass(frozen=True)
class StopDecision:
    result: int
    state: int
    applied_now: bool


@dataclass
class _Assembly:
    started_at: float
    rtcm_type: int
    total_length: int
    fragment_count: int
    parts: dict[int, bytes] = field(default_factory=dict)


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
            )
    return crc


def crc24q(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x1864CFB) & 0xFFFFFF
                if crc & 0x800000
                else (crc << 1) & 0xFFFFFF
            )
    return crc


def encode(frame: Frame) -> bytes:
    if not 0 <= frame.typ <= 0xFF:
        raise ApplicationError("message type out of range")
    if not 0 < frame.sender_session <= 0xFFFFFFFF:
        raise ApplicationError("sender session out of range")
    if not 0 < frame.message_id <= 0xFFFFFFFF:
        raise ApplicationError("message id out of range")
    if not 0 <= frame.source_age_ms <= 0xFFFF:
        raise ApplicationError("source age out of range")
    if len(frame.body) > MAX_BODY_SIZE:
        raise ApplicationError("body too large")
    header_and_body = (
        MAGIC
        + bytes((VERSION, frame.typ, 0, 0))
        + struct.pack(
            ">IIHH",
            frame.sender_session,
            frame.message_id,
            frame.source_age_ms,
            len(frame.body),
        )
        + frame.body
    )
    return header_and_body + struct.pack(">H", crc16(header_and_body))


def decode(raw: bytes) -> Frame:
    if not HEADER_SIZE + CRC_SIZE <= len(raw) <= MAX_FRAME_SIZE:
        raise ApplicationError("application frame size")
    if raw[:2] != MAGIC or raw[2] != VERSION or raw[4:6] != b"\0\0":
        raise ApplicationError("application frame header")
    sender_session, message_id, source_age_ms, body_length = struct.unpack_from(
        ">IIHH", raw, 6
    )
    if not sender_session or not message_id:
        raise ApplicationError("zero application identity")
    if body_length > MAX_BODY_SIZE or len(raw) != HEADER_SIZE + body_length + 2:
        raise ApplicationError("application frame length")
    if crc16(raw[:-2]) != int.from_bytes(raw[-2:], "big"):
        raise ApplicationError("application frame CRC")
    return Frame(raw[3], sender_session, message_id, source_age_ms, raw[HEADER_SIZE:-2])


def encode_hello(role: int, capabilities: int) -> bytes:
    if role not in (ROLE_BASE, ROLE_ROBOT) or not 0 <= capabilities <= 0xFFFF:
        raise ApplicationError("HELLO fields")
    return struct.pack(">BH", role, capabilities)


def decode_hello(body: bytes) -> tuple[int, int]:
    if len(body) != 3:
        raise ApplicationError("HELLO body")
    role, capabilities = struct.unpack(">BH", body)
    if role not in (ROLE_BASE, ROLE_ROBOT):
        raise ApplicationError("HELLO role")
    return role, capabilities


def encode_telemetry(sample: TelemetrySample) -> bytes:
    try:
        return struct.pack(
            ">IiiHHHHBBH",
            sample.uptime_ms,
            sample.latitude_e7,
            sample.longitude_e7,
            sample.speed_mm_s,
            sample.battery_mv,
            sample.last_rtcm_transport_age_ms,
            sample.status_flags,
            sample.fix_type,
            sample.satellites,
            0,
        )
    except struct.error as exc:
        raise ApplicationError("telemetry field range") from exc


def decode_telemetry(body: bytes) -> TelemetrySample:
    if len(body) != 24:
        raise ApplicationError("telemetry body")
    values = struct.unpack(">IiiHHHHBBH", body)
    if values[-1] != 0:
        raise ApplicationError("telemetry reserved field")
    return TelemetrySample(*values[:-1])


def encode_stop(request: StopRequestBody) -> bytes:
    if not 0 < request.target_session <= 0xFFFFFFFF:
        raise ApplicationError("STOP target session")
    if not 0 <= request.command <= 0xFF or not 0 <= request.reason <= 0xFF:
        raise ApplicationError("STOP fields")
    return struct.pack(">IBB", request.target_session, request.command, request.reason)


def decode_stop(body: bytes) -> StopRequestBody:
    if len(body) != 6:
        raise ApplicationError("STOP body")
    return StopRequestBody(*struct.unpack(">IBB", body))


def encode_ack(ack: CommandAckBody) -> bytes:
    if (
        not 0 < ack.request_session <= 0xFFFFFFFF
        or not 0 < ack.request_id <= 0xFFFFFFFF
    ):
        raise ApplicationError("ACK identity")
    if ack.result not in range(ACK_APPLIED, ACK_STALE_ID + 1):
        raise ApplicationError("ACK result")
    if ack.state not in (STATE_UNKNOWN, STATE_RUNNING, STATE_STOPPED):
        raise ApplicationError("ACK state")
    return struct.pack(
        ">IIBB", ack.request_session, ack.request_id, ack.result, ack.state
    )


def decode_ack(body: bytes) -> CommandAckBody:
    if len(body) != 10:
        raise ApplicationError("ACK body")
    ack = CommandAckBody(*struct.unpack(">IIBB", body))
    encode_ack(ack)
    return ack


def valid_rtcm3(raw: bytes) -> bool:
    if len(raw) < 6 or raw[0] != 0xD3 or raw[1] & 0xFC:
        return False
    payload_length = ((raw[1] & 0x03) << 8) | raw[2]
    return len(raw) == payload_length + 6 and crc24q(raw[:-3]) == int.from_bytes(
        raw[-3:], "big"
    )


def rtcm_type(raw: bytes) -> int:
    if not valid_rtcm3(raw):
        raise ApplicationError("invalid RTCM3 frame")
    return int.from_bytes(raw[3:5], "big") >> 4


def fragment(
    raw: bytes, sender_session: int, message_id: int, age_ms: int = 0
) -> list[Frame]:
    if not valid_rtcm3(raw) or len(raw) > MAX_RTCM_FRAME_SIZE:
        raise ApplicationError("RTCM frame")
    count = (len(raw) + RTCM_FRAGMENT_DATA_SIZE - 1) // RTCM_FRAGMENT_DATA_SIZE
    if count > MAX_RTCM_FRAGMENTS:
        raise ApplicationError("too many RTCM fragments")
    frames = []
    for index in range(count):
        data = raw[
            index * RTCM_FRAGMENT_DATA_SIZE : (index + 1) * RTCM_FRAGMENT_DATA_SIZE
        ]
        body = struct.pack(">HHBB", rtcm_type(raw), len(raw), index, count) + data
        frames.append(Frame(RTCM_FRAGMENT, sender_session, message_id, age_ms, body))
    return frames


class Reassembler:
    def __init__(self) -> None:
        self.items: OrderedDict[tuple[int, int], _Assembly] = OrderedDict()
        self.blocked_until: OrderedDict[tuple[int, int], float] = OrderedDict()
        self.timeout_count = 0
        self.conflict_count = 0
        self.eviction_count = 0
        self.duplicate_count = 0

    def _block(self, key: tuple[int, int], until: float) -> None:
        self.blocked_until[key] = until
        self.blocked_until.move_to_end(key)
        while len(self.blocked_until) > 64:
            self.blocked_until.popitem(last=False)

    def expire(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        expired = [
            key
            for key, item in self.items.items()
            if now - item.started_at >= REASSEMBLY_TIMEOUT_S
        ]
        for key in expired:
            item = self.items.pop(key)
            self._block(key, item.started_at + RTCM_TRANSPORT_TTL_MS / 1000)
            self.timeout_count += 1
        for key in [key for key, until in self.blocked_until.items() if now >= until]:
            del self.blocked_until[key]
        return len(expired)

    def add(self, frame: Frame, now: float | None = None) -> bytes | None:
        now = time.monotonic() if now is None else now
        self.expire(now)
        if frame.typ != RTCM_FRAGMENT or len(frame.body) < 6:
            raise ApplicationError("RTCM fragment body")
        if frame.source_age_ms >= RTCM_TRANSPORT_TTL_MS:
            raise ApplicationError("stale RTCM fragment")
        message_type, total_length, index, count = struct.unpack(
            ">HHBB", frame.body[:6]
        )
        data = frame.body[6:]
        expected_count = (
            total_length + RTCM_FRAGMENT_DATA_SIZE - 1
        ) // RTCM_FRAGMENT_DATA_SIZE
        expected_length = (
            RTCM_FRAGMENT_DATA_SIZE
            if index < count - 1
            else total_length - RTCM_FRAGMENT_DATA_SIZE * (count - 1)
        )
        if (
            not total_length
            or total_length > MAX_RTCM_FRAME_SIZE
            or count != expected_count
            or not 1 <= count <= MAX_RTCM_FRAGMENTS
            or index >= count
            or len(data) != expected_length
        ):
            raise ApplicationError("RTCM fragment shape")
        key = (frame.sender_session, frame.message_id)
        if key in self.blocked_until:
            raise ApplicationError("expired or completed RTCM message")
        if key not in self.items:
            if len(self.items) == MAX_REASSEMBLIES:
                evicted_key, evicted = self.items.popitem(last=False)
                self._block(
                    evicted_key, evicted.started_at + RTCM_TRANSPORT_TTL_MS / 1000
                )
                self.eviction_count += 1
            self.items[key] = _Assembly(now, message_type, total_length, count)
        assembly = self.items[key]
        if (assembly.rtcm_type, assembly.total_length, assembly.fragment_count) != (
            message_type,
            total_length,
            count,
        ):
            del self.items[key]
            self._block(key, assembly.started_at + RTCM_TRANSPORT_TTL_MS / 1000)
            self.conflict_count += 1
            raise ApplicationError("RTCM fragment metadata conflict")
        if index in assembly.parts:
            if assembly.parts[index] != data:
                del self.items[key]
                self._block(key, assembly.started_at + RTCM_TRANSPORT_TTL_MS / 1000)
                self.conflict_count += 1
                raise ApplicationError("RTCM fragment data conflict")
            self.duplicate_count += 1
            return None
        assembly.parts[index] = data
        if len(assembly.parts) < count:
            return None
        raw = b"".join(assembly.parts[part_index] for part_index in range(count))
        del self.items[key]
        self._block(key, assembly.started_at + RTCM_TRANSPORT_TTL_MS / 1000)
        if (
            len(raw) != total_length
            or not valid_rtcm3(raw)
            or rtcm_type(raw) != message_type
        ):
            raise ApplicationError("assembled RTCM frame")
        return raw


class StopCache:
    def __init__(self, local_session: int, ttl_ms: int = 2000) -> None:
        if not 0 < local_session <= 0xFFFFFFFF:
            raise ApplicationError("local session")
        self.local_session = local_session
        self.ttl_ms = ttl_ms
        self.state = STATE_RUNNING
        self.high_water: dict[int, int] = {}
        self.cache: OrderedDict[tuple[int, int], tuple[bytes, int, int]] = OrderedDict()
        self.apply_count = 0

    def _remember(self, frame: Frame, result: int) -> StopDecision:
        key = (frame.sender_session, frame.message_id)
        self.cache[key] = (frame.body, result, self.state)
        self.cache.move_to_end(key)
        while len(self.cache) > 64:
            self.cache.popitem(last=False)
        return StopDecision(result, self.state, False)

    def apply(self, frame: Frame) -> StopDecision:
        if frame.typ != STOP_REQUEST:
            raise ApplicationError("not a STOP request")
        key = (frame.sender_session, frame.message_id)
        if key in self.cache:
            body, result, state = self.cache[key]
            if body != frame.body:
                return StopDecision(ACK_ID_CONFLICT, self.state, False)
            return StopDecision(result, state, False)
        try:
            request = decode_stop(frame.body)
        except ApplicationError:
            return self._remember(frame, ACK_UNSUPPORTED_COMMAND)
        if request.target_session != self.local_session:
            return self._remember(frame, ACK_WRONG_TARGET_SESSION)
        if request.command != STOP_COMMAND:
            return self._remember(frame, ACK_UNSUPPORTED_COMMAND)
        if frame.source_age_ms > self.ttl_ms:
            return self._remember(frame, ACK_EXPIRED)
        if frame.message_id <= self.high_water.get(frame.sender_session, 0):
            return StopDecision(ACK_STALE_ID, self.state, False)
        self.high_water[frame.sender_session] = frame.message_id
        if self.state == STATE_STOPPED:
            return self._remember(frame, ACK_ALREADY_STOPPED)
        self.state = STATE_STOPPED
        self.apply_count += 1
        decision = self._remember(frame, ACK_APPLIED)
        return StopDecision(decision.result, decision.state, True)

    def reset_mock_state(self) -> None:
        """Test fixture only; no radio command can re-arm the mock state."""
        self.state = STATE_RUNNING


def synthetic_rtcm(message_type: int, length: int, seed: int = 0) -> bytes:
    """Build transport-valid, not semantically valid, synthetic RTCM3."""
    if not 6 <= length <= MAX_RTCM_FRAME_SIZE:
        raise ApplicationError("synthetic RTCM length")
    payload = bytearray(length - 6)
    payload[:2] = ((message_type << 4) & 0xFFFF).to_bytes(2, "big")
    for index in range(2, len(payload)):
        payload[index] = (seed * 31 + index * 17) & 0xFF
    header = b"\xd3" + bytes(((len(payload) >> 8) & 0x03, len(payload) & 0xFF))
    header_and_payload = header + payload
    return header_and_payload + crc24q(header_and_payload).to_bytes(3, "big")


def synthetic_epoch(epoch: int) -> list[bytes]:
    frames = [
        synthetic_rtcm(message_type, length, epoch)
        for message_type, length in RTCM_OBSERVATION_SIZES.items()
    ]
    if epoch % 10 == 0:
        frames.extend(
            synthetic_rtcm(message_type, length, epoch)
            for message_type, length in RTCM_STATIC_SIZES.items()
        )
    return frames
