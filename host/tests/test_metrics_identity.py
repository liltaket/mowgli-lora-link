import json
import os
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from host.mowgli_lora.metrics import Metrics, serve_metrics


class MetricsIdentityTests(unittest.TestCase):
    def test_health_reports_the_producer_invocation_captured_at_start(self):
        metrics = Metrics()
        for key in ("modem_connected", "modem_handshake_complete", "modem_radio_ready"):
            metrics.set(key, 1)
        invocation = "a" * 32
        with patch.dict(os.environ, {"INVOCATION_ID": invocation}):
            server = serve_metrics(metrics, "127.0.0.1", 0, role="robot")
        try:
            with (
                patch.dict(os.environ, {"INVOCATION_ID": "b" * 32}),
                urlopen(
                    f"http://127.0.0.1:{server.server_port}/healthz", timeout=2
                ) as response,
            ):
                health = json.load(response)
            self.assertEqual(invocation, health["invocation_id"])
            self.assertEqual("ok", health["status"])
            self.assertNotIn("rtcm_fresh", health)
        finally:
            server.shutdown()
            server.server_close()

    def test_invalid_or_absent_invocation_is_omitted(self):
        with patch.dict(os.environ, {"INVOCATION_ID": "not-an-invocation"}):
            server = serve_metrics(Metrics(), "127.0.0.1", 0)
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/healthz", timeout=2
            ) as response:
                self.assertNotIn("invocation_id", json.load(response))
        finally:
            server.shutdown()
            server.server_close()
