import tempfile
import unittest
from pathlib import Path

from host.mowgli_lora.config import load


class ConfigTest(unittest.TestCase):
    def _load_text(self, directory, text):
        path = Path(directory) / "service.yaml"
        path.write_text(text)
        return load(path)

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
rtcm:
  input:
    type: stdin
"""
            )
            config = load(path)
            self.assertEqual("DEBUG", config.logging_level)
            path.write_text("role: base\nmodem: {device: /dev/ttyACM0}\n")
            with self.assertRaises(ValueError):
                load(path)

    def test_validates_base_tcp_and_serial_input_synchronously(self):
        prefix = """role: base
modem: {device: /dev/serial/by-id/usb-test}
rtcm:
  input:
"""
        with tempfile.TemporaryDirectory() as directory:
            tcp = self._load_text(
                directory,
                prefix + "    type: tcp\n    host: 127.0.0.1\n    port: '2101'\n",
            )
            self.assertEqual(2101, tcp.rtcm_input["port"])
            serial = self._load_text(
                directory,
                prefix
                + "    type: serial\n"
                + "    device: /dev/serial/by-id/gnss\n"
                + "    baudrate: '115200'\n",
            )
            self.assertEqual(115200, serial.rtcm_input["baudrate"])

            invalid_inputs = (
                "    type: tcp\n    port: 2101\n",
                "    type: tcp\n    host: 127.0.0.1\n    port: 70000\n",
                "    type: serial\n    device: /dev/ttyUSB0\n",
                "    type: invalid\n",
            )
            for input_config in invalid_inputs:
                with (
                    self.subTest(input_config=input_config),
                    self.assertRaises((ValueError, TypeError)),
                ):
                    self._load_text(directory, prefix + input_config)

    def test_validates_robot_tcp_output_synchronously(self):
        prefix = """role: robot
modem: {device: /dev/serial/by-id/usb-test}
rtcm:
  output:
"""
        with tempfile.TemporaryDirectory() as directory:
            config = self._load_text(
                directory,
                prefix + "    type: tcp\n    bind: 127.0.0.1\n    port: '2233'\n",
            )
            self.assertEqual(2233, config.rtcm_output["port"])
            for output_config in (
                "    type: udp\n    bind: 127.0.0.1\n    port: 2233\n",
                "    type: tcp\n    port: 2233\n",
                "    type: tcp\n    bind: 127.0.0.1\n",
            ):
                with (
                    self.subTest(output_config=output_config),
                    self.assertRaises((ValueError, TypeError)),
                ):
                    self._load_text(directory, prefix + output_config)

    def test_rejects_missing_role_specific_rtcm_and_non_mapping_root(self):
        with tempfile.TemporaryDirectory() as directory:
            for text in (
                "- not-a-mapping\n",
                "role: base\nmodem: {device: /dev/serial/by-id/usb-test}\n",
                "role: robot\nmodem: {device: /dev/serial/by-id/usb-test}\n",
            ):
                with (
                    self.subTest(text=text),
                    self.assertRaises((ValueError, TypeError)),
                ):
                    self._load_text(directory, text)
