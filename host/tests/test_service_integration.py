import unittest
from collections import deque

from host.mowgli_lora.config import ModemConfig, ServiceConfig
from host.mowgli_lora.metrics import Metrics
from host.mowgli_lora.service import BaseService, RobotService
from tools.lora_usb import application


class CaptureOutput:
    def __init__(self):
        self.frames = []

    def publish(self, raw):
        self.frames.append(raw)
        return 1

    def flush(self):
        return None


class SimulatedRadio:
    """Deterministic local-TX-success link with injectable air loss."""

    def __init__(self):
        self.drop_next = 0
        self.hold = False
        self.held = []

    def connect(self, left, right):
        left.peer = right
        right.peer = left

    def transmit(self, sender, sequence, payload):
        sender.outcomes.append((sequence, True))
        if self.drop_next:
            self.drop_next -= 1
            return
        if sender.peer.connected:
            if self.hold:
                self.held.append((sender.peer, payload))
            else:
                sender.peer.events.append(payload)

    def release(self, order=None):
        held, self.held = self.held, []
        indexes = range(len(held)) if order is None else order
        for index in indexes:
            peer, payload = held[index]
            if peer.connected:
                peer.events.append(payload)


class SimulatedTransport:
    def __init__(self, link, session):
        self.link = link
        self.session = session
        self.connected = True
        self.peer = None
        self.events = deque()
        self.outcomes = deque()
        self.sequence = 0
        self.waiting = False
        self.diagnostics_idle_values = []

    @property
    def ready_for_send(self):
        return self.connected and not self.waiting

    def send_air(self, payload):
        if not self.ready_for_send:
            return None
        self.sequence += 1
        self.waiting = True
        self.link.transmit(self, self.sequence, payload)
        return self.sequence

    def tick(self, *, diagnostics_idle=True):
        self.diagnostics_idle_values.append(diagnostics_idle)
        if not self.connected:
            return []
        events = list(self.events)
        self.events.clear()
        return events

    def take_tx_outcomes(self):
        outcomes = list(self.outcomes)
        self.outcomes.clear()
        if outcomes:
            self.waiting = False
        return outcomes


class ServiceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.link = SimulatedRadio()
        self.base_transport = SimulatedTransport(self.link, 100)
        self.robot_transport = SimulatedTransport(self.link, 200)
        self.link.connect(self.base_transport, self.robot_transport)
        self.base_metrics = Metrics()
        self.robot_metrics = Metrics()
        self.base = BaseService(
            ServiceConfig("base", ModemConfig("/dev/serial/by-id/test")),
            self.base_transport,
            self.base_metrics,
        )
        self.output = CaptureOutput()
        self.robot = RobotService(self.robot_transport, self.output, self.robot_metrics)

    def drain(self, now, limit=10000):
        for _ in range(limit):
            self.base.step(now)
            self.robot.step(now)
            now += 0.0005
            if self.base.idle and not self.robot_transport.events:
                return now
        self.fail("simulated service pipeline did not drain")

    def test_nominal_stream_is_byte_exact_and_ordered(self):
        expected = []
        now = 10.0
        for epoch in range(1, 61):
            frames = application.synthetic_epoch(epoch)
            expected.extend(frames)
            for raw in frames:
                self.assertEqual(1, self.base.ingest(raw, now=now))
            now = self.drain(now)
            now += 1.0
        self.assertEqual(expected, self.output.frames)
        self.assertEqual(
            len(expected), self.robot_metrics.snapshot()["rtcm_frames_delivered"]
        )
        self.assertEqual(0, len(self.base.queue))
        self.assertEqual(0, len(self.base.fragments))
        self.assertFalse(any(self.base_transport.diagnostics_idle_values[:-1]))

    def test_packet_loss_expires_then_later_frame_recovers(self):
        first = application.synthetic_rtcm(1077, 446, 1)
        later = application.synthetic_rtcm(1127, 128, 2)
        self.link.drop_next = 1
        self.base.ingest(first, now=20.0)
        now = self.drain(20.0)
        self.robot.step(now + application.REASSEMBLY_TIMEOUT_S + 0.01)
        self.base.ingest(later, now=now + 1.0)
        self.drain(now + 1.0)
        self.assertEqual([later], self.output.frames)
        self.assertEqual(1, self.robot_metrics.snapshot()["rtcm_reassembly_timeouts"])

    def test_delayed_reordered_duplicate_fragments_reassemble_once(self):
        raw = application.synthetic_rtcm(1077, 446, 22)
        self.link.hold = True
        self.base.ingest(raw, now=25.0)
        now = self.drain(25.0)
        self.assertEqual(3, len(self.link.held))
        self.assertEqual([], self.output.frames)
        self.link.release(order=(1, 0, 1, 2))
        self.robot.step(now + 0.1)
        self.assertEqual([raw], self.output.frames)
        self.assertEqual(1, self.robot.reassembler.duplicate_count)

    def test_disconnect_drops_stale_and_fresh_data_resumes(self):
        stale = application.synthetic_rtcm(1077, 96, 3)
        fresh = application.synthetic_rtcm(1087, 96, 4)
        self.base_transport.connected = False
        self.base.ingest(stale, now=30.0)
        self.base.step(now=31.0)
        self.assertEqual(1, self.base_metrics.snapshot()["rtcm_frames_dropped_stale"])
        self.base_transport.connected = True
        self.base.ingest(fresh, now=31.1)
        self.drain(31.1)
        self.assertEqual([fresh], self.output.frames)

    def test_base_session_restart_does_not_resurrect_partial_frame(self):
        partial = application.synthetic_rtcm(1077, 446, 5)
        fresh = application.synthetic_rtcm(1230, 32, 6)
        self.base.ingest(partial, now=40.0)
        self.base.step(now=40.0)
        self.robot.step(now=40.0)
        self.base_transport.session += 1
        self.base.step(now=40.1)
        self.base.ingest(fresh, now=40.2)
        self.drain(40.2)
        self.assertEqual([fresh], self.output.frames)
        self.assertEqual(1, self.base_metrics.snapshot()["app_sender_session_resets"])


if __name__ == "__main__":
    unittest.main()
