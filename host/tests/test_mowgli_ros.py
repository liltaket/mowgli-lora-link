import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from host.mowgli_lora.mowgli_ros import (
    DEFAULT_TOPIC,
    RtcmTcpReader,
    main,
    populate_rtcm_message,
)
from host.mowgli_lora.rtcm import RtcmFrame


class DummyMessage:
    def __init__(self):
        self.stamp = None
        self.message_type = 0
        self.data = []


class MowgliRosAdapterTest(unittest.TestCase):
    def test_current_message_package_and_default_receiver_topic(self):
        published = []
        reader = SimpleNamespace(
            start=lambda: None, close=lambda: None, frames_received=0, frames_dropped=0
        )
        qos = SimpleNamespace(
            HistoryPolicy=SimpleNamespace(KEEP_LAST=1),
            ReliabilityPolicy=SimpleNamespace(RELIABLE=1),
            QoSProfile=lambda **values: SimpleNamespace(**values),
        )

        class Node:
            def __init__(self, _name):
                pass

            def create_publisher(self, message, topic, profile):
                published.append((message, topic, profile))
                return SimpleNamespace(publish=lambda _message: None)

            def create_timer(self, *_args):
                pass

            def get_logger(self):
                return SimpleNamespace(info=lambda _message: None)

            def destroy_node(self):
                pass

        rclpy = ModuleType("rclpy")
        rclpy.init = lambda **_kwargs: None
        rclpy.spin = lambda _node: None
        rclpy.shutdown = lambda: None
        modules = {
            "rclpy": rclpy,
            "rclpy.node": SimpleNamespace(Node=Node),
            "rclpy.qos": qos,
            "universal_gnss_msgs": ModuleType("universal_gnss_msgs"),
            "universal_gnss_msgs.msg": SimpleNamespace(RtcmFrame=DummyMessage),
        }
        with (
            patch.dict(sys.modules, modules),
            patch.object(sys, "argv", ["adapter"]),
            patch("host.mowgli_lora.mowgli_ros.RtcmTcpReader", return_value=reader),
        ):
            main()
        self.assertEqual("/universal_gnss_receiver/rtcm", DEFAULT_TOPIC)
        self.assertEqual(DummyMessage, published[0][0])
        self.assertEqual(DEFAULT_TOPIC, published[0][1])
        self.assertEqual(50, published[0][2].depth)

    def test_populates_universal_gnss_shape_byte_exactly(self):
        frame = RtcmFrame(b"\xd3\0\0abc", 1077, 12.0)
        message = populate_rtcm_message(DummyMessage(), frame, "stamp")
        self.assertEqual("stamp", message.stamp)
        self.assertEqual(1077, message.message_type)
        self.assertEqual(list(frame.raw), message.data)

    def test_bounded_queue_discards_oldest_frame(self):
        reader = RtcmTcpReader("127.0.0.1", 2233, queue_size=2, monotonic=lambda: 1.5)
        frames = [RtcmFrame(bytes((index,)), 1000 + index, 1.0) for index in range(3)]
        for frame in frames:
            reader._put_fresh(frame)
        self.assertEqual(frames[1:], reader.take())
        self.assertEqual(1, reader.frames_dropped)

    def test_take_never_returns_frame_past_application_ttl(self):
        reader = RtcmTcpReader("127.0.0.1", 2233, monotonic=lambda: 10.0)
        expired = RtcmFrame(b"old", 1077, 9.0)
        fresh = RtcmFrame(b"fresh", 1087, 9.001)
        reader._put_fresh(expired)
        reader._put_fresh(fresh)
        self.assertEqual([fresh], reader.take())
        self.assertEqual(1, reader.frames_dropped)

    def test_source_boundary_drops_complete_queued_old_frame(self):
        reader = RtcmTcpReader("127.0.0.1", 2233)
        old = RtcmFrame(b"complete-old-frame", 1077, 99.0)
        reader._put_fresh(old)

        class ClosedSource:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                return None

            def recv(self, _size):
                reader.stop_event.set()
                return b""

        with patch(
            "host.mowgli_lora.mowgli_ros.socket.create_connection",
            return_value=ClosedSource(),
        ):
            reader._run()
        self.assertEqual([], reader.take(now=99.1))
        self.assertEqual(1, reader.frames_dropped)

    def test_eof_drops_fresh_session_frames_before_reconnect_wait(self):
        from tools.lora_usb import application

        raw = application.synthetic_rtcm(1077, 32)
        reader = RtcmTcpReader("127.0.0.1", 2233)
        chunks = [raw, b""]
        observed_during_wait = []

        class Source:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                return None

            def recv(self, _size):
                return chunks.pop(0)

        def reconnect_wait(_delay):
            observed_during_wait.extend(reader.take(now=0.0))
            reader.stop_event.set()
            return True

        with (
            patch(
                "host.mowgli_lora.mowgli_ros.socket.create_connection",
                return_value=Source(),
            ),
            patch.object(reader.stop_event, "wait", side_effect=reconnect_wait),
        ):
            reader._run()

        self.assertEqual([], observed_during_wait)
        self.assertEqual(1, reader.frames_dropped)


if __name__ == "__main__":
    unittest.main()
