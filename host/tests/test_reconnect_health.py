import json
import time
import unittest
from contextlib import contextmanager
from queue import Empty, Queue
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from host.mowgli_lora.config import ModemConfig, ServiceConfig
from host.mowgli_lora.metrics import Metrics, health_for_role, serve_metrics
from host.mowgli_lora.rtcm import RtcmStreamParser
from host.mowgli_lora.service import (
    BaseService,
    _InputChunk,
    _InputPump,
    _InputSessionStarted,
    _run_base,
)
from tools.lora_usb import application


class _NoopTransport:
    session = 1
    connected = False
    ready_for_send = False

    def tick(self, **_kwargs):
        return []

    def take_tx_outcomes(self):
        return []


class ReconnectAndHealthTest(unittest.TestCase):
    def test_reconnect_resets_parser_and_tracks_source_state(self):
        raw = application.synthetic_rtcm(1077, 32)
        stopped = [False]
        attempts = []

        class Source:
            def close(self):
                return None

        def open_source(_config):
            attempt = len(attempts)
            attempts.append(attempt)
            if attempt == 0:
                raise OSError("refused")
            chunks = [raw[:4], b""] if attempt == 1 else [raw, b""]

            def read():
                result = chunks.pop(0)
                if not chunks and attempt == 2:
                    stopped[0] = True
                return result

            return Source(), read

        metrics = Metrics()
        delays = []
        pump = _InputPump(
            {"type": "tcp", "host": "localhost", "port": 1},
            stopped,
            metrics,
            sleep=delays.append,
            monotonic=lambda: 10.0,
        )
        with patch("host.mowgli_lora.service._open_input_source", open_source):
            pump._read()

        items = []
        while True:
            try:
                items.append(pump.queue.get_nowait())
            except Empty:
                break
        self.assertEqual([0.25, 0.25], delays)
        self.assertEqual(
            1, sum(isinstance(item, _InputSessionStarted) for item in items)
        )
        self.assertEqual(1, metrics.snapshot()["rtcm_source_connect_failures"])
        self.assertEqual(2, metrics.snapshot()["rtcm_source_connects"])
        self.assertEqual(2, metrics.snapshot()["rtcm_source_disconnects"])
        self.assertEqual(1, metrics.snapshot()["rtcm_source_reconnects"])
        self.assertEqual(1, metrics.snapshot()["rtcm_source_handoff_chunks_dropped"])
        self.assertEqual(0, metrics.snapshot()["rtcm_source_connected"])

        service = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            _NoopTransport(),
            metrics,
        )
        service.ingest(raw[:4], now=10)
        service.parser.reset()
        self.assertEqual(1, service.ingest(raw, now=11))

    def test_source_last_data_age_advances_deterministically(self):
        metrics = Metrics()
        pump = _InputPump({}, [False], metrics, monotonic=lambda: 4.0)
        pump._last_data_at = 1.5
        pump.tick_metrics(now=3.0)
        self.assertEqual(1500, metrics.snapshot()["rtcm_source_last_data_age_ms"])

    def test_serial_timeout_is_not_treated_as_a_disconnect(self):
        raw = application.synthetic_rtcm(1077, 32)
        stopped = [False]
        chunks = [b"", raw, b""]

        class Source:
            def close(self):
                return None

        def open_source(_config):
            def read():
                chunk = chunks.pop(0)
                if not chunks:
                    stopped[0] = True
                return chunk

            return Source(), read

        metrics = Metrics()
        pump = _InputPump(
            {"type": "serial", "device": "/dev/serial/by-id/test"},
            stopped,
            metrics,
            sleep=lambda _delay: self.fail("serial timeout must not back off"),
        )
        with patch("host.mowgli_lora.service._open_input_source", open_source):
            pump._read()
        items = []
        while not pump.queue.empty():
            items.append(pump.queue.get_nowait())
        self.assertEqual(
            1, sum(isinstance(item, _InputSessionStarted) for item in items)
        )
        self.assertIn(
            raw, [item.raw for item in items if isinstance(item, _InputChunk)]
        )
        self.assertEqual(1, metrics.snapshot()["rtcm_source_connects"])
        self.assertEqual(1, metrics.snapshot()["rtcm_source_disconnects"])

    def test_handoff_preserves_read_timestamp_so_delayed_data_is_stale(self):
        metrics = Metrics()
        pump = _InputPump({}, [False], metrics)
        raw = application.synthetic_rtcm(1077, 32)
        self.assertTrue(pump._put_fresh(raw, received_at=10.0))
        item = pump.queue.get_nowait()
        self.assertIsInstance(item, _InputChunk)

        transport = _NoopTransport()
        service = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        service.ingest(item.raw, now=item.received_at)
        service.step(now=11.0)
        self.assertFalse(service.queue)
        self.assertEqual(1, metrics.snapshot()["rtcm_frames_dropped_stale"])

    def test_full_handoff_prefers_latest_and_inserts_parser_reset(self):
        metrics = Metrics()
        pump = _InputPump({}, [False], metrics)
        abandoned = application.synthetic_rtcm(1077, 220, 1)
        latest = application.synthetic_rtcm(1087, 96, 2)
        pump.queue.put_nowait(_InputSessionStarted())
        for index in range(pump.queue.maxsize - 1):
            pump.queue.put_nowait(_InputChunk(abandoned[index : index + 1], 1.0))

        self.assertTrue(pump._put_fresh(latest, received_at=2.0))
        retained = []
        while not pump.queue.empty():
            retained.append(pump.queue.get_nowait())
        self.assertEqual(2, len(retained))
        self.assertIsInstance(retained[0], _InputSessionStarted)
        self.assertEqual(latest, retained[1].raw)

        parser = RtcmStreamParser()
        parser.feed(abandoned[:50])
        parser.reset()
        self.assertEqual(
            [latest], [frame.raw for frame in parser.feed(retained[1].raw)]
        )
        snapshot = metrics.snapshot()
        self.assertEqual(
            pump.queue.maxsize - 1,
            snapshot["rtcm_source_handoff_chunks_dropped"],
        )
        self.assertEqual(1, snapshot["rtcm_source_handoff_resets"])

    def test_reconnect_session_marker_discards_full_prior_session_queue(self):
        metrics = Metrics()
        pump = _InputPump({}, [False], metrics)
        old = application.synthetic_rtcm(1077, 32)
        for index in range(pump.queue.maxsize):
            pump.queue.put_nowait(_InputChunk(old[index : index + 1], 1.0))

        self.assertTrue(pump._start_input_session())
        retained = pump.queue.get_nowait()
        self.assertIsInstance(retained, _InputSessionStarted)
        self.assertTrue(pump.queue.empty())
        snapshot = metrics.snapshot()
        self.assertEqual(
            pump.queue.maxsize, snapshot["rtcm_source_handoff_chunks_dropped"]
        )
        self.assertEqual(1, snapshot["rtcm_source_handoff_resets"])

    def test_chunk_taken_by_consumer_is_superseded_by_reconnect_generation(self):
        pump = _InputPump({}, [False], Metrics())
        old = _InputChunk(b"old", 1.0, pump.current_generation)
        pump.queue.put_nowait(old)
        consumer_held = pump.queue.get_nowait()

        self.assertTrue(pump._start_input_session())
        marker = pump.queue.get_nowait()
        self.assertIsInstance(marker, _InputSessionStarted)
        self.assertNotEqual(consumer_held.generation, pump.current_generation)
        self.assertEqual(marker.generation, pump.current_generation)

    def test_reconnect_cannot_send_queued_complete_old_generation_frame(self):
        raw = application.synthetic_rtcm(1077, 32)
        metrics = Metrics()

        class Pump:
            current_generation = 2

            def __init__(self):
                self.queue = Queue()
                self.queue.put(_InputChunk(raw, 10.0, generation=1))
                self.queue.put(_InputSessionStarted(generation=2))
                self.queue.put(None)

            def start(self):
                return None

            def tick_metrics(self):
                return None

            @contextmanager
            def generation_guard(self):
                yield self.current_generation

        class Transport(_NoopTransport):
            session = 1
            connected = True
            ready_for_send = True

            def __init__(self):
                self.sent = []

            def send_air(self, payload):
                self.sent.append(payload)
                return len(self.sent)

            def close(self):
                return None

        transport = Transport()
        _run_base(
            ServiceConfig(
                "base",
                ModemConfig("/dev/serial/by-id/test"),
                rtcm_input={"type": "stdin"},
            ),
            metrics,
            [False],
            transport_factory=lambda *_args: transport,
            input_pump_factory=lambda *_args: Pump(),
        )
        self.assertFalse(transport.sent)
        self.assertEqual(1, metrics.snapshot()["rtcm_source_handoff_chunks_superseded"])

    def test_generation_guard_prevents_check_then_reconnect_send_race(self):
        raw = application.synthetic_rtcm(1077, 32)
        metrics = Metrics()
        pump = _InputPump({}, [False], metrics)
        old_generation = pump.current_generation
        pump.queue.put(_InputChunk(raw, 10.0, generation=old_generation))
        # Model the reconnect winning immediately after dequeue but before the
        # consumer can enter its atomic validation/send section.
        pump._next_generation()
        pump.queue.put(None)
        pump.start = lambda: None

        class Transport(_NoopTransport):
            session = 1
            connected = True
            ready_for_send = True

            def __init__(self):
                self.sent = []

            def send_air(self, payload):
                self.sent.append(payload)
                return len(self.sent)

            def close(self):
                return None

        transport = Transport()
        _run_base(
            ServiceConfig(
                "base",
                ModemConfig("/dev/serial/by-id/test"),
                rtcm_input={"type": "stdin"},
            ),
            metrics,
            [False],
            transport_factory=lambda *_args: transport,
            input_pump_factory=lambda *_args: pump,
        )

        self.assertFalse(transport.sent)
        self.assertEqual(1, metrics.snapshot()["rtcm_source_handoff_chunks_superseded"])

    def test_generation_advance_clears_backlog_before_next_idle_send(self):
        raw = application.synthetic_rtcm(1077, 32) * 2
        metrics = Metrics()
        pump = _InputPump({}, [False], metrics)
        pump.start = lambda: None

        class AdvancingQueue:
            def __init__(self):
                self.calls = 0

            def get(self, timeout):
                self.calls += 1
                if self.calls == 1:
                    return _InputChunk(raw, time.monotonic(), pump.current_generation)
                if self.calls == 2:
                    pump._next_generation()
                    raise Empty
                return None

        pump.queue = AdvancingQueue()

        class Transport(_NoopTransport):
            session = 1
            connected = True
            ready_for_send = True

            def __init__(self):
                self.sent = []
                self.outcomes = []

            def send_air(self, payload):
                sequence = len(self.sent) + 1
                self.sent.append(payload)
                self.outcomes.append((sequence, True))
                return sequence

            def take_tx_outcomes(self):
                outcomes, self.outcomes = self.outcomes, []
                return outcomes

            def close(self):
                return None

        transport = Transport()
        _run_base(
            ServiceConfig(
                "base",
                ModemConfig("/dev/serial/by-id/test"),
                rtcm_input={"type": "stdin"},
            ),
            metrics,
            [False],
            transport_factory=lambda *_args: transport,
            input_pump_factory=lambda *_args: pump,
        )

        self.assertEqual(1, len(transport.sent))
        self.assertEqual(1, metrics.snapshot()["rtcm_frames_dropped_source_reset"])

    def test_no_data_eof_uses_exponential_backoff(self):
        stopped = [False]
        delays = []

        class Source:
            def close(self):
                return None

        def sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                stopped[0] = True

        pump = _InputPump(
            {"type": "tcp", "host": "localhost", "port": 1},
            stopped,
            Metrics(),
            sleep=sleep,
        )
        with patch(
            "host.mowgli_lora.service._open_input_source",
            return_value=(Source(), lambda: b""),
        ):
            pump._read()
        self.assertEqual([0.25, 0.5, 1.0], delays)

    def test_read_error_reconnects_and_uses_read_failure_counter(self):
        raw = application.synthetic_rtcm(1077, 32)
        stopped = [False]
        attempts = []

        class Source:
            def close(self):
                return None

        def open_source(_config):
            attempt = len(attempts)
            attempts.append(attempt)
            if attempt == 0:
                return Source(), lambda: (_ for _ in ()).throw(OSError("reset"))

            chunks = [raw]

            def read():
                stopped[0] = True
                return chunks.pop()

            return Source(), read

        metrics = Metrics()
        pump = _InputPump(
            {"type": "tcp", "host": "localhost", "port": 1},
            stopped,
            metrics,
            sleep=lambda _delay: None,
        )
        with patch("host.mowgli_lora.service._open_input_source", open_source):
            pump._read()
        snapshot = metrics.snapshot()
        self.assertEqual(1, snapshot["rtcm_source_read_failures"])
        self.assertNotIn("rtcm_source_connect_failures", snapshot)
        self.assertEqual(1, snapshot["rtcm_source_reconnects"])

    def test_role_aware_health_requires_base_source(self):
        metrics = Metrics()
        metrics.set("modem_connected", 1)
        metrics.set("modem_handshake_complete", 1)
        metrics.set("modem_radio_ready", 1)
        self.assertFalse(health_for_role(metrics, "base")[0])
        self.assertTrue(health_for_role(metrics, "robot")[0])
        metrics.set("rtcm_source_connected", 1)
        self.assertTrue(health_for_role(metrics, "base")[0])

    def test_healthz_uses_503_for_degraded_base(self):
        metrics = Metrics()
        server = serve_metrics(metrics, "127.0.0.1", 0, role="base")
        try:
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/healthz"
            with self.assertRaises(HTTPError) as raised:
                urlopen(endpoint)
            self.assertEqual(503, raised.exception.code)
            self.assertEqual("degraded", json.loads(raised.exception.read())["status"])
            metrics.set("modem_connected", 1)
            metrics.set("modem_handshake_complete", 1)
            metrics.set("modem_radio_ready", 1)
            metrics.set("rtcm_source_connected", 1)
            with urlopen(endpoint) as response:
                self.assertEqual(200, response.status)
                self.assertEqual("ok", json.loads(response.read())["status"])
        finally:
            server.shutdown()
            server.server_close()
