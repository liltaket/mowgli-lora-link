"""Resilient host transport with explicit stage counters and boot-id recovery."""

from __future__ import annotations

import logging
import random
import time
from collections import OrderedDict, deque
from collections.abc import Callable

from tools.lora_usb import protocol
from tools.lora_usb.protocol import StreamDecoder

from .metrics import Metrics

LOG = logging.getLogger(__name__)


class ModemTransport:
    MAX_LATE_RADIO_RESULTS = 32
    LATE_RADIO_RESULT_TTL_S = 30.0

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
        self.radio_ready = False
        self.pending: dict[int, tuple[int, float]] = {}
        self.received: deque[bytes] = deque()
        self._tx_outcomes: deque[tuple[int, bool | None]] = deque()
        self._timed_out_radio: OrderedDict[int, float] = OrderedDict()
        self._next_attempt = 0.0
        self._last_diagnostics = 0.0
        self._last_radio_rx = 0.0
        self.metrics.set("modem_connected", 0)
        self.metrics.set("modem_handshake_complete", 0)
        self.metrics.set("modem_ready_for_send", 0)
        self.metrics.set("modem_radio_ready", 0)

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
            # Firmware keeps its last host session across a USB re-open. A
            # fresh host session avoids a stale firmware BUSY/pending window
            # wedging this new physical connection.
            self._begin_fresh_session()
            self.metrics.add("usb_reconnects")
            self.metrics.set("modem_connected", 1)
            self.metrics.set("modem_handshake_complete", 0)
            self._write(b"\0")
            self._send_hello()
        except (OSError, RuntimeError) as exc:
            self.metrics.add("usb_connect_failures")
            LOG.warning("modem open failed for %s: %s", self.device, exc)
            self._disconnect()

    def _disconnect(self) -> None:
        for sequence, (typ, _) in self.pending.items():
            if typ == protocol.RADIO_SEND:
                # A completed USB write only proves firmware accepted the
                # request bytes. If the device disappears before its terminal
                # response, the RF outcome is unknown and must never be
                # reported as a definite failure or retried automatically.
                self._tx_outcomes.append((sequence, None))
                self.metrics.add("radio_send_disconnect_uncertain")
        if self.serial is not None:
            try:
                self.serial.close()
            except OSError as exc:
                LOG.debug("modem close failed: %s", exc)
        self.serial = None
        self.pending.clear()
        self.received.clear()
        self._timed_out_radio.clear()
        self.handshake_complete = False
        self.radio_ready = False
        # These must change synchronously with a USB error: /healthz is often
        # the only signal systemd/supervisors have before the next reconnect.
        self.metrics.set("modem_connected", 0)
        self.metrics.set("modem_handshake_complete", 0)
        self.metrics.set("modem_ready_for_send", 0)
        self.metrics.set("modem_radio_ready", 0)
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

    def _begin_fresh_session(self) -> None:
        previous = self.session
        self.session = random.randrange(1, 2**32)
        if self.session == previous:
            self.session = 1 if previous == 0xFFFFFFFF else previous + 1
        self.sequence = 0
        self._timed_out_radio.clear()
        self.handshake_complete = False
        self.radio_ready = False
        self._last_radio_rx = 0.0
        self.metrics.set("modem_handshake_complete", 0)
        self.metrics.set("modem_ready_for_send", 0)
        self.metrics.set("modem_radio_ready", 0)

    def _rotate_before_sequence_wrap(self) -> bool:
        if self.sequence != 0xFFFFFFFF:
            return False
        self.metrics.add("usb_session_rotations_sequence_wrap")
        self._restart_host_session("USB sequence wrapped")
        return True

    def _request(self, typ: int, payload: bytes = b"") -> int:
        if self.sequence == 0xFFFFFFFF:
            raise RuntimeError("rotate host session before issuing a request")
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
        if self._rotate_before_sequence_wrap():
            self.metrics.add("radio_send_session_rotated")
            return None
        self.metrics.add("application_frames_generated")
        self.metrics.add("lora_bytes_queued", len(payload))
        try:
            return self._request(protocol.RADIO_SEND, payload)
        except OSError:
            # _write already reset the USB state and queued outcomes for any
            # older request. This new request was never accepted by firmware.
            self.metrics.add("radio_send_usb_write_failures")
            return None

    @property
    def ready_for_send(self) -> bool:
        """Firmware accepts one command window; never overlap host requests."""
        return (
            self.connected
            and self.handshake_complete
            and self.radio_ready
            and not self.pending
        )

    def take_tx_outcomes(self) -> list[tuple[int, bool | None]]:
        outcomes = list(self._tx_outcomes)
        self._tx_outcomes.clear()
        return outcomes

    def tick(self, *, diagnostics_idle: bool = True) -> list[bytes]:
        self._open()
        if self.serial is None:
            self.metrics.set("modem_ready_for_send", 0)
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
                request = self.pending.get(frame.sequence)
                if request is None or request[0] != protocol.HELLO:
                    self.metrics.add("usb_unexpected_info")
                    continue
                if not self._handle_info(frame.payload):
                    self.metrics.add("hello_invalid_info")
                    self._restart_host_session("malformed INFO during HELLO")
                    continue
                self.pending.pop(frame.sequence)
            elif frame.msg_type == protocol.RADIO_TX_RESULT:
                request = self.pending.get(frame.sequence)
                if request is None and frame.sequence in self._timed_out_radio:
                    self._timed_out_radio.pop(frame.sequence)
                    try:
                        protocol.parse_tx_result(frame.payload)
                    except protocol.ProtocolError:
                        self.metrics.add("radio_tx_late_result_decode_failures")
                    else:
                        self.metrics.add("radio_tx_late_completed")
                    continue
                if request is None or request[0] != protocol.RADIO_SEND:
                    self.metrics.add("usb_unexpected_radio_tx_result")
                    continue
                try:
                    protocol.parse_tx_result(frame.payload)
                except protocol.ProtocolError:
                    self.pending.pop(frame.sequence)
                    self._tx_outcomes.append((frame.sequence, False))
                    self.metrics.add("radio_tx_result_decode_failures")
                    continue
                self.pending.pop(frame.sequence)
                self._tx_outcomes.append((frame.sequence, True))
                self.metrics.add("radio_tx_completed")
            elif frame.msg_type == protocol.RADIO_RX:
                self.metrics.add("radio_rx_received")
                self._last_radio_rx = time.monotonic()
                try:
                    *_, payload = protocol.parse_radio_rx(frame.payload)
                    self.received.append(payload)
                    self.metrics.add("rx_event_queued")
                    self.metrics.add("application_frames_decoded")
                except protocol.ProtocolError:
                    self.metrics.add("radio_rx_decode_failures")
            elif frame.msg_type == protocol.DIAGNOSTICS:
                request = self.pending.get(frame.sequence)
                if request is None or request[0] != protocol.GET_DIAGNOSTICS:
                    self.metrics.add("usb_unexpected_diagnostics")
                    continue
                self.pending.pop(frame.sequence)
                self._handle_diagnostics(frame.payload)
            elif frame.msg_type == protocol.ERROR:
                self._handle_error(frame.sequence, frame.payload)
        now = time.monotonic()
        self._expire_timed_out_radio(now)
        for sequence, (typ, started) in list(self.pending.items()):
            if now - started > 3.0:
                if typ == protocol.RADIO_SEND:
                    # The payload may already have gone over the air. Release
                    # the command window without retrying it and retain a
                    # bounded correlation tombstone for a late terminal
                    # response. A late result is observability only and must
                    # never complete a newer in-flight send.
                    self.pending.pop(sequence)
                    self._remember_timed_out_radio(sequence, now)
                    self._tx_outcomes.append((sequence, None))
                    self.metrics.add("radio_send_timeouts")
                    self.metrics.add("usb_response_timeouts")
                    continue
                self.pending.pop(sequence)
                if typ == protocol.HELLO:
                    self.metrics.add("hello_timeouts")
                    self._restart_host_session("HELLO response timed out")
                    break
                self.metrics.add("usb_response_timeouts")
        if (
            self.handshake_complete
            and not self.pending
            and diagnostics_idle
            and now - self._last_radio_rx >= 1.0
            and now - self._last_diagnostics >= 1.0
            and not self._rotate_before_sequence_wrap()
        ):
            self._last_diagnostics = now
            try:
                self._request(protocol.GET_DIAGNOSTICS)
            except OSError:
                pass
        self.metrics.set("modem_connected", int(self.connected))
        self.metrics.set("modem_handshake_complete", int(self.handshake_complete))
        self.metrics.set("modem_ready_for_send", int(self.ready_for_send))
        events = list(self.received)
        self.received.clear()
        return events

    def _remember_timed_out_radio(self, sequence: int, now: float) -> None:
        self._timed_out_radio[sequence] = now
        while len(self._timed_out_radio) > self.MAX_LATE_RADIO_RESULTS:
            self._timed_out_radio.popitem(last=False)
            self.metrics.add("radio_tx_timeout_tombstones_evicted")

    def _expire_timed_out_radio(self, now: float) -> None:
        while self._timed_out_radio:
            sequence, started = next(iter(self._timed_out_radio.items()))
            if now - started <= self.LATE_RADIO_RESULT_TTL_S:
                break
            self._timed_out_radio.pop(sequence)
            self.metrics.add("radio_tx_timeout_tombstones_expired")

    def _handle_error(self, sequence: int, payload: bytes) -> None:
        request = self.pending.get(sequence)
        if request is None:
            if sequence in self._timed_out_radio:
                try:
                    _, _, failed_type = protocol.parse_error(payload)
                except protocol.ProtocolError:
                    self.metrics.add("radio_tx_late_error_decode_failures")
                    return
                self._timed_out_radio.pop(sequence)
                if failed_type == protocol.RADIO_SEND:
                    self.metrics.add("radio_tx_late_errors")
                else:
                    self.metrics.add("radio_tx_late_error_type_mismatch")
                return
            self.metrics.add("usb_unexpected_error")
            return
        try:
            code, _, failed_type = protocol.parse_error(payload)
        except protocol.ProtocolError:
            self.metrics.add("modem_error_decode_failures")
            return
        request_type = request[0]
        if failed_type != request_type:
            self.metrics.add("modem_error_type_mismatch")
            return
        self.pending.pop(sequence)
        self.metrics.add("modem_errors")
        if request_type == protocol.RADIO_SEND:
            self._tx_outcomes.append((sequence, False))
        if request_type == protocol.HELLO:
            self.metrics.add("hello_errors")
            self._restart_host_session("firmware rejected HELLO")
        elif code == protocol.ERROR_CODE_WRONG_SESSION and request_type in {
            protocol.GET_DIAGNOSTICS,
            protocol.RADIO_SEND,
        }:
            self.metrics.add("modem_wrong_session_recoveries")
            self._restart_host_session("modem rejected request for wrong session")

    def _restart_host_session(self, reason: str) -> None:
        for sequence, (typ, _) in self.pending.items():
            if typ == protocol.RADIO_SEND:
                self._tx_outcomes.append((sequence, False))
        self.pending.clear()
        self.received.clear()
        self._begin_fresh_session()
        self.metrics.set("modem_handshake_complete", 0)
        self.metrics.set("modem_ready_for_send", 0)
        LOG.warning("host session reset: %s", reason)
        try:
            self._send_hello()
        except OSError:
            # A concurrent USB disappearance must leave the caller in its
            # normal reconnect loop rather than terminating the service.
            self.metrics.add("usb_session_reset_write_failures")

    def _handle_info(self, payload: bytes) -> bool:
        try:
            _, boot_id, _, _, radio_ready, _, _ = protocol.parse_info(payload)
        except protocol.ProtocolError:
            self.metrics.add("info_decode_failures")
            return False
        if self.boot_id is not None and boot_id != self.boot_id:
            self.metrics.add("modem_boot_id_changes")
        self.boot_id = boot_id
        self.handshake_complete = True
        self.radio_ready = bool(radio_ready)
        self.metrics.set("modem_boot_id", boot_id)
        self.metrics.set("modem_radio_ready", int(self.radio_ready))
        self.metrics.set("modem_connected", 1)
        self.metrics.set("modem_handshake_complete", 1)
        return True

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
