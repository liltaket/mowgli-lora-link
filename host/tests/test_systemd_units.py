import configparser
import unittest
from pathlib import Path


class SystemdUnitTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parents[1] / "systemd"

    def _load(self, name):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read(self.directory / name)
        return parser

    def test_units_use_installed_absolute_commands_and_restart_on_failure(self):
        for path in sorted(self.directory.glob("*.service")):
            with self.subTest(unit=path.name):
                unit = self._load(path.name)
                service = unit["Service"]
                self.assertEqual("mowgli", service["User"])
                self.assertEqual("mowgli", service["Group"])
                self.assertTrue(service["ExecStart"].startswith("/"))
                self.assertNotIn("PYTHONPATH", path.read_text())
                self.assertEqual("on-failure", service["Restart"])
                self.assertEqual("true", service["NoNewPrivileges"])
                self.assertEqual("true", service["PrivateTmp"])
                self.assertEqual("strict", service["ProtectSystem"])
                self.assertNotIn("PrivateDevices", service)

    def test_usb_services_retain_dialout_access(self):
        for name in ("mowgli-lora-base.service", "mowgli-lora-robot.service"):
            with self.subTest(unit=name):
                service = self._load(name)["Service"]
                self.assertIn("dialout", service["SupplementaryGroups"].split())


if __name__ == "__main__":
    unittest.main()
