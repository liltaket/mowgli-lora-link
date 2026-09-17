"""Incremental, self-healing RTCM3 stream parser."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass

from tools.lora_usb.application import rtcm_type, valid_rtcm3

MAX_RTCM3_PAYLOAD = 1023


@dataclass(frozen=True)
class RtcmFrame:
    raw: bytes
    message_type: int
    received_at: float


class RtcmStreamParser:
    """Resynchronises at every D3 byte; invalid candidates never poison later input."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats: Counter[str] = Counter()
        self.message_types: Counter[int] = Counter()

    def reset(self) -> None:
        """Discard a partial stream after its underlying source changed.

        A TCP/USB reconnect is a stream boundary.  Keeping a partial RTCM
        frame from before that boundary could splice two unrelated sessions
        together, so retain the cumulative observability counters but never
        retain buffered bytes.
        """
        self.buffer.clear()
        self.stats["stream_resets"] += 1

    def feed(self, chunk: bytes, received_at: float | None = None) -> list[RtcmFrame]:
        now = time.monotonic() if received_at is None else received_at
        self.stats["input_bytes"] += len(chunk)
        self.buffer.extend(chunk)
        output: list[RtcmFrame] = []
        while self.buffer:
            start = self.buffer.find(0xD3)
            if start < 0:
                self.stats["garbage_bytes"] += len(self.buffer)
                self.buffer.clear()
                break
            if start:
                self.stats["garbage_bytes"] += start
                del self.buffer[:start]
            if len(self.buffer) < 3:
                break
            if self.buffer[1] & 0xFC:
                self.stats["header_failures"] += 1
                del self.buffer[0]
                continue
            length = ((self.buffer[1] & 3) << 8) | self.buffer[2]
            if length > MAX_RTCM3_PAYLOAD:
                self.stats["length_failures"] += 1
                del self.buffer[0]
                continue
            total = length + 6
            if len(self.buffer) < total:
                break
            raw = bytes(self.buffer[:total])
            del self.buffer[:total]
            if not valid_rtcm3(raw):
                self.stats["crc_failures"] += 1
                # Preserve potential D3 bytes inside a corrupt candidate.
                self.buffer[:0] = raw[1:]
                continue
            typ = rtcm_type(raw)
            self.stats["frames"] += 1
            self.stats["frame_bytes"] += len(raw)
            self.message_types[typ] += 1
            output.append(RtcmFrame(raw, typ, now))
        return output
