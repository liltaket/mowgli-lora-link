import contextlib
import io
import math
import random
import re
import struct
import tempfile
import unittest
from pathlib import Path

from .airtime import (
    FIRMWARE_PHY,
    LEGACY_BW250_PHY,
    LoRaPhy,
    application_airtime_s,
    rtcm_fragment_sizes,
)
from .application import (
    RTCM_FRAGMENT_DATA_SIZE,
    SYNTHETIC_RTCM_SIZES,
    encode,
    fragment,
    synthetic_rtcm,
)
from .duty_cycle import AirtimeBudget, max_window_usage
from .mixed_bench import lora_airtime_s
from .rf_budget import (
    BANDS_SE,
    PLANS,
    RTCM_SCENARIOS,
    Emission,
    RtcmMessage,
    afa_channels,
    assess,
    bands_for,
    cca_threshold_dbm,
    erp_dbm,
    main,
    max_conducted_dbm,
    max_path_loss_db,
    msm_frame_bytes,
    mw_to_dbm,
    polite_access_plan,
    range_table,
    recording_messages,
    rtcm_bytes_per_second,
    rtcm_traffic,
    sensitivity_dbm,
    telemetry_traffic,
)

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware/lora_radio/src/main.cpp"


class AirtimeTests(unittest.TestCase):
    def test_matches_published_lorawan_reference_values(self):
        # 23-byte PHY payload, CR4/5, 8-symbol preamble, explicit header, CRC.
        sf7 = LoRaPhy(7, 125_000, preamble_symbols=8)
        sf12 = LoRaPhy(12, 125_000, preamble_symbols=8)
        self.assertAlmostEqual(sf7.time_on_air_s(23) * 1000, 61.696, places=3)
        self.assertTrue(sf12.ldro)
        self.assertAlmostEqual(sf12.time_on_air_s(23) * 1000, 1482.752, places=3)

    def test_legacy_estimator_was_the_bw250_formula(self):
        for size in range(1, 201):
            legacy = 0.000128 * (26.25 + 5 * math.ceil((8 * (size + 18) + 16) / 20))
            self.assertAlmostEqual(
                application_airtime_s(size, LEGACY_BW250_PHY), legacy
            )

    def test_mixed_bench_uses_the_compiled_firmware_phy(self):
        source = FIRMWARE.read_text()
        match = re.search(r"FREQ = ([\d.]+)F, BW = ([\d.]+)F;", source)
        self.assertIsNotNone(match)
        self.assertEqual(float(match.group(2)) * 1000, FIRMWARE_PHY.bandwidth_hz)
        self.assertIn("constexpr uint8_t SF = 5, CR = 5", source)
        self.assertIn("SYNC_WORD_PRIVATE, POWER, 12,", source)
        self.assertEqual(lora_airtime_s(200), application_airtime_s(200, FIRMWARE_PHY))
        # BW500 halves the symbol time of the old BW250 estimate exactly.
        self.assertAlmostEqual(
            lora_airtime_s(200) * 2, application_airtime_s(200, LEGACY_BW250_PHY)
        )

    def test_fragment_sizes_match_the_real_fragmenter(self):
        for length in (18, 25, 173, 174, 175, 348, 446, 586, 1029):
            raw = synthetic_rtcm(1077, length, 1)
            expected = [len(encode(frame)) + 18 for frame in fragment(raw, 1, 1)]
            self.assertEqual(rtcm_fragment_sizes(length), expected)
        self.assertEqual(rtcm_fragment_sizes(RTCM_FRAGMENT_DATA_SIZE), [218])


class RtcmModelTests(unittest.TestCase):
    def test_msm_sizes_reproduce_the_synthetic_fixtures(self):
        self.assertEqual(msm_frame_bytes(7, 12, 3), SYNTHETIC_RTCM_SIZES[1077])
        self.assertEqual(msm_frame_bytes(7, 8, 2), SYNTHETIC_RTCM_SIZES[1087])
        self.assertEqual(msm_frame_bytes(7, 16, 3), SYNTHETIC_RTCM_SIZES[1127])
        self.assertLess(msm_frame_bytes(4, 12, 3), msm_frame_bytes(7, 12, 3))
        with self.assertRaises(ValueError):
            msm_frame_bytes(7, 22, 3)

    def test_live_model_matches_the_measured_rate(self):
        live = next(s for s in RTCM_SCENARIOS if s.key == "live")
        self.assertEqual(len({m.message_type for m in live.messages}), 14)
        self.assertAlmostEqual(rtcm_bytes_per_second(live.messages), 2139, delta=25)

    def test_synthetic_profile_reproduces_documented_airtime(self):
        messages = [RtcmMessage(t, n) for t, n in SYNTHETIC_RTCM_SIZES.items()]
        messages = [m for m in messages if m.message_type not in (1005, 1230)]
        messages += [RtcmMessage(1005, 25, 10.0), RtcmMessage(1230, 18, 10.0)]
        traffic = rtcm_traffic(messages).merged(telemetry_traffic(2.0))
        # docs: about 33 % at BW500 and about 66 % at the earlier BW250.
        self.assertAlmostEqual(traffic.airtime_fraction(FIRMWARE_PHY), 0.33, delta=0.01)
        self.assertAlmostEqual(
            traffic.airtime_fraction(LEGACY_BW250_PHY), 0.66, delta=0.01
        )


class RegulatoryArithmeticTests(unittest.TestCase):
    def plan(self, key):
        return next(plan for plan in PLANS if plan.key == key)

    def test_erp_reference_and_limits(self):
        self.assertEqual(erp_dbm(10, 2.15), 10)
        self.assertAlmostEqual(max_conducted_dbm(BANDS_SE[2], 2.15), 13.98, places=2)
        band_p = next(band for band in BANDS_SE if band.key == "P")
        self.assertAlmostEqual(max_conducted_dbm(band_p, 2.15), 6.99, places=2)
        self.assertAlmostEqual(cca_threshold_dbm(200_000, 10), -79.0, places=1)

    def test_current_profile_fits_only_band_m(self):
        self.assertEqual(
            [b.key for b in bands_for(self.plan("current").emission)], ["M"]
        )

    def test_500khz_never_fits_the_869_mhz_bands(self):
        for center in (869.525e6, 869.85e6):
            self.assertEqual(bands_for(Emission(center, FIRMWARE_PHY, 0)), [])

    def test_current_live_traffic_exceeds_band_m(self):
        live = next(s for s in RTCM_SCENARIOS if s.key == "live")
        report = assess(
            self.plan("current"), rtcm_traffic(live.messages), telemetry_traffic(2)
        )
        self.assertFalse(report["lawful"])
        self.assertGreater(report["base"]["bands"][0]["duty_overrun_factor"], 40)
        self.assertFalse(report["robot"]["bands"][0]["duty_ok"])

    def test_lab_17dbm_build_exceeds_25mw_with_a_dipole(self):
        report = assess(
            self.plan("current-lab17"), telemetry_traffic(0.001), telemetry_traffic(0)
        )
        self.assertFalse(report["base"]["bands"][0]["erp_ok"])

    def test_band_p_candidate_with_reduced_rtcm(self):
        mix = next(s for s in RTCM_SCENARIOS if s.key == "msm4-4c")
        report = assess(
            self.plan("p-bw250"), rtcm_traffic(mix.messages), telemetry_traffic(2)
        )
        self.assertTrue(report["lawful"])
        self.assertTrue(report["capacity_ok"])
        self.assertIn("P", [row["band"] for row in report["base"]["bands"]])

    def test_recording_uses_exact_frame_sizes_and_span(self):
        frames = [synthetic_rtcm(1077, 446, 1), synthetic_rtcm(1005, 25, 2)] * 3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "base.mlr"
            with path.open("wb") as sink:
                sink.write(b"MLR1")
                for index, raw in enumerate(frames):
                    sink.write(struct.pack(">QH", index * 500_000_000, len(raw)) + raw)
            messages, duration = recording_messages(path)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["--rtcm-recording", str(path), "--plan", "current"]), 0
                )
        self.assertEqual(duration, 2.5)
        self.assertAlmostEqual(rtcm_bytes_per_second(messages), 3 * (446 + 25) / 2.5)
        self.assertIn("RTCM source rate 565 B/s", output.getvalue())

    def test_cli_report(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--plan", "current"]), 0)
        self.assertIn("NOT OK", output.getvalue())


class PoliteAccessAndRangeTests(unittest.TestCase):
    def scenario(self, key):
        return rtcm_traffic(next(s for s in RTCM_SCENARIOS if s.key == key).messages)

    def test_channels_never_share_a_200khz_portion(self):
        widths = (500e3, 250e3, 125e3)
        counts = {b.key: [afa_channels(b, bw) for bw in widths] for b in BANDS_SE[:3]}
        self.assertEqual(counts["K"], [3, 5, 10])
        self.assertEqual(counts["L"], [5, 7, 15])
        self.assertEqual(counts["M"], [1, 1, 3])

    def test_msm4_fits_multichannel_lbt_but_live_does_not(self):
        reduced = polite_access_plan(LoRaPhy(5, 500_000), self.scenario("msm4-4c"))
        self.assertTrue(reduced["fits_conservative"])
        self.assertEqual(
            (reduced["channels_needed"], reduced["channels_available"]), (7, 9)
        )
        live = polite_access_plan(LoRaPhy(5, 500_000), self.scenario("live"))
        self.assertFalse(live["fits_conservative"])
        self.assertTrue(live["single_transmission_ok"])
        self.assertAlmostEqual(reduced["cca_threshold_dbm"], -75.0)

    def test_band_p_link_budget_equals_today(self):
        self.assertAlmostEqual(sensitivity_dbm(LoRaPhy(7, 125_000)), -124.0, delta=0.1)
        today = max_path_loss_db(10.0, LoRaPhy(5, 500_000))
        band_p = max_path_loss_db(mw_to_dbm(5), LoRaPhy(5, 250_000))
        self.assertAlmostEqual(today, band_p, delta=0.05)
        rows = {row["case"]: row for row in range_table(1.5, 0.3)}
        self.assertLess(
            rows["band P, 5 mW, SF5/BW250"]["range_m"]["25.0"],
            rows["LBT+AFA K/L/M, 25 mW, SF5/BW500"]["range_m"]["25.0"],
        )

    def test_cli_polite_access_and_range(self):
        for argv, text in (
            (["--polite-access", "--scenario", "msm4-4c"], "7 channels of  9"),
            (["--range"], "band P, 5 mW"),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(argv), 0)
            self.assertIn(text, output.getvalue())


class AirtimeBudgetTests(unittest.TestCase):
    def run_traffic(self, budget, start, stop, rng, sent):
        t = start
        while t < stop:
            duration = rng.choice((0.01, 0.03, 0.2))
            if budget.try_transmit(t, duration):
                sent.append((t, duration))
            t += rng.expovariate(4.0) + duration

    def test_never_exceeds_any_window_across_restarts(self):
        for prior in ("full", "snapshot"):
            for seed in range(6):
                with self.subTest(prior=prior, seed=seed):
                    rng = random.Random(seed)
                    sent: list[tuple[float, float]] = []
                    window, limit = 600.0, 6.0
                    first = AirtimeBudget(limit, window, 15.0, now=0.0, prior="none")
                    self.run_traffic(first, 0.0, 900.0, rng, sent)
                    snapshot = first.snapshot(900.0)
                    reboot = 900.0 + rng.choice((0.0, 3.0, 40.0))
                    second = AirtimeBudget(
                        limit, window, 15.0, now=reboot, prior=prior, snapshot=snapshot
                    )
                    self.run_traffic(second, reboot, reboot + 1500.0, rng, sent)
                    self.assertLessEqual(max_window_usage(sent, window), limit + 1e-9)
                    self.assertGreater(len(sent), 100)

    def test_full_prior_blocks_until_the_window_has_passed(self):
        budget = AirtimeBudget(36.0, now=0.0)
        self.assertFalse(budget.try_transmit(1.0, 0.03))
        self.assertTrue(budget.try_transmit(3600.0 + 60.0 + 4.0, 0.03))

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            AirtimeBudget(0.0)
        with self.assertRaises(ValueError):
            AirtimeBudget(1.0, prior="snapshot")


if __name__ == "__main__":
    unittest.main()
