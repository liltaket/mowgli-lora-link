import unittest

from host.mowgli_lora.rtcm import RtcmStreamParser
from tools.lora_usb.application import synthetic_rtcm


class RtcmParserTest(unittest.TestCase):
    def test_chunked_garbage_and_recovery(self):
        good = synthetic_rtcm(1077, 446)
        parser = RtcmStreamParser()
        self.assertEqual([], parser.feed(b"junk\xd3\xff"))
        output = []
        for index in range(0, len(good), 17):
            output.extend(parser.feed(good[index : index + 17], received_at=4.0))
        self.assertEqual([good], [item.raw for item in output])
        self.assertEqual(1077, output[0].message_type)

    def test_crc_failure_recovers_next_frame(self):
        bad = bytearray(synthetic_rtcm(1077, 32))
        bad[-1] ^= 1
        good = synthetic_rtcm(1127, 48)
        parser = RtcmStreamParser()
        output = parser.feed(bytes(bad) + good)
        self.assertEqual([good], [item.raw for item in output])
        self.assertEqual(1, parser.stats["crc_failures"])

    def test_load(self):
        parser = RtcmStreamParser()
        raw = b"".join(synthetic_rtcm(1077, 446, seed) for seed in range(100))
        output = parser.feed(raw)
        self.assertEqual(100, len(output))
        self.assertEqual(100, parser.message_types[1077])
