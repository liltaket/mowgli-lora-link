import unittest
from pathlib import Path


class MowgliRosSystemdTest(unittest.TestCase):
    def test_ros_log_path_is_writable_with_strict_system_protection(self):
        unit = (
            Path(__file__).parents[1] / "systemd" / "mowgli-lora-mowgli-rtcm.service"
        ).read_text()
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("RuntimeDirectory=mowgli-lora", unit)
        self.assertIn("RuntimeDirectoryMode=0750", unit)
        self.assertIn("Environment=ROS_LOG_DIR=/run/mowgli-lora", unit)
