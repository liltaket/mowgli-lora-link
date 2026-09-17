import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from host.mowgli_lora import rtcm_tool
from tools.lora_usb.application import synthetic_rtcm


class RtcmToolTest(unittest.TestCase):
    def test_compare_quantifies_missing_and_unexpected_frames(self):
        first = synthetic_rtcm(1077, 32)
        second = synthetic_rtcm(1087, 32)
        extra = synthetic_rtcm(1127, 32)
        report = rtcm_tool.compare_frames([first, second], [first, extra])
        self.assertEqual(1, report["matched_frames_in_order"])
        self.assertEqual(1, report["missing_frames"])
        self.assertEqual(1, report["unexpected_frames"])
        self.assertFalse(report["byte_exact_ordered_match"])

    def test_record_inspect_and_deterministic_replay(self):
        first, second = synthetic_rtcm(1077, 32), synthetic_rtcm(1127, 48)
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "capture.mlr"
            old_stdin, old_argv = sys.stdin, sys.argv
            try:
                sys.stdin = io.TextIOWrapper(io.BytesIO(first + second))
                sys.argv = ["record", str(recording)]
                rtcm_tool.main_record()
            finally:
                sys.stdin, sys.argv = old_stdin, old_argv
            self.assertEqual(
                [(1077), (1127)],
                [
                    rtcm_tool.RtcmStreamParser().feed(raw)[0].message_type
                    for _, raw in rtcm_tool._records(recording)
                ],
            )

            class Stdout:
                def __init__(self):
                    self.buffer = io.BytesIO()

            output = Stdout()
            old_stdout, old_argv = sys.stdout, sys.argv
            try:
                sys.stdout = output
                sys.argv = [
                    "replay",
                    str(recording),
                    "--speed",
                    "100000",
                    "--drop-every",
                    "2",
                ]
                rtcm_tool.main_replay()
            finally:
                sys.stdout, sys.argv = old_stdout, old_argv
            self.assertEqual(first, output.buffer.getvalue())

    def test_inspect_is_machine_readable_with_timing_boundary(self):
        first, second = synthetic_rtcm(1077, 32), synthetic_rtcm(1127, 48)
        timed = rtcm_tool.inspect_records(
            [(1_000_000_000, first), (3_000_000_000, second)]
        )
        raw = rtcm_tool.inspect_records([(None, first + second)])
        self.assertEqual({"1077", "1127"}, set(map(str, timed["message_types"])))
        self.assertTrue(timed["timing_available"])
        self.assertEqual(2.0, timed["duration_seconds"])
        self.assertFalse(raw["timing_available"])
        self.assertIsNone(raw["average_bytes_per_second"])
        self.assertIsInstance(json.dumps(timed), str)
