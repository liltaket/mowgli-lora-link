import unittest
from collections import deque
from contextlib import contextmanager

from host.mowgli_lora.config import ModemConfig, ServiceConfig
from host.mowgli_lora.metrics import Metrics
from host.mowgli_lora.service import BaseService, RobotService, TcpRtcmOutput, _run_base
from tools.lora_usb import application


class FakeTransport:
    def __init__(self):
        self.session = 42
        self.sent = []
        self.events = []
        self.connected = True
        self.ready_for_send = True
        self.outcomes = []
        self.diagnostics_idle = []

    def tick(self, *, diagnostics_idle=True):
        self.diagnostics_idle.append(diagnostics_idle)
        result, self.events = self.events, []
        return result

    def send_air(self, payload):
        self.sent.append(payload)
        self.ready_for_send = False
        return len(self.sent)

    def take_tx_outcomes(self):
        outcomes, self.outcomes = self.outcomes, []
        if outcomes:
            self.ready_for_send = True
        return outcomes


class FakeOutput:
    def __init__(self):
        self.published = []

    def publish(self, raw):
        self.published.append(raw)
        return 1

    def flush(self):
        return None


class ServiceTest(unittest.TestCase):
    def test_base_to_robot_chunks_and_stale_queue(self):
        metrics, transport = Metrics(), FakeTransport()
        config = ServiceConfig("base", ModemConfig("/dev/serial/by-id/test"))
        base = BaseService(config, transport, metrics)
        raw = application.synthetic_rtcm(1077, 446)
        self.assertEqual(1, base.ingest(raw, now=10))
        base.step(now=10.1)
        self.assertEqual(1, len(transport.sent))
        self.assertIn(application.decode(transport.sent[0]).source_age_ms, {99, 100})
        transport.outcomes = [(1, True)]
        base.step(now=10.2)
        self.assertEqual(2, len(transport.sent))
        transport.outcomes = [(2, True)]
        base.step(now=10.3)
        self.assertEqual(3, len(transport.sent))
        output = FakeOutput()
        robot = RobotService(transport, output, metrics)
        transport.events = transport.sent
        robot.step(now=10.2)
        self.assertEqual([raw], output.published)
        base.ingest(raw, now=1)
        base.step(now=2)
        self.assertEqual(1, metrics.snapshot()["rtcm_frames_dropped_stale"])

    def test_base_defers_diagnostics_while_rtcm_work_is_pending(self):
        metrics, transport = Metrics(), FakeTransport()
        base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        base.ingest(application.synthetic_rtcm(1077, 32), now=10)
        base.step(now=10.1)
        self.assertFalse(transport.diagnostics_idle[-1])
        transport.outcomes = [(1, True)]
        base.step(now=10.2)
        base.step(now=10.3)
        self.assertTrue(transport.diagnostics_idle[-1])

    def test_robot_rejects_garbage(self):
        metrics, transport, output = Metrics(), FakeTransport(), FakeOutput()
        transport.events = [b"garbage"]
        RobotService(transport, output, metrics).step(now=1)
        self.assertEqual(1, metrics.snapshot()["application_decode_failures"])

    def test_base_retains_fresh_frame_through_usb_reconnect(self):
        metrics, transport = Metrics(), FakeTransport()
        transport.connected = False
        config = ServiceConfig("base", ModemConfig("/dev/serial/by-id/test"))
        base = BaseService(config, transport, metrics)
        base.ingest(application.synthetic_rtcm(1077, 446), now=10)
        base.step(now=10.1)
        self.assertEqual(1, len(base.queue))
        self.assertEqual([], transport.sent)

    def test_base_queue_is_bounded_and_prefers_fresh_frames(self):
        metrics, transport = Metrics(), FakeTransport()
        transport.connected = False
        base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        raw = application.synthetic_rtcm(1077, 446)
        for index in range(base.MAX_QUEUED_RTCM_FRAMES + 5):
            base.ingest(raw, now=10 + index / 1000)
        self.assertEqual(base.MAX_QUEUED_RTCM_FRAMES, len(base.queue))
        self.assertEqual(5, metrics.snapshot()["rtcm_frames_dropped_queue_full"])

    def test_source_reset_drops_queued_frames_and_unsent_fragments(self):
        metrics, transport = Metrics(), FakeTransport()
        transport.ready_for_send = False
        base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        raw = application.synthetic_rtcm(1077, 446)
        base.ingest(raw + raw, now=10.0)
        base.step(now=10.1)
        self.assertTrue(base.queue)
        self.assertTrue(base.fragments)
        base.reset_source()
        self.assertFalse(base.queue)
        self.assertFalse(base.fragments)
        snapshot = metrics.snapshot()
        self.assertEqual(1, snapshot["rtcm_frames_dropped_source_reset"])
        self.assertGreater(snapshot["rtcm_fragments_dropped_source_reset"], 0)

    def test_base_exposes_one_second_input_and_lora_rates(self):
        metrics, transport = Metrics(), FakeTransport()
        base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        raw = application.synthetic_rtcm(1087, 226)
        base.ingest(raw, now=10.0)
        base.step(now=10.1)
        snapshot = metrics.snapshot()
        self.assertEqual(len(raw), snapshot["rtcm_input_bytes_per_second"])
        self.assertEqual(1, snapshot["rtcm_input_frames_per_second"])
        self.assertGreater(snapshot["lora_application_bytes_per_second"], 0)
        base.step(now=11.1)
        self.assertEqual(0, metrics.snapshot()["rtcm_input_bytes_per_second"])

    def test_multifragment_never_has_more_than_one_outstanding(self):
        metrics, transport = Metrics(), FakeTransport()
        config = ServiceConfig("base", ModemConfig("/dev/serial/by-id/test"))
        base = BaseService(config, transport, metrics)
        base.ingest(application.synthetic_rtcm(1077, 446), now=10)
        for number in range(1, 4):
            base.step(now=10 + number / 10)
            self.assertEqual(number, len(transport.sent))
            self.assertEqual(number, base.in_flight)
            base.step(now=10 + number / 10 + 0.01)
            self.assertEqual(number, len(transport.sent))
            transport.outcomes = [(number, True)]
        base.step(now=10.5)
        self.assertIsNone(base.in_flight)
        self.assertEqual(3, metrics.snapshot()["rtcm_fragments_tx_completed"])

    def test_tx_failure_is_not_counted_as_completed(self):
        metrics, transport = Metrics(), FakeTransport()
        base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        base.ingest(application.synthetic_rtcm(1077, 446), now=10)
        base.step(now=10.1)
        transport.outcomes = [(1, False)]
        base.step(now=10.2)
        snapshot = metrics.snapshot()
        self.assertEqual(1, snapshot["rtcm_fragments_tx_failed"])
        self.assertNotIn("rtcm_fragments_tx_completed", snapshot)

    def test_session_rotation_preserves_explicit_in_flight_failure_metric(self):
        metrics, transport = Metrics(), FakeTransport()
        base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        base.ingest(application.synthetic_rtcm(1077, 446), now=10)
        base.step(now=10.1)
        self.assertEqual(1, base.in_flight)

        transport.outcomes = [(1, False)]
        transport.session = 43
        base.step(now=10.2)

        snapshot = metrics.snapshot()
        self.assertEqual(1, snapshot["rtcm_fragments_tx_failed"])
        self.assertNotIn("radio_tx_unexpected_outcomes", snapshot)
        self.assertEqual(43, base.session)
        self.assertIsNone(base.in_flight)
        self.assertFalse(base.fragments)

    def test_uncertain_tx_aborts_frame_without_claiming_radio_failure(self):
        metrics, transport = Metrics(), FakeTransport()
        base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        base.ingest(application.synthetic_rtcm(1077, 446), now=10)
        base.step(now=10.1)
        transport.outcomes = [(1, None)]

        base.step(now=10.2)

        snapshot = metrics.snapshot()
        self.assertEqual(1, snapshot["rtcm_fragments_tx_uncertain"])
        self.assertNotIn("rtcm_fragments_tx_failed", snapshot)
        self.assertIsNone(base.in_flight)
        self.assertFalse(base.fragments)

    def test_unsent_fragment_aborts_when_source_becomes_stale(self):
        metrics, transport = Metrics(), FakeTransport()
        transport.ready_for_send = False
        base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            transport,
            metrics,
        )
        base.ingest(application.synthetic_rtcm(1077, 446), now=10)
        base.step(now=10.1)
        self.assertTrue(base.fragments)
        transport.ready_for_send = True
        base.step(now=11.0)
        self.assertFalse(base.fragments)
        self.assertEqual(1, metrics.snapshot()["rtcm_fragments_dropped_stale"])

    def test_tcp_output_queues_partial_writes_byte_exactly(self):
        class PartialClient:
            def __init__(self):
                self.output = bytearray()
                self.blocked = True

            def send(self, raw):
                if self.blocked:
                    self.blocked = False
                    raise BlockingIOError()
                part = raw[:2]
                self.output.extend(part)
                return len(part)

            def close(self):
                return None

        metrics = Metrics()
        output = TcpRtcmOutput("127.0.0.1", 0, metrics)
        client = PartialClient()
        output.clients[client] = deque()
        try:
            self.assertEqual(1, output.publish(b"abcdef", now=10.0))
            self.assertEqual(b"", client.output)
            for _ in range(3):
                output.flush(now=10.1)
            self.assertEqual(b"abcdef", client.output)
            self.assertEqual(0, metrics.snapshot()["rtcm_tcp_client_queue_bytes"])
        finally:
            output.close()

    def test_tcp_output_drops_stale_client_without_reordering_frames(self):
        class BlockedClient:
            def __init__(self):
                self.closed = False

            def send(self, _raw):
                raise BlockingIOError()

            def close(self):
                self.closed = True

        metrics = Metrics()
        output = TcpRtcmOutput("127.0.0.1", 0, metrics)
        client = BlockedClient()
        output.clients[client] = deque()
        try:
            output.publish(b"first", now=10.0)
            output.publish(b"second", now=10.5)
            self.assertEqual(
                [b"first", b"second"],
                [frame.raw for frame in output.clients[client]],
            )
            output.flush(now=11.0)
            self.assertTrue(client.closed)
            self.assertNotIn(client, output.clients)
            self.assertEqual(1, metrics.snapshot()["rtcm_tcp_client_dropped_stale"])
            self.assertEqual(
                len(b"firstsecond"),
                metrics.snapshot()["rtcm_tcp_client_bytes_abandoned"],
            )
        finally:
            output.close()

    def test_tcp_output_rejects_clients_above_global_limit(self):
        class Client:
            def __init__(self):
                self.closed = False

            def setblocking(self, _blocking):
                return None

            def close(self):
                self.closed = True

        class Listener:
            def __init__(self, clients):
                self.clients = deque(clients)

            def accept(self):
                if not self.clients:
                    raise BlockingIOError()
                return self.clients.popleft(), ("127.0.0.1", 1)

            def close(self):
                return None

        metrics = Metrics()
        output = TcpRtcmOutput("127.0.0.1", 0, metrics)
        output.socket.close()
        clients = [Client() for _ in range(output.MAX_CLIENTS + 2)]
        output.socket = Listener(clients)
        try:
            output._accept()
            self.assertEqual(output.MAX_CLIENTS, len(output.clients))
            self.assertTrue(clients[-1].closed)
            self.assertTrue(clients[-2].closed)
            self.assertEqual(2, metrics.snapshot()["rtcm_tcp_clients_rejected_limit"])
        finally:
            output.close()

    def test_idle_ingress_still_ticks_modem(self):
        stopped = [False]

        class IdleTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.ticks = 0
                self.closed = False

            def tick(self, *, diagnostics_idle=True):
                self.ticks += 1
                if self.ticks == 3:
                    stopped[0] = True
                return super().tick()

            def close(self):
                self.closed = True

        class IdlePump:
            current_generation = 0

            def __init__(self, *_args):
                from queue import Queue

                self.queue = Queue()

            def start(self):
                return None

            def tick_metrics(self):
                return None

            @contextmanager
            def generation_guard(self):
                yield self.current_generation

        transport = IdleTransport()
        config = ServiceConfig("base", ModemConfig("/dev/serial/by-id/test"))
        _run_base(
            config,
            Metrics(),
            stopped,
            transport_factory=lambda *_args: transport,
            input_pump_factory=IdlePump,
        )
        self.assertGreaterEqual(transport.ticks, 3)
        self.assertTrue(transport.closed)
