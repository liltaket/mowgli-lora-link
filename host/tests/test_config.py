import tempfile
import unittest
from pathlib import Path

from host.mowgli_lora.config import load


class ConfigTest(unittest.TestCase):
    def test_validates_stable_path_ranges_and_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.yaml"
            path.write_text(
                """role: base
modem:
  device: /dev/serial/by-id/usb-test
  reconnect_seconds: 0.5
metrics:
  port: 9608
logging:
  level: debug
"""
            )
            config = load(path)
            self.assertEqual("DEBUG", config.logging_level)
            path.write_text("role: base\nmodem: {device: /dev/ttyACM0}\n")
            with self.assertRaises(ValueError):
                load(path)
