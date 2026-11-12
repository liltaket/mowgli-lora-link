import unittest
from pathlib import Path


class UdevRulesTest(unittest.TestCase):
    def setUp(self):
        self.rule_path = (
            Path(__file__).parents[1] / "udev" / "99-mowgli-esp32s3-dialout.rules"
        )
        self.rule = next(
            line.strip()
            for line in self.rule_path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    def test_rule_targets_only_supported_esp32s3_usb_tty(self):
        self.assertEqual(
            'SUBSYSTEM=="tty", SUBSYSTEMS=="usb", '
            'ATTRS{idVendor}=="303a", ATTRS{idProduct}=="1001", '
            'GROUP="dialout", MODE="0660"',
            self.rule,
        )
        self.assertNotIn("serial", self.rule.lower())
        self.assertNotIn("plugdev", self.rule.lower())

    def test_rule_name_sorts_after_openocd_rule(self):
        self.assertGreater(self.rule_path.name, "60-openocd.rules")


if __name__ == "__main__":
    unittest.main()
