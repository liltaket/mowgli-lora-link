import struct
import sys
import types
import unittest
from unittest.mock import patch

from .client import ModemClient
from .protocol import *


class ProtocolTests(unittest.TestCase):
    def test_crc_golden(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)

    def test_cobs_zero_and_long(self):
        for value in (b"\0\x01\0", bytes(range(1, 256)) * 2):
            self.assertEqual(cobs_decode(cobs_encode(value)), value)

    def test_frame_roundtrip_and_negative_metrics(self):
        raw = encode_frame(RADIO_RX, 4, 8, b"x")
        self.assertEqual(decode_frame(raw[:-1]), (RADIO_RX, 4, 8, b"x"))
        self.assertEqual(
            decode_frame(
                encode_frame(ERROR, 1, 2, struct.pack(">HhB", 1, -12, 2))[:-1]
            )[3][2:4],
            b"\xff\xf4",
        )

    def test_corrupt_truncated_overlong_recovery(self):
        good = encode_frame(HELLO, 1, 1)
        decoder = StreamDecoder()
        frames = []
        frames += decoder.feed(b"bad\0" + good[:-2])
        frames += decoder.feed(b"\0" + b"x" * 500)
        frames += decoder.feed(b"x" * 40)
        frames += decoder.feed(b"\0" + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].msg_type, HELLO)

    def test_radio_and_mock_parser(self):
        packet = encode_radio(3, 4, b"a")
        self.assertEqual(decode_radio(packet), (3, 4, b"a"))
        self.assertEqual(parse_mock_app(make_mock_app(2, 9, b"ok")), (2, 9, b"ok"))
        with self.assertRaises(ProtocolError):
            parse_mock_app(b"MB\x01\x01\0\0\0\0\x02")

    def test_typed_payload_parsers(self):
        self.assertEqual(
            parse_info(b"123456" + struct.pack(">I I H B B I", 2, 3, 200, 1, 4, 5))[1],
            2,
        )
        self.assertEqual(parse_tx_result(struct.pack(">III", 1, 2, 3)), (1, 2, 3))
        rx = parse_radio_rx(struct.pack(">IIhhI", 1, 2, -5234, -125, 8) + b"x")
        self.assertEqual(rx[2:4], (-52.34, -1.25))
        self.assertEqual(rx[-1], b"x")
        status = parse_link_status(struct.pack(">IIB3xIIIIII", *range(1, 10)))
        self.assertEqual(status, (1, 2, 3, 4, 5, 6, 7, 8, 9))
        self.assertEqual(
            parse_diagnostics(struct.pack(">IIIIIIIIII", *range(10))),
            tuple(range(10)),
        )
        self.assertEqual(parse_error(struct.pack(">HhB", 2, -4, 3)), (2, -4, 3))

    def test_client_preserves_event_and_filters_sessions(self):
        class FakeSerial:
            def __init__(self, *a, **k):
                self.rx = bytearray()
                self.closed = False

            def write(self, data):
                if data == b"\0":
                    return
                typ, session, seq, _payload = decode_frame(data[:-1])
                if typ == RADIO_SEND:
                    self.rx.extend(
                        encode_frame(
                            RADIO_RX,
                            session,
                            0,
                            struct.pack(">IIhhI", 1, 2, -5000, -100, 3) + b"event",
                        )
                    )
                    self.rx.extend(
                        encode_frame(
                            RADIO_TX_RESULT, session, seq, struct.pack(">III", 1, 2, 3)
                        )
                    )
                    self.rx.extend(
                        encode_frame(
                            RADIO_TX_RESULT,
                            session + 1,
                            seq,
                            struct.pack(">III", 9, 9, 9),
                        )
                    )

            def read(self, n):
                out = bytes(self.rx)
                self.rx.clear()
                return out

            def close(self):
                self.closed = True

        fake = types.SimpleNamespace(Serial=FakeSerial)
        with patch.dict(sys.modules, {"serial": fake}):
            client = ModemClient("fake")
            result, _ = client.send(b"x")
            events = client.events()
        self.assertEqual(parse_tx_result(result.payload), (1, 2, 3))
        self.assertEqual(len(events), 1)
        self.assertEqual(client.counters["wrong_session"], 1)
        self.assertEqual(client.counters["radio_rx_events"], 1)
        self.assertEqual(client.counters["radio_rx_events_matched"], 0)

    def test_take_radio_event_preserves_unmatched_event(self):
        client = ModemClient.__new__(ModemClient)
        first = Frame(RADIO_RX, 1, 0, b"first")
        second = Frame(RADIO_RX, 1, 0, b"second")
        client.inbox = [first, second]
        client.counters = {}
        client.poll = lambda: client.inbox

        found = client.take_radio_event(lambda frame: frame.payload == b"second")

        self.assertEqual(found, second)
        self.assertEqual(client.inbox, [first])


if __name__ == "__main__":
    unittest.main()
