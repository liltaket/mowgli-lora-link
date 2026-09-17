"""Base and robot service loops. RTCM only; no mower-control integration."""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import sys
import time
from collections import deque
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

from tools.lora_usb import application

from .config import ServiceConfig, load
from .metrics import Metrics, serve_metrics
from .modem import ModemTransport
from .rtcm import RtcmFrame, RtcmStreamParser

LOG = logging.getLogger(__name__)


class BaseService:
    MAX_QUEUED_RTCM_FRAMES = 128

    def __init__(
        self, config: ServiceConfig, transport: ModemTransport, metrics: Metrics
    ) -> None:
        self.config, self.transport, self.metrics = config, transport, metrics
        self.parser = RtcmStreamParser()
        self.queue: deque[RtcmFrame] = deque()
        self.fragments: deque[tuple[application.Frame, float]] = deque()
        self.input_rate: deque[tuple[float, int, int]] = deque()
        self.lora_rate: deque[tuple[float, int]] = deque()
        self.in_flight: int | None = None
        self.session, self.message_id = transport.session, 0

    def ingest(self, chunk: bytes, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        frames = self.parser.feed(chunk, now)
        self.input_rate.append((now, len(chunk), len(frames)))
        self.metrics.add("rtcm_input_bytes", len(chunk))
        for frame in frames:
            self.queue.append(frame)
            if len(self.queue) > self.MAX_QUEUED_RTCM_FRAMES:
                self.queue.popleft()
                self.metrics.add("rtcm_frames_dropped_queue_full")
            self.metrics.add("rtcm_frames_ingested")
            self.metrics.add(f"rtcm_type_{frame.message_type}")
        self._update_queue_metrics(now)
        self.metrics.set("rtcm_crc_failures", self.parser.stats["crc_failures"])
        self._update_rates(now)
        return len(frames)

    def _update_rates(self, now: float) -> None:
        while self.input_rate and now - self.input_rate[0][0] >= 1.0:
            self.input_rate.popleft()
        while self.lora_rate and now - self.lora_rate[0][0] >= 1.0:
            self.lora_rate.popleft()
        self.metrics.set(
            "rtcm_input_bytes_per_second", sum(item[1] for item in self.input_rate)
        )
        self.metrics.set(
            "rtcm_input_frames_per_second", sum(item[2] for item in self.input_rate)
        )
        self.metrics.set(
            "lora_application_bytes_per_second", sum(item[1] for item in self.lora_rate)
        )

    def _update_queue_metrics(self, now: float) -> None:
        source_times = []
        if self.queue:
            source_times.append(self.queue[0].received_at)
        if self.fragments:
            source_times.append(self.fragments[0][1])
        self.metrics.set("rtcm_queue_depth", len(self.queue))
        self.metrics.set("rtcm_fragment_queue_depth", len(self.fragments))
        self.metrics.set("rtcm_fragment_in_flight", int(self.in_flight is not None))
        self.metrics.set(
            "rtcm_oldest_queued_age_ms",
            int(1000 * (now - min(source_times))) if source_times else 0,
        )

    def step(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.transport.tick()
        if self.transport.session != self.session:
            # A modem restart invalidates modem-side state. Re-fragment only
            # source frames that remain fresh under a new app sender session.
            self.session, self.message_id = self.transport.session, 0
            self.fragments.clear()
            self.in_flight = None
            self.metrics.add("app_sender_session_resets")
        for sequence, completed in self.transport.take_tx_outcomes():
            if sequence != self.in_flight:
                self.metrics.add("radio_tx_unexpected_outcomes")
                continue
            self.in_flight = None
            if completed:
                self.metrics.add("rtcm_fragments_tx_completed")
            else:
                self.fragments.clear()
                self.metrics.add("rtcm_fragments_tx_failed")
        while self.queue and now - self.queue[0].received_at > 0.8:
            self.queue.popleft()
            self.metrics.add("rtcm_frames_dropped_stale")
        if not self.transport.connected:
            self._update_queue_metrics(now)
            return
        if not self.fragments and self.queue:
            frame = self.queue.popleft()
            self.message_id = (
                1 if self.message_id == 0xFFFFFFFF else self.message_id + 1
            )
            try:
                fragments = application.fragment(
                    frame.raw, self.session, self.message_id
                )
            except application.ApplicationError:
                self.metrics.add("rtcm_fragment_failures")
            else:
                for fragment in fragments:
                    self.fragments.append((fragment, frame.received_at))
        if self.in_flight is None and self.fragments and self.transport.ready_for_send:
            fragment, received_at = self.fragments[0]
            source_age_ms = int((now - received_at) * 1000)
            if source_age_ms >= application.RTCM_TRANSPORT_TTL_MS:
                self.fragments.clear()
                self.metrics.add("rtcm_fragments_dropped_stale")
            else:
                self.fragments.popleft()
                encoded = application.encode(
                    replace(fragment, source_age_ms=source_age_ms)
                )
                sequence = self.transport.send_air(encoded)
                if sequence is None:
                    self.fragments.appendleft((fragment, received_at))
                    self.metrics.add("rtcm_fragments_dropped_disconnected")
                else:
                    self.in_flight = sequence
                    self.metrics.add("rtcm_fragments_usb_accepted")
                    self.metrics.add("lora_bytes_sent", len(encoded))
                    self.lora_rate.append((now, len(encoded)))
        self._update_rates(now)
        self._update_queue_metrics(now)

    @property
    def idle(self) -> bool:
        return not self.queue and not self.fragments and self.in_flight is None


class TcpRtcmOutput:
    """Nonblocking broadcast with bounded per-client queues."""

    MAX_CLIENT_QUEUE_BYTES = 256 * 1024

    def __init__(self, bind: str, port: int, metrics: Metrics | None = None) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((bind, port))
        self.socket.listen()
        self.socket.setblocking(False)
        self.metrics = metrics or Metrics()
        self.clients: dict[socket.socket, bytearray] = {}

    def _accept(self) -> None:
        try:
            while True:
                client, _ = self.socket.accept()
                client.setblocking(False)
                self.clients[client] = bytearray()
                self.metrics.add("rtcm_tcp_clients_connected")
        except BlockingIOError:
            return

    def _drop(self, client: socket.socket, reason: str) -> None:
        pending = len(self.clients.pop(client, b""))
        try:
            client.close()
        except OSError:
            pass
        self.metrics.add(f"rtcm_tcp_client_dropped_{reason}")
        self.metrics.add("rtcm_tcp_client_bytes_abandoned", pending)

    def flush(self) -> None:
        self._accept()
        for client, pending in list(self.clients.items()):
            if not pending:
                continue
            try:
                sent = client.send(pending)
            except BlockingIOError:
                continue
            except OSError:
                self._drop(client, "socket_error")
                continue
            if sent <= 0:
                self._drop(client, "closed")
            else:
                del pending[:sent]
                self.metrics.add("rtcm_tcp_bytes_sent", sent)
        self.metrics.set(
            "rtcm_tcp_client_queue_bytes", sum(map(len, self.clients.values()))
        )

    def publish(self, raw: bytes) -> int:
        self._accept()
        delivered = 0
        for client, pending in list(self.clients.items()):
            if len(pending) + len(raw) > self.MAX_CLIENT_QUEUE_BYTES:
                self._drop(client, "slow")
                continue
            pending.extend(raw)
            delivered += 1
            self.metrics.add("rtcm_tcp_bytes_queued", len(raw))
        self.flush()
        return delivered

    def close(self) -> None:
        for client in self.clients:
            client.close()
        self.socket.close()


class RobotService:
    def __init__(
        self, transport: ModemTransport, output: TcpRtcmOutput, metrics: Metrics
    ) -> None:
        self.transport, self.output, self.metrics = transport, output, metrics
        self.reassembler = application.Reassembler()

    def step(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for payload in self.transport.tick():
            try:
                frame = application.decode(payload)
            except application.ApplicationError:
                self.metrics.add("application_decode_failures")
                continue
            if frame.typ != application.RTCM_FRAGMENT:
                self.metrics.add("application_non_rtcm_frames")
                continue
            try:
                raw = self.reassembler.add(frame, now)
                self.metrics.add("rtcm_fragments_accepted")
            except application.ApplicationError:
                self.metrics.add("rtcm_fragments_rejected")
                continue
            if raw is not None:
                self.metrics.add("rtcm_frames_completed")
                self.metrics.add("rtcm_frames_delivered")
                self.metrics.add("rtcm_output_bytes", len(raw))
                self.metrics.add("rtcm_tcp_clients_delivered", self.output.publish(raw))
        self.reassembler.expire(now)
        self.output.flush()
        self.metrics.set("rtcm_reassembly_timeouts", self.reassembler.timeout_count)
        self.metrics.set("rtcm_reassembly_conflicts", self.reassembler.conflict_count)


def _input_chunks(config: dict[str, object], stopped: list[bool]):
    typ = config.get("type", "stdin")
    if typ == "stdin":
        while not stopped[0]:
            chunk = sys.stdin.buffer.read1(4096)
            if not chunk:
                return
            yield chunk
    if typ == "tcp":
        with closing(
            socket.create_connection((str(config["host"]), int(config["port"])))
        ) as source:
            source.settimeout(0.2)
            while not stopped[0]:
                try:
                    chunk = source.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    return
                yield chunk
        return
    if typ == "serial":
        import serial

        with serial.Serial(
            str(config["device"]), int(config.get("baudrate", 115200)), timeout=1
        ) as source:
            while not stopped[0]:
                if chunk := source.read(4096):
                    yield chunk
        return
    raise ValueError("rtcm.input.type must be stdin, tcp, or serial")


class _InputPump:
    """Leaves blocking ingress in a daemon; service loop remains reconnectable."""

    def __init__(self, config: dict[str, object], stopped: list[bool]) -> None:
        self.queue: Queue[bytes | Exception | None] = Queue(maxsize=64)
        self.thread = Thread(target=self._read, args=(config, stopped), daemon=True)

    def _read(self, config: dict[str, object], stopped: list[bool]) -> None:
        try:
            for chunk in _input_chunks(config, stopped):
                self.queue.put(chunk)
        except (OSError, RuntimeError, ValueError) as exc:
            self.queue.put(exc)
        finally:
            self.queue.put(None)

    def start(self) -> None:
        self.thread.start()


def _run_base(
    config: ServiceConfig,
    metrics: Metrics,
    stopped: list[bool],
    transport_factory=ModemTransport,
    input_pump_factory=_InputPump,
) -> None:
    modem = transport_factory(
        config.modem.device,
        metrics,
        config.modem.baudrate,
        config.modem.reconnect_seconds,
    )
    service = BaseService(config, modem, metrics)
    pump = input_pump_factory(config.rtcm_input, stopped)
    pump.start()
    source_done = False
    try:
        while not stopped[0]:
            try:
                item = pump.queue.get(timeout=0.02)
            except Empty:
                item = b""
            if isinstance(item, Exception):
                raise item
            if item is None:
                source_done = True
            elif item:
                service.ingest(item)
            service.step()
            if source_done and service.idle:
                break
    finally:
        modem.close()


def _run_robot(config: ServiceConfig, metrics: Metrics, stopped: list[bool]) -> None:
    output = config.rtcm_output
    if output.get("type", "tcp") != "tcp":
        raise ValueError("only rtcm.output.type=tcp is currently supported")
    publisher = TcpRtcmOutput(
        str(output.get("bind", "127.0.0.1")), int(output["port"]), metrics
    )
    modem = ModemTransport(
        config.modem.device,
        metrics,
        config.modem.baudrate,
        config.modem.reconnect_seconds,
    )
    service = RobotService(modem, publisher, metrics)
    try:
        while not stopped[0]:
            service.step()
            time.sleep(0.002)
    finally:
        publisher.close()
        modem.close()


def _main(role: str) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = load(args.config)
    if config.role != role:
        raise SystemExit(f"config role is {config.role}, expected {role}")
    logging.basicConfig(
        level=config.logging_level, format="%(asctime)s %(levelname)s %(message)s"
    )
    metrics = Metrics()
    server = serve_metrics(metrics, config.metrics_bind, config.metrics_port)
    stopped = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopped.__setitem__(0, True))
    try:
        (_run_base if role == "base" else _run_robot)(config, metrics, stopped)
    finally:
        server.shutdown()


def main_base() -> None:
    _main("base")


def main_robot() -> None:
    _main("robot")
