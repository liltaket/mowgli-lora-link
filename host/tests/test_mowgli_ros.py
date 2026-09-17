import unittest

from host.mowgli_lora.mowgli_ros import RtcmTcpReader, populate_rtcm_message
from host.mowgli_lora.rtcm import RtcmFrame


class DummyMessage:
    def __init__(self):
        self.stamp = None
        self.message_type = 0
        self.data = []


class MowgliRosAdapterTest(unittest.TestCase):
    def test_populates_universal_gnss_shape_byte_exactly(self):
        frame = RtcmFrame(b"\xd3\0\0abc", 1077, 12.0)
        message = populate_rtcm_message(DummyMessage(), frame, "stamp")
        self.assertEqual("stamp", message.stamp)
        self.assertEqual(1077, message.message_type)
        self.assertEqual(list(frame.raw), message.data)

    def test_bounded_queue_discards_oldest_frame(self):
        reader = RtcmTcpReader("127.0.0.1", 2233, queue_size=2)
        frames = [RtcmFrame(bytes((index,)), 1000 + index, 1.0) for index in range(3)]
        for frame in frames:
            reader._put_fresh(frame)
        self.assertEqual(frames[1:], reader.take())
        self.assertEqual(1, reader.frames_dropped)


if __name__ == "__main__":
    unittest.main()
