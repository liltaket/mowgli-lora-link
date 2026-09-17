import unittest

from host.mowgli_lora.service_bench import _error_counts


class ServiceBenchTest(unittest.TestCase):
    def test_acceptance_errors_include_uncertain_timeout_and_all_stale_drops(self):
        errors = _error_counts(
            {
                "rtcm_fragments_tx_uncertain": 1,
                "rtcm_fragments_dropped_stale": 2,
                "rtcm_frames_dropped_stale": 3,
                "usb_response_timeouts": 1,
            },
            {},
        )

        self.assertEqual(1, errors["base_tx_uncertain"])
        self.assertEqual(5, errors["base_stale"])
        self.assertEqual(1, errors["base_response_timeouts"])


if __name__ == "__main__":
    unittest.main()
