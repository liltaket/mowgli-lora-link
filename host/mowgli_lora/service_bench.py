"""Bounded physical RTCM bench for the asynchronous Pi service path."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from tools.lora_usb.application import synthetic_epoch

from .config import ModemConfig, ServiceConfig
from .metrics import Metrics
from .modem import ModemTransport
from .rtcm_tool import compare_frames
from .service import BaseService, RobotService


class CaptureOutput:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def publish(self, raw: bytes) -> int:
        self.frames.append(raw)
        return 1

    def flush(self) -> None:
        return


def _error_counts(
    base_snapshot: dict[str, int | float],
    robot_snapshot: dict[str, int | float],
) -> dict[str, int | float]:
    return {
        "base_tx_failed": base_snapshot.get("rtcm_fragments_tx_failed", 0),
        "base_tx_uncertain": base_snapshot.get("rtcm_fragments_tx_uncertain", 0),
        "base_stale": base_snapshot.get("rtcm_fragments_dropped_stale", 0)
        + base_snapshot.get("rtcm_frames_dropped_stale", 0),
        "base_response_timeouts": base_snapshot.get("usb_response_timeouts", 0),
        "base_modem_errors": base_snapshot.get("modem_errors", 0),
        "robot_rejected": robot_snapshot.get("rtcm_fragments_rejected", 0),
        "robot_timeouts": robot_snapshot.get("rtcm_reassembly_timeouts", 0),
        "robot_modem_errors": robot_snapshot.get("modem_errors", 0),
    }


def run(base_port: str, robot_port: str, duration_s: float, rtcm_hz: float) -> dict:
    base_metrics, robot_metrics = Metrics(), Metrics()
    base_transport = ModemTransport(base_port, base_metrics)
    robot_transport = ModemTransport(robot_port, robot_metrics)
    base = BaseService(
        ServiceConfig("base", ModemConfig(base_port)),
        base_transport,
        base_metrics,
    )
    output = CaptureOutput()
    robot = RobotService(robot_transport, output, robot_metrics)
    source: list[bytes] = []
    started = time.monotonic()
    next_epoch = 0.0
    epoch = 0
    try:
        while True:
            now = time.monotonic()
            elapsed = now - started
            while elapsed < duration_s and elapsed >= next_epoch:
                epoch += 1
                for raw in synthetic_epoch(epoch):
                    source.append(raw)
                    base.ingest(raw, now)
                next_epoch += 1.0 / rtcm_hz
            base.step(now)
            robot.step(now)
            if elapsed >= duration_s and base.idle:
                break
            if elapsed >= duration_s + 2.0:
                break
            time.sleep(0.001)
    finally:
        base_transport.close()
        robot_transport.close()
    comparison = compare_frames(source, output.frames)
    base_snapshot = base_metrics.snapshot()
    robot_snapshot = robot_metrics.snapshot()
    errors = _error_counts(base_snapshot, robot_snapshot)
    return {
        "mode": "physical-pi-service",
        "duration_s": duration_s,
        "rtcm_hz": rtcm_hz,
        "pass": bool(source)
        and comparison["byte_exact_ordered_match"]
        and not any(errors.values()),
        "comparison": comparison,
        "errors": errors,
        "base_metrics": base_snapshot,
        "robot_metrics": robot_snapshot,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BOUNDED physical RTCM bench for the async Pi service path"
    )
    parser.add_argument("--base-port", required=True)
    parser.add_argument("--robot-port", required=True)
    parser.add_argument("--duration-s", type=float, default=20.0)
    parser.add_argument("--rtcm-hz", type=float, default=0.75)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not 0 < args.duration_s <= 30:
        parser.error("physical service bench duration must be in (0, 30] seconds")
    if not math.isfinite(args.rtcm_hz) or not 0 < args.rtcm_hz <= 1:
        parser.error("physical service bench RTCM rate must be in (0, 1] Hz")
    report = run(args.base_port, args.robot_port, args.duration_s, args.rtcm_hz)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
