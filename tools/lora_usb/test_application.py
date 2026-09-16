import struct
import unittest
from dataclasses import replace

from .application import *


class ApplicationFrameTests(unittest.TestCase):
    def test_crc_and_frame_round_trip(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)
        frame = Frame(TELEMETRY, 1, 2, 3, b"payload")
        self.assertEqual(decode(encode(frame)), frame)

    def test_frame_rejects_header_length_crc_and_ranges(self):
        good = encode(Frame(TELEMETRY, 1, 2, 3, b"payload"))
        bad_length = bytearray(good)
        bad_length[17] += 1
        cases = (
            good[:-1],
            b"XX" + good[2:],
            good[:2] + b"\x02" + good[3:],
            good[:4] + b"\x01" + good[5:],
            bytes(bad_length),
            good[:-1] + bytes((good[-1] ^ 1,)),
        )
        for raw in cases:
            with self.assertRaises(ApplicationError):
                decode(raw)
        for frame in (
            Frame(TELEMETRY, 0, 1, 0, b""),
            Frame(TELEMETRY, 1, 0, 0, b""),
            Frame(TELEMETRY, 1, 1, 65536, b""),
            Frame(TELEMETRY, 1, 1, 0, bytes(181)),
        ):
            with self.assertRaises(ApplicationError):
                encode(frame)

    def test_body_codecs(self):
        self.assertEqual(
            decode_hello(encode_hello(ROLE_BASE, 0x1234)), (ROLE_BASE, 0x1234)
        )
        stop = StopRequestBody(99, STOP_COMMAND, 7)
        self.assertEqual(decode_stop(encode_stop(stop)), stop)
        ack = CommandAckBody(1, 2, ACK_APPLIED, STATE_STOPPED)
        self.assertEqual(decode_ack(encode_ack(ack)), ack)
        sample = TelemetrySample(
            123, -591234567, UNKNOWN_I32, UNKNOWN_U16, 25123, 456, 3, 4, 27
        )
        body = encode_telemetry(sample)
        self.assertEqual(len(body), 24)
        self.assertEqual(decode_telemetry(body), sample)
        with self.assertRaises(ApplicationError):
            decode_telemetry(body[:-2] + b"\0\1")


class StopCacheTests(unittest.TestCase):
    def make_stop(self, message_id=1, target=99, age=0, command=STOP_COMMAND, reason=0):
        return Frame(
            STOP_REQUEST,
            7,
            message_id,
            age,
            encode_stop(StopRequestBody(target, command, reason)),
        )

    def test_apply_duplicate_and_already_stopped(self):
        cache = StopCache(99)
        first = cache.apply(self.make_stop())
        duplicate = cache.apply(replace(self.make_stop(), source_age_ms=500))
        second = cache.apply(self.make_stop(message_id=2))
        self.assertEqual((first.result, first.applied_now), (ACK_APPLIED, True))
        self.assertEqual(
            (duplicate.result, duplicate.applied_now), (ACK_APPLIED, False)
        )
        self.assertEqual(second.result, ACK_ALREADY_STOPPED)
        self.assertEqual(cache.apply_count, 1)

    def test_result_cases_and_bounded_cache(self):
        cache = StopCache(99)
        cache.apply(self.make_stop(message_id=10))
        self.assertEqual(
            cache.apply(self.make_stop(message_id=10, reason=1)).result, ACK_ID_CONFLICT
        )
        self.assertEqual(cache.apply(self.make_stop(message_id=9)).result, ACK_STALE_ID)
        self.assertEqual(
            cache.apply(self.make_stop(message_id=11, target=100)).result,
            ACK_WRONG_TARGET_SESSION,
        )
        self.assertEqual(
            cache.apply(self.make_stop(message_id=12, age=2001)).result, ACK_EXPIRED
        )
        self.assertEqual(
            cache.apply(self.make_stop(message_id=13, command=2)).result,
            ACK_UNSUPPORTED_COMMAND,
        )
        cache = StopCache(99)
        for message_id in range(1, 70):
            cache.reset_mock_state()
            cache.apply(self.make_stop(message_id=message_id))
        self.assertEqual(len(cache.cache), 64)
        self.assertEqual(cache.apply(self.make_stop(message_id=1)).result, ACK_STALE_ID)


class RtcmTests(unittest.TestCase):
    def test_synthetic_profile_sizes_types_and_crc(self):
        epoch = synthetic_epoch(10)
        self.assertEqual(
            [len(frame) for frame in epoch], list(SYNTHETIC_RTCM_SIZES.values())
        )
        self.assertEqual(
            [rtcm_type(frame) for frame in epoch], list(SYNTHETIC_RTCM_SIZES)
        )
        self.assertTrue(all(valid_rtcm3(frame) for frame in epoch))
        corrupt = epoch[0][:-1] + bytes((epoch[0][-1] ^ 1,))
        self.assertFalse(valid_rtcm3(corrupt))

    def test_reassembly_reorder_duplicate_timeout_conflict_recovery(self):
        raw = synthetic_rtcm(1077, 446, 1)
        pieces = fragment(raw, 1, 2)
        reassembler = Reassembler()
        self.assertIsNone(reassembler.add(pieces[2], 0.0))
        self.assertIsNone(reassembler.add(pieces[0], 0.1))
        self.assertIsNone(reassembler.add(pieces[0], 0.2))
        self.assertEqual(reassembler.add(pieces[1], 0.3), raw)
        self.assertEqual(reassembler.duplicate_count, 1)
        reassembler = Reassembler()
        reassembler.add(pieces[0], 0.0)
        self.assertEqual(reassembler.expire(0.6), 1)
        with self.assertRaises(ApplicationError):
            reassembler.add(pieces[1], 0.7)
        conflict = Reassembler()
        conflict.add(pieces[0], 0.0)
        changed = bytearray(pieces[0].body)
        changed[-1] ^= 1
        with self.assertRaises(ApplicationError):
            conflict.add(replace(pieces[0], body=bytes(changed)), 0.1)
        next_raw = synthetic_rtcm(1087, 226, 2)
        result = None
        for piece in fragment(next_raw, 1, 3):
            result = conflict.add(piece, 0.2)
        self.assertEqual(result, next_raw)

    def test_shape_age_type_and_capacity(self):
        raw = synthetic_rtcm(1127, 586, 1)
        pieces = fragment(raw, 1, 20)
        with self.assertRaises(ApplicationError):
            Reassembler().add(replace(pieces[0], source_age_ms=1000), 0.0)
        malformed = bytearray(pieces[0].body)
        malformed[5] = 0
        with self.assertRaises(ApplicationError):
            Reassembler().add(replace(pieces[0], body=bytes(malformed)), 0.0)
        reassembler = Reassembler()
        for message_id in range(1, MAX_REASSEMBLIES + 2):
            reassembler.add(fragment(raw, 1, message_id)[0], message_id / 100)
        self.assertEqual(
            (len(reassembler.items), reassembler.eviction_count), (MAX_REASSEMBLIES, 1)
        )
        wrong_type = bytearray(pieces[-1].body)
        struct.pack_into(">H", wrong_type, 0, 1077)
        checker = Reassembler()
        for piece in pieces[:-1]:
            checker.add(piece, 0.0)
        with self.assertRaises(ApplicationError):
            checker.add(replace(pieces[-1], body=bytes(wrong_type)), 0.1)


if __name__ == "__main__":
    unittest.main()
