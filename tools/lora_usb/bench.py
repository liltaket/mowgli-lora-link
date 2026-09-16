"""One-command BASE/ROBOT mock bench. Prototype; never production control software."""

import argparse
import sys
import time

from .client import ModemClient, ModemError
from .protocol import *


def main():
    p = argparse.ArgumentParser(
        description="PROTOTYPE: mock base/robot over two LoRa USB modems"
    )
    p.add_argument("--base-port", required=True)
    p.add_argument("--robot-port", required=True)
    p.add_argument(
        "--count", type=int, default=0, help="bounded runs; omit for interactive q/quit"
    )
    p.add_argument("--interval", type=float, default=2.0)
    a = p.parse_args()
    base = robot = None
    try:
        base, robot = ModemClient(a.base_port), ModemClient(a.robot_port)
        bi, ri = base.hello(), robot.hello()
        print(f"PROTOTYPE MOCK BENCH ready base={a.base_port} robot={a.robot_port}")
        print(
            f"  base INFO {parse_info(bi.payload)}\n  robot INFO {parse_info(ri.payload)}"
        )
        n = 0
        failures = 0
        while a.count <= 0 or n < a.count:
            if a.count <= 0:
                cmd = input("mock base> [Enter] send, q quit: ").strip().lower()
                if cmd in ("q", "quit"):
                    break
            n += 1
            payload = make_mock_app(1, n, f"mock-ping-{n}".encode())
            started = time.monotonic()
            tx, _ = base.send(payload)
            deadline = time.monotonic() + 3
            received = None
            while time.monotonic() < deadline:
                for event in robot.events():
                    try:
                        kind, seq, data = parse_mock_app(
                            parse_radio_rx(event.payload)[5]
                        )
                        if kind == 1:
                            robot.send(make_mock_app(2, seq, b"ack:" + data))
                            break
                    except ProtocolError:
                        pass
                for event in base.events():
                    try:
                        kind, seq, data = parse_mock_app(
                            parse_radio_rx(event.payload)[5]
                        )
                        if kind == 2:
                            received = (seq, data, event)
                            break
                    except ProtocolError:
                        pass
                if received:
                    break
            if not received:
                print(f"{n}: TIMEOUT tx={tx.payload.hex()}")
                failures += 1
            else:
                seq, data, event = received
                _, _, rssi, snr, _, _ = parse_radio_rx(event.payload)
                if seq != n or data != b"ack:" + f"mock-ping-{n}".encode():
                    failures += 1
                    print(f"{n}: BAD ACK seq={seq} data={data!r}")
                else:
                    print(
                        f"{n}: OK app_seq={seq} data={data!r} RTT={1000 * (time.monotonic() - started):.1f}ms RSSI={rssi:.2f}dBm SNR={snr:.2f}dB"
                    )
            # Reverse direction on every bounded iteration: ROBOT initiates,
            # BASE parses and acknowledges, then ROBOT verifies the reply.
            reverse = make_mock_app(3, n, f"mock-reverse-{n}".encode())
            reverse_started = time.monotonic()
            robot.send(reverse)
            reverse_deadline = time.monotonic() + 3
            reverse_ok = False
            reverse_metrics = None
            while time.monotonic() < reverse_deadline and not reverse_ok:
                for event in base.events():
                    try:
                        kind, seq, data = parse_mock_app(
                            parse_radio_rx(event.payload)[5]
                        )
                        if kind == 3:
                            base.send(make_mock_app(4, seq, b"ack:" + data))
                    except ProtocolError:
                        pass
                for event in robot.events():
                    try:
                        kind, seq, data = parse_mock_app(
                            parse_radio_rx(event.payload)[5]
                        )
                        reverse_ok = (
                            kind == 4
                            and seq == n
                            and data == b"ack:" + f"mock-reverse-{n}".encode()
                        )
                        if reverse_ok:
                            _, _, rr, ss, _, _ = parse_radio_rx(event.payload)
                            reverse_metrics = (rr, ss)
                    except ProtocolError:
                        pass
            if not reverse_ok:
                print(f"{n}: REVERSE TIMEOUT", file=sys.stderr)
                failures += 1
            else:
                print(
                    f"{n}: reverse ROBOT->BASE->ROBOT OK RTT={1000 * (time.monotonic() - reverse_started):.1f}ms RSSI={reverse_metrics[0]:.2f}dBm SNR={reverse_metrics[1]:.2f}dB"
                )
            if a.count <= 0 or n < a.count:
                time.sleep(a.interval)
        bs, rs = base.get_link_status(), robot.get_link_status()
        print(
            f"  base counters {parse_link_status(bs.payload)}\n  robot counters {parse_link_status(rs.payload)}"
        )
        if failures:
            return 2
    except (ModemError, TimeoutError, OSError, KeyboardInterrupt) as exc:
        print(f"bench stopped: {exc}", file=sys.stderr)
        return 2
    finally:
        if base:
            base.close()
        if robot:
            robot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
