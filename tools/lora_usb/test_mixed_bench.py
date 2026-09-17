import contextlib
import io
import unittest

from .application import (
    RTCM_FRAGMENT,
    STOP_REQUEST,
    TELEMETRY,
    Frame,
    StopRequestBody,
    encode_stop,
)
from .mixed_bench import (
    BASE_TO_ROBOT,
    MixedRunner,
    Scheduler,
    SimulatedTransport,
    WorkItem,
    main,
    run_fault_test,
    validate_physical_guard,
)


class SchedulerTests(unittest.TestCase):
    def test_priority_latest_telemetry_and_stale_rtcm(self):
        scheduler = Scheduler()
        scheduler.push(
            WorkItem(Frame(RTCM_FRAGMENT, 1, 1, 0, b"x"), BASE_TO_ROBOT, 0, 1, (1, 1))
        )
        scheduler.push(WorkItem(Frame(TELEMETRY, 2, 1, 0, b"x"), "robot_to_base", 0, 2))
        scheduler.push(WorkItem(Frame(TELEMETRY, 2, 2, 0, b"y"), "robot_to_base", 0, 3))
        stop = Frame(STOP_REQUEST, 1, 2, 0, encode_stop(StopRequestBody(2, 1, 1)))
        scheduler.push(WorkItem(stop, BASE_TO_ROBOT, 0, 4))
        self.assertEqual(scheduler.telemetry_replaced, 1)
        self.assertEqual(scheduler.pop(0).frame.typ, STOP_REQUEST)
        scheduler.pop(1.0)
        self.assertEqual(scheduler.stale_rtcm_drops, 1)

    def test_physical_guardrails(self):
        validate_physical_guard("short-capacity", 20)
        validate_physical_guard("low-duty", 900)
        with self.assertRaises(ValueError):
            validate_physical_guard("short-capacity", 20.1)
        with self.assertRaises(ValueError):
            validate_physical_guard("low-duty", 901)

    def test_invalid_rtcm_rates_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for value in ("0", "-1", "inf", "nan"):
                with self.subTest(value=value), self.assertRaises(SystemExit):
                    main(["--fault-test", "--rtcm-hz", value])
            with self.assertRaises(SystemExit):
                main(["--profile", "low-duty", "--rtcm-hz", "0.5"])


class MixedRunnerTests(unittest.TestCase):
    def test_sixty_second_nominal_capacity(self):
        transport = SimulatedTransport()
        report = MixedRunner(transport, "nominal", 60, 0, 15, 7).run()
        self.assertTrue(report["pass"], report)
        self.assertEqual(report["stop"]["unique"], 3)
        self.assertEqual(report["stop"]["applied"], 3)
        self.assertGreaterEqual(report["rtcm"]["completion_ratio"], 0.99)
        self.assertGreaterEqual(report["telemetry"]["delivery_ratio"], 0.99)

    def test_nominal_rtcm_rate_is_configurable(self):
        report = MixedRunner(SimulatedTransport(), "nominal", 10, 0, 0, 7, 0.5).run()
        self.assertTrue(report["pass"], report)
        self.assertEqual(report["rtcm_hz"], 0.5)
        self.assertEqual(report["rtcm"]["observation_epochs_offered"], 5)

    def test_low_duty_reports_actual_rate(self):
        report = MixedRunner(SimulatedTransport(), "low-duty", 10, 0, 0, 7).run()
        self.assertEqual(report["rtcm_hz"], 0.1)

    def test_fault_recovery(self):
        report = run_fault_test()
        self.assertTrue(report["pass"], report)


if __name__ == "__main__":
    unittest.main()
