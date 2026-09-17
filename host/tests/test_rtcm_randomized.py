import random
import unittest

from host.mowgli_lora.rtcm import MAX_RTCM3_PAYLOAD, RtcmStreamParser
from tools.lora_usb.application import synthetic_rtcm


class RtcmRandomizedTest(unittest.TestCase):
    def test_deterministic_random_streams_recover_all_valid_frames(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                expected = []
                stream = bytearray()
                for index in range(25):
                    # Include hostile D3 bytes with invalid reserved header
                    # bits as well as ordinary noise. This exercises false
                    # preamble recovery without accidentally constructing a
                    # second valid RTCM frame in the random garbage itself.
                    for _ in range(rng.randrange(0, 24)):
                        if rng.randrange(8) == 0:
                            stream.extend(
                                (0xD3, 0xFC | rng.randrange(4), rng.randrange(256))
                            )
                        else:
                            value = rng.randrange(255)
                            stream.append(value if value < 0xD3 else value + 1)
                    raw = synthetic_rtcm(
                        rng.choice((1005, 1077, 1087, 1097, 1127, 1230)),
                        rng.randrange(18, 220),
                        seed * 100 + index,
                    )
                    if index % 4 == 1:
                        corrupt = bytearray(raw)
                        corrupt[-1] ^= 0x5A
                        stream.extend(corrupt)
                    else:
                        stream.extend(raw)
                        expected.append(raw)

                parser = RtcmStreamParser()
                actual = []
                offset = 0
                while offset < len(stream):
                    size = rng.randrange(1, 61)
                    actual.extend(parser.feed(stream[offset : offset + size]))
                    offset += size

                self.assertEqual(expected, [frame.raw for frame in actual])
                self.assertLessEqual(len(parser.buffer), MAX_RTCM3_PAYLOAD + 5)

    def test_source_reset_discards_partial_frame_before_new_session(self):
        parser = RtcmStreamParser()
        abandoned = synthetic_rtcm(1077, 220, 1)
        good = synthetic_rtcm(1087, 96, 2)
        self.assertEqual([], parser.feed(abandoned[:71]))
        parser.reset()
        self.assertEqual([good], [frame.raw for frame in parser.feed(good)])
        self.assertEqual(1, parser.stats["stream_resets"])

    def test_malicious_incomplete_candidate_stays_bounded_and_recovers(self):
        parser = RtcmStreamParser()
        parser.feed(b"\xd3\x03\xff" + b"x" * (MAX_RTCM3_PAYLOAD - 1))
        self.assertLessEqual(len(parser.buffer), MAX_RTCM3_PAYLOAD + 5)
        good = synthetic_rtcm(1077, 64, 99)
        output = parser.feed(b"x" * 16 + good)
        self.assertEqual([good], [frame.raw for frame in output])
        self.assertLessEqual(len(parser.buffer), MAX_RTCM3_PAYLOAD + 5)


if __name__ == "__main__":
    unittest.main()
