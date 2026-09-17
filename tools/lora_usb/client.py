"""Small synchronous serial client for the Phase 2 modem bench (prototype)."""

import random
import time

from .protocol import (
    DIAGNOSTICS,
    ERROR,
    GET_DIAGNOSTICS,
    GET_LINK_STATUS,
    HELLO,
    INFO,
    LINK_STATUS,
    RADIO_RX,
    RADIO_SEND,
    RADIO_TX_RESULT,
    StreamDecoder,
    encode_frame,
)


class ModemError(RuntimeError):
    pass


class ModemClient:
    def __init__(self, port, baudrate=115200, timeout=0):
        try:
            import serial
        except ImportError as exc:
            raise ModemError("Install pyserial for physical modem use") from exc
        self.serial = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self.session = random.randrange(1, 2**32)
        self.sequence = 0
        self.decoder = StreamDecoder()
        self.inbox = []
        self.session_events = []
        self.info = None
        self.last_completed = None
        self.counters = {
            "usb_bytes_read": 0,
            "frames_decoded": 0,
            "wrong_session": 0,
            "radio_rx_events": 0,
            "radio_rx_events_matched": 0,
            "responses": 0,
        }
        self.serial.write(b"\0")

    def _next(self):
        self.sequence += 1
        if self.sequence == 0xFFFFFFFF:
            raise ModemError("sequence exhausted")
        return self.sequence

    def poll(self):
        data = self.serial.read(512)
        self.counters["usb_bytes_read"] += len(data)
        for frame in self.decoder.feed(data):
            self.counters["frames_decoded"] += 1
            if frame.session != self.session:
                self.counters["wrong_session"] += 1
            else:
                self.inbox.append(frame)
                if frame.msg_type == RADIO_RX:
                    self.counters["radio_rx_events"] += 1
                else:
                    self.counters["responses"] += 1
        return list(self.inbox)

    def _wait_response(self, msg_type, seq, deadline):
        while time.monotonic() < deadline:
            self.poll()
            for i, frame in enumerate(self.inbox):
                if frame.msg_type == msg_type and frame.sequence == seq:
                    return self.inbox.pop(i)
                if frame.msg_type == ERROR and frame.sequence == seq:
                    raise ModemError(frame.payload.hex())
            time.sleep(0.001)
        raise TimeoutError("correlated response timeout")

    def hello(self, deadline=2.0):
        seq = self._next()
        self.serial.write(encode_frame(HELLO, self.session, seq))
        frame = self._wait_response(INFO, seq, time.monotonic() + deadline)
        self.info = frame.payload
        return frame

    def send(self, payload, deadline=3.0):
        seq = self._next()
        started = time.monotonic()
        self.serial.write(encode_frame(RADIO_SEND, self.session, seq, payload))
        frame = self._wait_response(RADIO_TX_RESULT, seq, started + deadline)
        return frame, time.monotonic() - started

    def events(self):
        self.poll()
        events = [f for f in self.inbox if f.msg_type == RADIO_RX]
        self.inbox[:] = [f for f in self.inbox if f.msg_type != RADIO_RX]
        return events

    def take_radio_event(self, predicate):
        """Remove one matching radio event while preserving unrelated events."""
        self.poll()
        for index, frame in enumerate(self.inbox):
            if frame.msg_type == RADIO_RX and predicate(frame):
                self.counters["radio_rx_events_matched"] = (
                    self.counters.get("radio_rx_events_matched", 0) + 1
                )
                return self.inbox.pop(index)
        return None

    def get_link_status(self, deadline=2.0):
        seq = self._next()
        self.serial.write(encode_frame(GET_LINK_STATUS, self.session, seq))
        return self._wait_response(LINK_STATUS, seq, time.monotonic() + deadline)

    def get_diagnostics(self, deadline=2.0):
        seq = self._next()
        self.serial.write(encode_frame(GET_DIAGNOSTICS, self.session, seq))
        return self._wait_response(DIAGNOSTICS, seq, time.monotonic() + deadline)

    def close(self):
        self.serial.close()
