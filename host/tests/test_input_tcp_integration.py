import socket
import time
import unittest

from host.mowgli_lora.metrics import Metrics
from host.mowgli_lora.rtcm import RtcmStreamParser
from host.mowgli_lora.service import _InputChunk, _InputPump, _InputSessionStarted
from tools.lora_usb.application import synthetic_rtcm


class TcpInputIntegrationTest(unittest.TestCase):
    def _listener(self, port):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        listener.settimeout(2.0)
        return listener

    def _wait_for(self, predicate, message, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail(message)

    def test_absent_server_partial_eof_restart_and_recovery(self):
        # Reserve an ephemeral port and close it so the first pump attempts are
        # real connection refusals rather than a mocked exception.
        probe = self._listener(0)
        port = probe.getsockname()[1]
        probe.close()

        stopped = [False]
        metrics = Metrics()
        pump = _InputPump(
            {"type": "tcp", "host": "127.0.0.1", "port": port}, stopped, metrics
        )
        pump.INITIAL_BACKOFF_SECONDS = 0.01
        pump.MAX_BACKOFF_SECONDS = 0.05
        pump.start()
        try:
            self._wait_for(
                lambda: metrics.snapshot().get("rtcm_source_connect_failures", 0) >= 1,
                "pump did not observe absent TCP source",
            )

            raw = synthetic_rtcm(1077, 128, 41)
            first = self._listener(port)
            connection, _ = first.accept()
            connection.sendall(raw[:31])
            connection.close()
            first.close()
            self._wait_for(
                lambda: metrics.snapshot().get("rtcm_source_disconnects", 0) >= 1,
                "pump did not observe clean EOF",
            )

            second = self._listener(port)
            connection, _ = second.accept()
            connection.sendall(raw)

            def raw_is_queued():
                with pump.queue.mutex:
                    return any(
                        isinstance(item, _InputChunk) and item.raw == raw
                        for item in pump.queue.queue
                    )

            self._wait_for(
                lambda: metrics.snapshot().get("rtcm_source_reconnects", 0) >= 1,
                "pump did not reconnect to restarted server",
            )
            self._wait_for(
                raw_is_queued,
                "valid RTCM was not queued after source restart",
            )
            stopped[0] = True
            connection.close()
            second.close()
            pump.thread.join(timeout=1.0)

            parser = RtcmStreamParser()
            recovered = []
            while not pump.queue.empty():
                item = pump.queue.get_nowait()
                if isinstance(item, _InputSessionStarted):
                    parser.reset()
                elif isinstance(item, _InputChunk):
                    recovered.extend(parser.feed(item.raw, item.received_at))
            self.assertEqual([raw], [frame.raw for frame in recovered])
            snapshot = metrics.snapshot()
            self.assertGreaterEqual(snapshot["rtcm_source_connect_failures"], 1)
            self.assertGreaterEqual(snapshot["rtcm_source_connects"], 2)
            self.assertGreaterEqual(snapshot["rtcm_source_disconnects"], 2)
            self.assertGreaterEqual(snapshot["rtcm_source_reconnects"], 1)
        finally:
            stopped[0] = True
            pump.thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
