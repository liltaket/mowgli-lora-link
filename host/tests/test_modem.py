import struct
import time
import unittest

from host.mowgli_lora.metrics import Metrics
from host.mowgli_lora.modem import ModemTransport
from tools.lora_usb import protocol


class FakeSerial:
    def __init__(self):
        self.input = bytearray()
        self.output = bytearray()
        self.closed = False

    def read(self, _size):
        data, self.input = bytes(self.input), bytearray()
        return data

    def write(self, raw):
        self.output.extend(raw)
        return len(raw)

    def close(self):
        self.closed = True


class ModemTransportTest(unittest.TestCase):
    def _info(self, session, sequence, boot):
        payload = b"ABCDEF" + boot.to_bytes(4, "big") + b"\0" * 12
        return protocol.encode_frame(protocol.INFO, session, sequence, payload)

    def test_boot_change_discards_pending_and_delivers_rx(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        self.assertTrue(modem.connected)
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        modem.send_air(b"payload")
        self.assertTrue(modem.pending)
        old_session = modem.session
        serial.input.extend(self._info(modem.session, 3, 12))
        modem.tick()
        self.assertNotEqual(old_session, modem.session)
        self.assertTrue(modem.pending)  # new HELLO is the only pending request
        self.assertFalse(modem.ready_for_send)
        serial.input.extend(self._info(modem.session, 1, 12))
        modem.tick()
        self.assertTrue(modem.ready_for_send)
        radio = protocol.encode_radio(1, 1, b"hello")
        rx = b"\0" * 16 + radio
        serial.input.extend(
            protocol.encode_frame(protocol.RADIO_RX, modem.session, 0, rx)
        )
        events = modem.tick()
        self.assertEqual([radio], events)
        stats = modem.metrics.snapshot()
        self.assertEqual(1, stats["modem_boot_id_changes"])
        self.assertEqual(1, stats["rx_event_queued"])

    def test_diagnostics_are_exposed_as_stage_metrics(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        diagnostics = protocol.encode_frame(
            protocol.DIAGNOSTICS,
            modem.session,
            2,
            b"".join(value.to_bytes(4, "big") for value in range(10)),
        )
        serial.input.extend(diagnostics)
        modem.tick()
        self.assertEqual(6, modem.metrics.snapshot()["modem_rx_event_queued"])

    def test_info_clears_hello_pending(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        self.assertEqual(1, len(modem.pending))
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        self.assertFalse(modem.pending)

    def test_partial_serial_writes_are_completed(self):
        class PartialSerial(FakeSerial):
            def write(self, raw):
                part = raw[:2]
                self.output.extend(part)
                return len(part)

        serial = PartialSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        decoded = protocol.StreamDecoder().feed(bytes(serial.output))
        self.assertEqual(protocol.HELLO, decoded[0].msg_type)

    def test_wrong_session_diagnostics_starts_new_hello(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        old_session = modem.session
        sequence = next(iter(modem.pending))
        error = protocol.encode_frame(
            protocol.ERROR,
            old_session,
            sequence,
            struct.pack(">HhB", 4, 0, protocol.GET_DIAGNOSTICS),
        )
        serial.input.extend(error)
        modem.tick()
        self.assertNotEqual(old_session, modem.session)
        self.assertFalse(modem.handshake_complete)
        self.assertEqual(1, modem.metrics.snapshot()["modem_wrong_session_recoveries"])

    def test_disconnect_then_reconnect(self):
        serials = [FakeSerial(), FakeSerial()]
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            reconnect_seconds=0,
            serial_factory=lambda *_args, **_kwargs: serials.pop(0),
        )
        modem.tick()
        modem.serial.read = lambda _size: (_ for _ in ()).throw(OSError("gone"))
        modem.tick()
        self.assertFalse(modem.connected)
        modem.tick()
        self.assertTrue(modem.connected)
        self.assertEqual(2, modem.metrics.snapshot()["usb_reconnects"])
