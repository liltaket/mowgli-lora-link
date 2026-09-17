"""Base and robot service loops. RTCM only; no mower-control integration."""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import sys
import time
from collections import deque
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread

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

    def reset_source(self) -> None:
        """Start a clean ingress epoch without reviving queued old corrections."""
        self.parser.reset()
        if self.queue:
            self.metrics.add("rtcm_frames_dropped_source_reset", len(self.queue))
            self.queue.clear()
        if self.fragments:
            self.metrics.add("rtcm_fragments_dropped_source_reset", len(self.fragments))
            self.fragments.clear()

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
        # Firmware diagnostics use the same single-command USB window as a
        # fragment. Poll only when no application work was pending before this
        # tick; a diagnostic must not add latency to RTCM.
        self.transport.tick(diagnostics_idle=self.idle)
        for sequence, completed in self.transport.take_tx_outcomes():
            if sequence != self.in_flight:
                self.metrics.add("radio_tx_unexpected_outcomes")
                continue
            self.in_flight = None
            if completed is True:
                self.metrics.add("rtcm_fragments_tx_completed")
            elif completed is False:
                self.fragments.clear()
                self.metrics.add("rtcm_fragments_tx_failed")
            else:
                self.fragments.clear()
                self.metrics.add("rtcm_fragments_tx_uncertain")
        if self.transport.session != self.session:
            # A modem restart invalidates modem-side state. Re-fragment only
            # source frames that remain fresh under a new app sender session.
            # Consume any explicit failed outcome first so the loss remains
            # observable rather than being mislabeled as unexpected.
            self.session, self.message_id = self.transport.session, 0
            self.fragments.clear()
            self.in_flight = None
            self.metrics.add("app_sender_session_resets")
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
    """Nonblocking broadcast with frame-aware, freshness-bounded queues."""

    MAX_CLIENTS = 16
    MAX_CLIENT_QUEUE_BYTES = 64 * application.MAX_RTCM_FRAME_SIZE
    MAX_CLIENT_QUEUE_FRAMES = 64
    CLIENT_QUEUE_TTL_S = application.RTCM_TRANSPORT_TTL_MS / 1000

    def __init__(self, bind: str, port: int, metrics: Metrics | None = None) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((bind, port))
        self.socket.listen(self.MAX_CLIENTS)
        self.socket.setblocking(False)
        self.metrics = metrics or Metrics()
        self.clients: dict[socket.socket, deque[_QueuedRtcmOutput]] = {}

    def _accept(self) -> None:
        try:
            while True:
                client, _ = self.socket.accept()
                if len(self.clients) >= self.MAX_CLIENTS:
                    client.close()
                    self.metrics.add("rtcm_tcp_clients_rejected_limit")
                    continue
                client.setblocking(False)
                self.clients[client] = deque()
                self.metrics.add("rtcm_tcp_clients_connected")
        except BlockingIOError:
            return

    def _drop(self, client: socket.socket, reason: str) -> None:
        frames = self.clients.pop(client, ())
        pending = sum(frame.remaining for frame in frames)
        try:
            client.close()
        except OSError:
            pass
        self.metrics.add(f"rtcm_tcp_client_dropped_{reason}")
        self.metrics.add("rtcm_tcp_client_bytes_abandoned", pending)

    def _queued_bytes(self, frames: deque[_QueuedRtcmOutput]) -> int:
        return sum(frame.remaining for frame in frames)

    def flush(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._accept()
        for client, frames in list(self.clients.items()):
            if not frames:
                continue
            if now - frames[0].enqueued_at >= self.CLIENT_QUEUE_TTL_S:
                self._drop(client, "stale")
                continue
            pending = frames[0]
            try:
                sent = client.send(pending.raw[pending.offset :])
            except BlockingIOError:
                continue
            except OSError:
                self._drop(client, "socket_error")
                continue
            if sent <= 0:
                self._drop(client, "closed")
            else:
                pending.offset += sent
                if pending.offset == len(pending.raw):
                    frames.popleft()
                self.metrics.add("rtcm_tcp_bytes_sent", sent)
        self.metrics.set(
            "rtcm_tcp_client_queue_bytes",
            sum(self._queued_bytes(frames) for frames in self.clients.values()),
        )

    def publish(self, raw: bytes, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        self._accept()
        delivered = 0
        for client, frames in list(self.clients.items()):
            if frames and now - frames[0].enqueued_at >= self.CLIENT_QUEUE_TTL_S:
                self._drop(client, "stale")
                continue
            if (
                len(frames) >= self.MAX_CLIENT_QUEUE_FRAMES
                or self._queued_bytes(frames) + len(raw) > self.MAX_CLIENT_QUEUE_BYTES
            ):
                self._drop(client, "slow")
                continue
            frames.append(_QueuedRtcmOutput(raw, now))
            delivered += 1
            self.metrics.add("rtcm_tcp_bytes_queued", len(raw))
        self.flush(now)
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


@dataclass
class _QueuedRtcmOutput:
    raw: bytes
    enqueued_at: float
    offset: int = 0

    @property
    def remaining(self) -> int:
        return len(self.raw) - self.offset


@dataclass(frozen=True)
class _InputChunk:
    raw: bytes
    received_at: float
    generation: int = 0


def _input_chunks(config: dict[str, object], stopped: list[bool]):
    """Yield one ingress stream session.

    Reconnection deliberately lives in :class:`_InputPump`; this helper's EOF
    is a session boundary, not a process-lifetime boundary.
    """
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


def _open_input_source(
    config: dict[str, object],
) -> tuple[object | None, Callable[[], bytes]]:
    """Open exactly one source session and return its non-buffering reader."""
    typ = config.get("type", "stdin")
    if typ == "stdin":
        return None, lambda: sys.stdin.buffer.read1(4096)
    if typ == "tcp":
        source = socket.create_connection((str(config["host"]), int(config["port"])))
        source.settimeout(0.2)
        return source, lambda: source.recv(4096)
    if typ == "serial":
        import serial

        source = serial.Serial(
            str(config["device"]), int(config.get("baudrate", 115200)), timeout=1
        )
        return source, lambda: source.read(4096)
    raise ValueError("rtcm.input.type must be stdin, tcp, or serial")


@dataclass(frozen=True)
class _InputSessionStarted:
    """Queue marker ensuring parser state cannot cross an ingress reconnect."""

    generation: int = 0


class _InputPump:
    """Reconnect blocking RTCM ingress while the modem loop stays responsive."""

    INITIAL_BACKOFF_SECONDS = 0.25
    MAX_BACKOFF_SECONDS = 5.0

    def __init__(
        self,
        config: dict[str, object],
        stopped: list[bool],
        metrics: Metrics | None = None,
        *,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self.queue: Queue[_InputChunk | _InputSessionStarted | Exception | None] = (
            Queue(maxsize=64)
        )
        self.config, self.stopped = config, stopped
        self.metrics = metrics or Metrics()
        self._sleep, self._monotonic = sleep, monotonic
        self._lock = Lock()
        self._connected = False
        self._last_data_at: float | None = None
        self._generation = 0
        self.thread = Thread(target=self._read, daemon=True)
        self.metrics.set("rtcm_source_connected", 0)
        self.metrics.set("rtcm_source_last_data_age_ms", -1)

    def _set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected
        self.metrics.set("rtcm_source_connected", int(connected))

    def tick_metrics(self, now: float | None = None) -> None:
        now = self._monotonic() if now is None else now
        with self._lock:
            last_data_at = self._last_data_at
        self.metrics.set(
            "rtcm_source_last_data_age_ms",
            -1 if last_data_at is None else max(0, int((now - last_data_at) * 1000)),
        )

    @property
    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def _next_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    @contextmanager
    def generation_guard(self):
        """Linearize all queued work and sends against a session change."""
        with self._lock:
            yield self._generation

    def _put(self, item: _InputChunk | _InputSessionStarted | Exception | None) -> bool:
        """Do not let a full hand-off queue prevent a prompt shutdown."""
        while not self.stopped[0]:
            try:
                self.queue.put(item, timeout=0.1)
                return True
            except Full:
                continue
        return False

    def _discard_queued_input(self) -> tuple[int, int]:
        dropped_chunks = 0
        dropped_bytes = 0
        while True:
            try:
                dropped = self.queue.get_nowait()
            except Empty:
                break
            if isinstance(dropped, _InputChunk):
                dropped_chunks += 1
                dropped_bytes += len(dropped.raw)
        if dropped_chunks:
            self.metrics.add("rtcm_source_handoff_chunks_dropped", dropped_chunks)
            self.metrics.add("rtcm_source_handoff_bytes_dropped", dropped_bytes)
        return dropped_chunks, dropped_bytes

    def _start_input_session(self) -> bool:
        """Put the parser boundary ahead of any pending prior-session bytes."""
        generation = self._next_generation()
        dropped_chunks, _ = self._discard_queued_input()
        if dropped_chunks:
            self.metrics.add("rtcm_source_handoff_resets")
        return self._put(_InputSessionStarted(generation))

    def _put_fresh(self, chunk: bytes, received_at: float) -> bool:
        """Prefer current bytes and force a parser boundary after overflow."""
        generation = self.current_generation
        item = _InputChunk(chunk, received_at, generation)
        try:
            self.queue.put_nowait(item)
            return True
        except Full:
            pass

        generation = self._next_generation()
        item = _InputChunk(chunk, received_at, generation)
        self._discard_queued_input()
        self.metrics.add("rtcm_source_handoff_resets")
        # The reset marker must precede retained bytes. If a consumer already
        # took an old chunk, it will still encounter this boundary before the
        # new chunk, so bytes from the two stream regions cannot be spliced.
        self.queue.put_nowait(_InputSessionStarted(generation))
        self.queue.put_nowait(item)
        return True

    def _read(self) -> None:
        typ = self.config.get("type", "stdin")
        backoff = self.INITIAL_BACKOFF_SECONDS
        had_connection = False
        while not self.stopped[0]:
            source: object | None = None
            connected = False
            try:
                source, read = _open_input_source(self.config)
                connected = True
                self._set_connected(True)
                self.metrics.add("rtcm_source_connects")
                if had_connection:
                    self.metrics.add("rtcm_source_reconnects")
                had_connection = True
                if not self._start_input_session():
                    return
                while not self.stopped[0]:
                    try:
                        chunk = read()
                    except TimeoutError:
                        continue
                    if not chunk and typ == "serial":
                        # pyserial timeout is represented by b""; it is not a
                        # device disconnect and must not churn the USB port.
                        continue
                    if not chunk:
                        break
                    if self.stopped[0]:
                        return
                    received_at = self._monotonic()
                    with self._lock:
                        self._last_data_at = received_at
                    backoff = self.INITIAL_BACKOFF_SECONDS
                    if not self._put_fresh(chunk, received_at):
                        return
                # A clean EOF is a disconnect for reconnectable sources.
            except (OSError, RuntimeError) as exc:
                self.metrics.add(
                    "rtcm_source_read_failures"
                    if connected
                    else "rtcm_source_connect_failures"
                )
                LOG.warning("RTCM source unavailable: %s", exc)
            except ValueError as exc:
                # Bad static config cannot become healthy through reconnecting.
                self._put(exc)
                return
            finally:
                if connected:
                    self.metrics.add("rtcm_source_disconnects")
                self._set_connected(False)
                if source is not None:
                    try:
                        source.close()  # type: ignore[attr-defined]
                    except OSError:
                        pass

            if typ == "stdin":
                self._put(None)
                return
            if self.stopped[0]:
                return
            self._sleep(backoff)
            backoff = min(self.MAX_BACKOFF_SECONDS, backoff * 2)
        self._put(None)

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
    pump = input_pump_factory(config.rtcm_input, stopped, metrics)
    pump.start()
    source_done = False
    active_generation = pump.current_generation
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
            # Every service step shares the producer's generation lock. A
            # reconnect therefore resets parser, queued frames, and unsent
            # fragments before any later idle iteration can send old work,
            # even if its reset marker is still waiting in the handoff queue.
            with pump.generation_guard() as current_generation:
                if current_generation != active_generation:
                    active_generation = current_generation
                    service.reset_source()
                    metrics.add("rtcm_source_parser_resets")
                if isinstance(item, _InputSessionStarted):
                    if item.generation != current_generation:
                        metrics.add("rtcm_source_handoff_markers_superseded")
                elif isinstance(item, _InputChunk):
                    if item.generation != current_generation:
                        metrics.add("rtcm_source_handoff_chunks_superseded")
                        metrics.add(
                            "rtcm_source_handoff_bytes_superseded", len(item.raw)
                        )
                    else:
                        service.ingest(item.raw, now=item.received_at)
                service.step()
            pump.tick_metrics()
            # stdin has finite input; reconnectable TCP/serial sources keep
            # their process alive across clean EOF and temporary outages.
            if (
                source_done
                and config.rtcm_input.get("type", "stdin") == "stdin"
                and service.idle
            ):
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
    server = serve_metrics(
        metrics, config.metrics_bind, config.metrics_port, role=config.role
    )
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
