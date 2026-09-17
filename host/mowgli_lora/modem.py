"""Resilient host transport with explicit stage counters and boot-id recovery."""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from collections.abc import Callable

from tools.lora_usb import protocol
from tools.lora_usb.protocol import StreamDecoder

from .metrics import Metrics

LOG = logging.getLogger(__name__)


class ModemTransport:
    def __init__(
        self,
        device: str,
        metrics: Metrics,
        baudrate: int = 115200,
        reconnect_seconds: float = 1.0,
        serial_factory: Callable[..., object] | None = None,
    ) -> None:
        self.device, self.metrics = device, metrics
        self.baudrate, self.reconnect_seconds = baudrate, reconnect_seconds
        self._serial_factory = serial_factory
        self.serial: object | None = None
        self.decoder = StreamDecoder()
        self.session = random.randrange(1, 2**32)
        self.sequence = 0
        self.boot_id: int | None = None
        self.handshake_complete = False
        self.pending: dict[int, tuple[int, float]] = {}
        self.received: deque[bytes] = deque()
        self._tx_outcomes: deque[tuple[int, bool]] = deque()
        self._next_attempt = 0.0
        self._last_diagnostics = 0.0

    @property
    def connected(self) -> bool:
        return self.serial is not None

    def _open(self) -> None:
        if self.serial is not None or time.monotonic() < self._next_attempt:
            return
        try:
            if self._serial_factory is None:
                import serial

                self.serial = serial.Serial(self.device, self.baudrate, timeout=0)
            else:
                self.serial = self._serial_factory(
                    self.device, self.baudrate, timeout=0
                )
            self.decoder = StreamDecoder()
            self.pending.clear()
            self.metrics.add("usb_reconnects")
            self._write(b"\0")
            self._send_hello()
        except (OSError, RuntimeError) as exc:
            self.metrics.add("usb_connect_failures")
            LOG.warning("modem open failed for %s: %s", self.device, exc)
            self._disconnect()

    def _disconnect(self) -> None:
        for sequence, (typ, _) in self.pending.items():
            if typ == protocol.RADIO_SEND:
                self._tx_outcomes.append((sequence, False))
        if self.serial is not None:
            try:
                self.serial.close()
            except OSError as exc:
                LOG.debug("modem close failed: %s", exc)
        self.serial = None
        self.pending.clear()
        self.handshake_complete = False
        self._next_attempt = time.monotonic() + self.reconnect_seconds

    def _write(self, raw: bytes) -> None:
        if self.serial is None:
            raise OSError("modem disconnected")
        try:
            offset = 0
            while offset < len(raw):
                written = self.serial.write(raw[offset:])
                if not written:
                    raise OSError("partial USB serial write")
                offset += written
                self.metrics.add("usb_bytes_written", written)
        except OSError:
            self.metrics.add("usb_write_failures")
            self._disconnect()
            raise

    def _next(self) -> int:
        self.sequence = 1 if self.sequence == 0xFFFFFFFF else self.sequence + 1
        return self.sequence

    def _request(self, typ: int, payload: bytes = b"") -> int:
        sequence = self._next()
        self.metrics.add("usb_send_requests")
        self._write(protocol.encode_frame(typ, self.session, sequence, payload))
        self.pending[sequence] = (typ, time.monotonic())
        self.metrics.add("usb_send_accepted")
        return sequence

    def _send_hello(self) -> None:
        self._request(protocol.HELLO)

    def send_air(self, payload: bytes) -> int | None:
        if not self.ready_for_send:
            self.metrics.add("radio_send_disconnected")
            return None
        self.metrics.add("application_frames_generated")
        self.metrics.add("lora_bytes_queued", len(payload))
        return self._request(protocol.RADIO_SEND, payload)

    @property
    def ready_for_send(self) -> bool:
        """Firmware accepts one command window; never overlap host requests."""
        return self.connected and self.handshake_complete and not self.pending

    def take_tx_outcomes(self) -> list[tuple[int, bool]]:
        outcomes = list(self._tx_outcomes)
        self._tx_outcomes.clear()
        return outcomes

    def tick(self) -> list[bytes]:
        self._open()
        if self.serial is None:
            return []
        try:
            raw = self.serial.read(4096)
        except OSError:
            self.metrics.add("usb_read_failures")
            self._disconnect()
            return []
        if raw:
            self.metrics.add("host_usb_bytes_received", len(raw))
        for frame in self.decoder.feed(raw):
            self.metrics.add("host_usb_frames_received")
            if frame.session != self.session:
                self.metrics.add("usb_wrong_session")
                continue
            if frame.msg_type == protocol.INFO:
                self.pending.pop(frame.sequence, None)
                self._handle_info(frame.sequence, frame.payload)
            elif frame.msg_type == protocol.RADIO_TX_RESULT:
                request = self.pending.pop(frame.sequence, None)
                if request is not None and request[0] == protocol.RADIO_SEND:
                    self._tx_outcomes.append((frame.sequence, True))
                    self.metrics.add("radio_tx_completed")
            elif frame.msg_type == protocol.RADIO_RX:
                self.metrics.add("radio_rx_received")
                try:
                    *_, payload = protocol.parse_radio_rx(frame.payload)
                    self.received.append(payload)
                    self.metrics.add("rx_event_queued")
                    self.metrics.add("application_frames_decoded")
                except protocol.ProtocolError:
                    self.metrics.add("radio_rx_decode_failures")
            elif frame.msg_type == protocol.DIAGNOSTICS:
                self.pending.pop(frame.sequence, None)
                self._handle_diagnostics(frame.payload)
            elif frame.msg_type == protocol.ERROR:
                request = self.pending.pop(frame.sequence, None)
                if request is not None and request[0] == protocol.RADIO_SEND:
                    self._tx_outcomes.append((frame.sequence, False))
                elif request is not None:
                    self._handle_error(request[0], frame.payload)
                self.metrics.add("modem_errors")
        now = time.monotonic()
        for sequence, (typ, started) in list(self.pending.items()):
            if now - started > 3.0:
                self.pending.pop(sequence)
                if typ == protocol.RADIO_SEND:
                    self._tx_outcomes.append((sequence, False))
                self.metrics.add("usb_response_timeouts")
        if (
            self.handshake_complete
            and not self.pending
            and now - self._last_diagnostics >= 1.0
        ):
            self._last_diagnostics = now
            try:
                self._request(protocol.GET_DIAGNOSTICS)
            except OSError:
                pass
        events = list(self.received)
        self.received.clear()
        return events

    def _handle_error(self, request_type: int, payload: bytes) -> None:
        try:
            code, _, failed_type = protocol.parse_error(payload)
        except protocol.ProtocolError:
            self.metrics.add("modem_error_decode_failures")
            return
        if failed_type != request_type:
            self.metrics.add("modem_error_type_mismatch")
            return
        if (
            request_type == protocol.GET_DIAGNOSTICS
            and code == protocol.ERROR_CODE_WRONG_SESSION
        ):
            self.metrics.add("modem_wrong_session_recoveries")
            self._restart_host_session("diagnostics rejected after modem restart")

    def _restart_host_session(self, reason: str) -> None:
        for sequence, (typ, _) in self.pending.items():
            if typ == protocol.RADIO_SEND:
                self._tx_outcomes.append((sequence, False))
        self.pending.clear()
        self.received.clear()
        self.session = random.randrange(1, 2**32)
        self.sequence = 0
        self.handshake_complete = False
        LOG.warning("host session reset: %s", reason)
        self._send_hello()

    def _handle_info(self, _sequence: int, payload: bytes) -> None:
        try:
            _, boot_id, *_ = protocol.parse_info(payload)
        except protocol.ProtocolError:
            self.metrics.add("info_decode_failures")
            return
        if self.boot_id is not None and boot_id != self.boot_id:
            self.metrics.add("modem_boot_id_changes")
            self._restart_host_session("boot ID changed")
            self.boot_id = boot_id
            self.metrics.set("modem_boot_id", boot_id)
            return
        self.boot_id = boot_id
        self.handshake_complete = True
        self.metrics.set("modem_boot_id", boot_id)
        self.metrics.set("modem_connected", 1)

    def _handle_diagnostics(self, payload: bytes) -> None:
        try:
            values = protocol.parse_diagnostics(payload)
        except protocol.ProtocolError:
            self.metrics.add("diagnostics_decode_failures")
            return
        for name, value in zip(
            (
                "modem_usb_frames",
                "modem_usb_accepted",
                "modem_send_accepted",
                "modem_tx_started",
                "modem_tx_completed",
                "modem_radio_rx_received",
                "modem_rx_event_queued",
                "modem_rx_event_written_usb",
                "modem_event_drops",
                "modem_event_queue_depth",
            ),
            values,
            strict=True,
        ):
            self.metrics.set(name, value)

    def close(self) -> None:
        self._disconnect()
        self.metrics.set("modem_connected", 0)
