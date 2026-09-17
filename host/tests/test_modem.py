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
    def _info(self, session, sequence, boot, *, radio_ready=1):
        payload = struct.pack(
            ">6sI I H B B I", b"ABCDEF", boot, 15, 200, radio_ready, 1, 0
        )
        return protocol.encode_frame(protocol.INFO, session, sequence, payload)

    def test_boot_change_on_current_hello_completes_without_double_handshake(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        self.assertTrue(modem.connected)
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        modem._restart_host_session("test reboot")
        current_session = modem.session
        hello_sequence = next(iter(modem.pending))
        serial.output.clear()
        serial.input.extend(self._info(current_session, hello_sequence, 12))
        modem.tick()
        self.assertEqual(current_session, modem.session)
        self.assertTrue(modem.ready_for_send)
        stats = modem.metrics.snapshot()
        self.assertEqual(1, stats["modem_boot_id_changes"])
        self.assertFalse(modem.pending)
        self.assertEqual(b"", bytes(serial.output))

    def test_diagnostics_are_exposed_as_stage_metrics(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = 0.0
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

    def test_disconnect_resets_connection_and_handshake_gauges_immediately(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        serial.read = lambda _size: (_ for _ in ()).throw(OSError("gone"))
        modem.tick()
        values = modem.metrics.snapshot()
        self.assertEqual(0, values["modem_connected"])
        self.assertEqual(0, values["modem_handshake_complete"])
        self.assertEqual(0, values["modem_ready_for_send"])

    def test_disconnect_after_radio_accept_is_uncertain_not_failed(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick(diagnostics_idle=False)
        sequence = modem.send_air(b"payload")
        serial.read = lambda _size: (_ for _ in ()).throw(OSError("gone"))

        modem.tick(diagnostics_idle=False)

        self.assertEqual([(sequence, None)], modem.take_tx_outcomes())
        self.assertEqual(1, modem.metrics.snapshot()["radio_send_disconnect_uncertain"])
        self.assertFalse(modem.connected)

    def test_diagnostics_do_not_compete_with_application_work(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        serial.output.clear()
        modem._last_diagnostics = 0.0
        modem.tick(diagnostics_idle=False)
        self.assertEqual(b"", bytes(serial.output))
        modem.tick(diagnostics_idle=True)
        decoded = protocol.StreamDecoder().feed(bytes(serial.output))
        self.assertEqual(
            [protocol.GET_DIAGNOSTICS], [frame.msg_type for frame in decoded]
        )

    def test_radio_write_error_is_contained_and_clears_pending_events(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        modem.received.append(b"already-decoded")
        serial.write = lambda _raw: (_ for _ in ()).throw(OSError("gone"))
        self.assertIsNone(modem.send_air(b"payload"))
        self.assertFalse(modem.connected)
        self.assertEqual([], modem.tick())
        self.assertEqual(1, modem.metrics.snapshot()["radio_send_usb_write_failures"])

    def test_wrong_session_radio_error_fails_and_immediately_rehandshakes(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        sequence = modem.send_air(b"payload")
        self.assertIsNotNone(sequence)
        old_session = modem.session
        serial.input.extend(
            protocol.encode_frame(
                protocol.ERROR,
                old_session,
                sequence,
                struct.pack(">HhB", 4, 0, protocol.RADIO_SEND),
            )
        )
        modem.tick(diagnostics_idle=False)
        self.assertNotEqual(old_session, modem.session)
        self.assertFalse(modem.handshake_complete)
        self.assertEqual([(sequence, False)], modem.take_tx_outcomes())

    def test_unexpected_reply_cannot_remove_radio_send_pending(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        sequence = modem.send_air(b"payload")
        self.assertIsNotNone(sequence)
        serial.input.extend(
            protocol.encode_frame(
                protocol.DIAGNOSTICS,
                modem.session,
                sequence,
                b"\0" * 40,
            )
        )
        modem.tick(diagnostics_idle=False)
        self.assertIn(sequence, modem.pending)
        self.assertEqual(1, modem.metrics.snapshot()["usb_unexpected_diagnostics"])

    def test_info_requires_a_correlated_hello_and_cannot_consume_radio_send(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        sequence = modem.send_air(b"payload")
        serial.input.extend(self._info(modem.session, sequence, 99))
        modem.tick(diagnostics_idle=False)
        self.assertIn(sequence, modem.pending)
        self.assertEqual(11, modem.boot_id)
        self.assertEqual(1, modem.metrics.snapshot()["usb_unexpected_info"])

    def test_every_physical_reopen_uses_a_fresh_host_session(self):
        serials = [FakeSerial(), FakeSerial()]
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            reconnect_seconds=0,
            serial_factory=lambda *_args, **_kwargs: serials.pop(0),
        )
        modem.tick()
        first_session = modem.session
        modem.serial.read = lambda _size: (_ for _ in ()).throw(OSError("gone"))
        modem.tick()
        modem.tick()
        self.assertNotEqual(first_session, modem.session)
        frames = protocol.StreamDecoder().feed(bytes(modem.serial.output))
        self.assertEqual(
            [(protocol.HELLO, modem.session)], [(f.msg_type, f.session) for f in frames]
        )

    def test_hello_timeout_error_and_malformed_info_retry_fresh_sessions(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        sequence = next(iter(modem.pending))
        modem.pending[sequence] = (protocol.HELLO, time.monotonic() - 4)
        first_session = modem.session
        modem.tick()
        self.assertNotEqual(first_session, modem.session)
        sequence = next(iter(modem.pending))
        second_session = modem.session
        serial.input.extend(
            protocol.encode_frame(
                protocol.ERROR,
                second_session,
                sequence,
                struct.pack(">HhB", 3, 0, protocol.HELLO),
            )
        )
        modem.tick()
        self.assertNotEqual(second_session, modem.session)
        sequence = next(iter(modem.pending))
        third_session = modem.session
        serial.input.extend(
            protocol.encode_frame(protocol.INFO, third_session, sequence, b"bad")
        )
        modem.tick()
        self.assertNotEqual(third_session, modem.session)
        self.assertEqual(1, modem.metrics.snapshot()["hello_timeouts"])
        self.assertEqual(1, modem.metrics.snapshot()["hello_errors"])
        self.assertEqual(1, modem.metrics.snapshot()["hello_invalid_info"])

    def test_radio_ready_from_info_gates_sends(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        serial.input.extend(self._info(modem.session, 1, 11, radio_ready=0))
        modem.tick(diagnostics_idle=False)
        self.assertTrue(modem.handshake_complete)
        self.assertFalse(modem.ready_for_send)
        self.assertEqual(0, modem.metrics.snapshot()["modem_radio_ready"])

    def test_malformed_tx_result_is_not_successful(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        sequence = modem.send_air(b"payload")
        serial.input.extend(
            protocol.encode_frame(
                protocol.RADIO_TX_RESULT, modem.session, sequence, b"bad"
            )
        )
        modem.tick(diagnostics_idle=False)
        self.assertEqual([(sequence, False)], modem.take_tx_outcomes())
        self.assertEqual(1, modem.metrics.snapshot()["radio_tx_result_decode_failures"])

    def test_radio_send_timeout_is_uncertain_and_late_result_is_observed(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick(diagnostics_idle=False)
        sequence = modem.send_air(b"payload")
        session = modem.session
        modem.pending[sequence] = (protocol.RADIO_SEND, time.monotonic() - 4)

        modem.tick(diagnostics_idle=False)

        self.assertEqual(session, modem.session)
        self.assertEqual([(sequence, None)], modem.take_tx_outcomes())
        self.assertTrue(modem.handshake_complete)
        self.assertTrue(modem.ready_for_send)
        snapshot = modem.metrics.snapshot()
        self.assertEqual(1, snapshot["radio_send_timeouts"])
        self.assertEqual(1, snapshot["usb_response_timeouts"])

        next_sequence = modem.send_air(b"next")
        serial.input.extend(
            protocol.encode_frame(
                protocol.RADIO_TX_RESULT,
                session,
                sequence,
                struct.pack(">III", 11, 22, 33),
            )
            + protocol.encode_frame(
                protocol.RADIO_TX_RESULT,
                session,
                next_sequence,
                struct.pack(">III", 11, 23, 34),
            )
        )
        modem.tick(diagnostics_idle=False)
        self.assertEqual([(next_sequence, True)], modem.take_tx_outcomes())
        self.assertEqual(1, modem.metrics.snapshot()["radio_tx_late_completed"])
        self.assertNotIn("usb_unexpected_radio_tx_result", modem.metrics.snapshot())

    def test_late_error_for_timed_out_send_does_not_consume_newer_request(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick(diagnostics_idle=False)
        sequence = modem.send_air(b"payload")
        modem.pending[sequence] = (protocol.RADIO_SEND, time.monotonic() - 4)
        modem.tick(diagnostics_idle=False)
        modem.take_tx_outcomes()

        next_sequence = modem.send_air(b"next")
        serial.input.extend(
            protocol.encode_frame(
                protocol.ERROR,
                modem.session,
                sequence,
                struct.pack(">HhB", 8, 0, protocol.RADIO_SEND),
            )
        )
        modem.tick(diagnostics_idle=False)

        self.assertIn(next_sequence, modem.pending)
        self.assertEqual([], modem.take_tx_outcomes())
        self.assertEqual(1, modem.metrics.snapshot()["radio_tx_late_errors"])

    def test_radio_timeout_tombstones_are_bounded_and_expire(self):
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: FakeSerial(),
        )
        for sequence in range(modem.MAX_LATE_RADIO_RESULTS + 2):
            modem._remember_timed_out_radio(sequence, 10.0)
        self.assertEqual(modem.MAX_LATE_RADIO_RESULTS, len(modem._timed_out_radio))
        self.assertEqual(
            2,
            modem.metrics.snapshot()["radio_tx_timeout_tombstones_evicted"],
        )

        modem._expire_timed_out_radio(10.0 + modem.LATE_RADIO_RESULT_TTL_S + 0.1)
        self.assertFalse(modem._timed_out_radio)
        self.assertEqual(
            modem.MAX_LATE_RADIO_RESULTS,
            modem.metrics.snapshot()["radio_tx_timeout_tombstones_expired"],
        )

    def test_sequence_wrap_rotates_before_any_new_radio_request(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        old_session = modem.session
        modem.sequence = 0xFFFFFFFF
        self.assertIsNone(modem.send_air(b"payload"))
        self.assertNotEqual(old_session, modem.session)
        self.assertFalse(modem.handshake_complete)
        self.assertEqual(protocol.HELLO, next(iter(modem.pending.values()))[0])

    def test_continuous_radio_rx_defers_diagnostics_polling(self):
        serial = FakeSerial()
        modem = ModemTransport(
            "/dev/serial/by-id/test",
            Metrics(),
            serial_factory=lambda *_args, **_kwargs: serial,
        )
        modem.tick()
        modem._last_diagnostics = time.monotonic()
        serial.input.extend(self._info(modem.session, 1, 11))
        modem.tick()
        serial.output.clear()
        modem._last_diagnostics = 0.0
        serial.input.extend(
            protocol.encode_frame(
                protocol.RADIO_RX,
                modem.session,
                0,
                b"\0" * 16 + b"payload",
            )
        )
        modem.tick(diagnostics_idle=True)
        self.assertEqual(b"", bytes(serial.output))
        modem._last_radio_rx = 0.0
        modem.tick(diagnostics_idle=True)
        frames = protocol.StreamDecoder().feed(bytes(serial.output))
        self.assertEqual(
            [protocol.GET_DIAGNOSTICS], [frame.msg_type for frame in frames]
        )
