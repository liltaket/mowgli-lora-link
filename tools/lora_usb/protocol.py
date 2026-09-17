"""LoRa USB modem v1 framing and payload helpers (prototype)."""

import struct
from dataclasses import dataclass

MAGIC = b"MU"
VERSION = 1
MAX_AIR_PAYLOAD = 200
MAX_USB_PAYLOAD = 256
MAX_DECODED = 274

HELLO, INFO, RADIO_SEND, RADIO_TX_RESULT = 0x01, 0x81, 0x02, 0x82
GET_LINK_STATUS, LINK_STATUS = 0x03, 0x83
GET_DIAGNOSTICS, DIAGNOSTICS = 0x04, 0x84
RADIO_RX, ERROR = 0x90, 0xFF
ERROR_CODE_WRONG_SESSION = 4


class ProtocolError(ValueError):
    pass


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
            )
    return crc


def cobs_encode(data: bytes) -> bytes:
    if not data:
        return b"\x01"
    out = bytearray([0])
    code = 1
    code_pos = 0
    for byte in data:
        if byte == 0:
            out[code_pos] = code
            code_pos = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 0xFF:
                out[code_pos] = code
                code_pos = len(out)
                out.append(0)
                code = 1
    out[code_pos] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    if not data:
        raise ProtocolError("empty COBS frame")
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0:
            raise ProtocolError("zero COBS code")
        i += 1
        end = i + code - 1
        if end > len(data):
            raise ProtocolError("truncated COBS frame")
        out.extend(data[i:end])
        i = end
        if code != 0xFF and i < len(data):
            out.append(0)
    return bytes(out)


def encode_frame(
    msg_type: int, session: int, sequence: int, payload: bytes = b""
) -> bytes:
    if (
        not 0 <= msg_type <= 255
        or not 0 < session <= 0xFFFFFFFF
        or not 0 <= sequence <= 0xFFFFFFFF
    ):
        raise ProtocolError("invalid frame identity")
    if len(payload) > MAX_USB_PAYLOAD:
        raise ProtocolError("USB payload too large")
    body = (
        MAGIC
        + bytes((VERSION, msg_type, 0, 0))
        + struct.pack(">IIH", session, sequence, len(payload))
        + payload
    )
    return cobs_encode(body + struct.pack(">H", crc16(body))) + b"\0"


def decode_frame(encoded: bytes) -> tuple[int, int, int, bytes]:
    body = cobs_decode(encoded)
    if len(body) < 18 or len(body) > MAX_DECODED or body[:2] != MAGIC:
        raise ProtocolError("bad USB frame")
    if body[2] != VERSION or body[4] or body[5]:
        raise ProtocolError("unsupported USB header")
    length = struct.unpack_from(">H", body, 14)[0]
    if length > MAX_USB_PAYLOAD or len(body) != 18 + length:
        raise ProtocolError("USB length mismatch")
    if crc16(body[:-2]) != struct.unpack_from(">H", body, len(body) - 2)[0]:
        raise ProtocolError("USB CRC mismatch")
    return (
        body[3],
        struct.unpack_from(">I", body, 6)[0],
        struct.unpack_from(">I", body, 10)[0],
        body[16:-2],
    )


def encode_radio(boot_id: int, air_sequence: int, payload: bytes) -> bytes:
    if (
        not 0 < boot_id <= 0xFFFFFFFF
        or not 0 < air_sequence <= 0xFFFFFFFF
        or not 0 < len(payload) <= MAX_AIR_PAYLOAD
    ):
        raise ProtocolError("invalid radio packet")
    body = (
        b"ML"
        + bytes((1, 1, 0, 0))
        + struct.pack(">IIH", boot_id, air_sequence, len(payload))
        + payload
    )
    return body + struct.pack(">H", crc16(body))


def decode_radio(packet: bytes) -> tuple[int, int, bytes]:
    if len(packet) < 19 or packet[:2] != b"ML" or packet[2:6] != b"\x01\x01\0\0":
        raise ProtocolError("bad radio packet")
    n = struct.unpack_from(">H", packet, 14)[0]
    if (
        not 0 < n <= MAX_AIR_PAYLOAD
        or len(packet) != 18 + n
        or crc16(packet[:-2]) != struct.unpack_from(">H", packet, len(packet) - 2)[0]
    ):
        raise ProtocolError("bad radio packet")
    return struct.unpack_from(">II", packet, 6) + (packet[16:-2],)


def parse_mock_app(payload: bytes) -> tuple[int, int, bytes]:
    if len(payload) < 9 or payload[:2] != b"MB" or payload[2] != 1:
        raise ProtocolError("bad mock application packet")
    n = payload[8]
    if len(payload) != 9 + n:
        raise ProtocolError("mock application length mismatch")
    return payload[3], struct.unpack_from(">I", payload, 4)[0], payload[9:]


def make_mock_app(kind: int, sequence: int, data: bytes) -> bytes:
    if not 0 <= kind <= 255 or len(data) > 191:
        raise ProtocolError("bad mock application payload")
    return (
        b"MB\x01"
        + bytes((kind,))
        + struct.pack(">I", sequence)
        + bytes((len(data),))
        + data
    )


@dataclass(frozen=True)
class Frame:
    msg_type: int
    session: int
    sequence: int
    payload: bytes


def parse_info(p):
    if len(p) != 22:
        raise ProtocolError("bad INFO payload")
    return struct.unpack(">6sI I H B B I", p)


def parse_tx_result(p):
    if len(p) != 12:
        raise ProtocolError("bad TX_RESULT payload")
    return struct.unpack(">III", p)


def parse_radio_rx(p):
    if len(p) < 16:
        raise ProtocolError("bad RADIO_RX payload")
    sender, air, rssi, snr, uptime = struct.unpack_from(">IIhhI", p)
    return sender, air, rssi / 100.0, snr / 100.0, uptime, p[16:]


def parse_link_status(p):
    if len(p) != 36:
        raise ProtocolError("bad LINK_STATUS payload")
    boot, uptime, state = struct.unpack_from(">IIB", p)
    return (boot, uptime, state) + struct.unpack_from(">IIIIII", p, 12)


def parse_diagnostics(p):
    """Decode modem stage counters returned by GET_DIAGNOSTICS (0x04)."""
    if len(p) != 40:
        raise ProtocolError("bad DIAGNOSTICS payload")
    return struct.unpack(">IIIIIIIIII", p)


def parse_error(p):
    if len(p) != 5:
        raise ProtocolError("bad ERROR payload")
    return struct.unpack(">HhB", p)


class StreamDecoder:
    """Incremental delimiter decoder; oversized frames are discarded intact."""

    def __init__(self, max_encoded=380):
        self.buffer = bytearray()
        self.discarding = False
        self.max_encoded = max_encoded

    def feed(self, data):
        frames = []
        for byte in data:
            if byte == 0:
                if not self.discarding and self.buffer:
                    try:
                        frames.append(Frame(*decode_frame(bytes(self.buffer))))
                    except ProtocolError:
                        pass
                self.buffer.clear()
                self.discarding = False
            elif not self.discarding:
                self.buffer.append(byte)
                if len(self.buffer) > self.max_encoded:
                    self.buffer.clear()
                    self.discarding = True
        return frames


def split_stream(stream: bytearray, max_encoded: int = 380):
    """Yield valid decoded frames and silently recover at delimiters."""
    decoder = StreamDecoder(max_encoded)
    frames = decoder.feed(stream)
    stream.clear()
    stream.extend(decoder.buffer)
    return frames
