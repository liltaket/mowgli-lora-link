"""Mixed STOP/telemetry/RTCM prototype bench.

STOP only changes an in-memory mock state. Physical mode controls two radio
modems on a table; it never connects to mower control.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .application import (
    COMMAND_ACK,
    HELLO,
    ROLE_BASE,
    ROLE_ROBOT,
    RTCM_FRAGMENT,
    RTCM_OBSERVATION_SIZES,
    STATE_STOPPED,
    STOP_COMMAND,
    STOP_REQUEST,
    TELEMETRY,
    ApplicationError,
    CommandAckBody,
    Frame,
    Reassembler,
    StopCache,
    StopRequestBody,
    TelemetrySample,
    decode,
    decode_ack,
    decode_hello,
    decode_telemetry,
    encode,
    encode_ack,
    encode_hello,
    encode_stop,
    encode_telemetry,
    fragment,
    rtcm_type,
    synthetic_epoch,
    synthetic_rtcm,
)
from .protocol import (
    parse_diagnostics,
    parse_info,
    parse_link_status,
    parse_radio_rx,
)

BASE_TO_ROBOT = "base_to_robot"
ROBOT_TO_BASE = "robot_to_base"

PRIORITY = {
    COMMAND_ACK: 0,
    STOP_REQUEST: 1,
    HELLO: 1,
    TELEMETRY: 2,
    RTCM_FRAGMENT: 3,
}

KIND_BY_TYPE = {
    HELLO: "hello",
    STOP_REQUEST: "stop",
    COMMAND_ACK: "ack",
    TELEMETRY: "telemetry",
    RTCM_FRAGMENT: "rtcm_fragment",
}


@dataclass
class WorkItem:
    frame: Frame
    direction: str
    created_at: float
    order: int
    rtcm_key: tuple[int, int] | None = None


def lora_airtime_s(application_size: int) -> float:
    """SX1262 SF5/BW250/CR4/5 explicit-header airtime, preamble 12."""
    radio_packet_bytes = application_size + 18
    payload_term = math.ceil((8 * radio_packet_bytes + 16) / 20)
    return 0.000128 * (26.25 + 5 * payload_term)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


class Scheduler:
    def __init__(self, limit: int = 96) -> None:
        self.limit = limit
        self.items: list[WorkItem] = []
        self.high_water = 0
        self.queue_drops = 0
        self.telemetry_replaced = 0
        self.stale_rtcm_drops = 0
        self.stale_rtcm_keys: set[tuple[int, int]] = set()

    def push(self, item: WorkItem) -> bool:
        if item.frame.typ == TELEMETRY:
            retained = []
            for queued in self.items:
                if queued.frame.typ == TELEMETRY and queued.direction == item.direction:
                    self.telemetry_replaced += 1
                else:
                    retained.append(queued)
            self.items = retained

        if len(self.items) >= self.limit:
            worst_index = max(
                range(len(self.items)),
                key=lambda index: (
                    PRIORITY[self.items[index].frame.typ],
                    self.items[index].order,
                ),
            )
            if PRIORITY[item.frame.typ] < PRIORITY[self.items[worst_index].frame.typ]:
                self.items.pop(worst_index)
                self.queue_drops += 1
            else:
                self.queue_drops += 1
                return False
        self.items.append(item)
        self.high_water = max(self.high_water, len(self.items))
        return True

    def pop(self, now: float) -> WorkItem | None:
        retained = []
        for item in self.items:
            if item.frame.typ == RTCM_FRAGMENT and now - item.created_at > 0.900:
                self.stale_rtcm_drops += 1
                if item.rtcm_key:
                    self.stale_rtcm_keys.add(item.rtcm_key)
            else:
                retained.append(item)
        self.items = retained
        if not self.items:
            return None
        index = min(
            range(len(self.items)),
            key=lambda candidate: (
                PRIORITY[self.items[candidate].frame.typ],
                self.items[candidate].created_at,
                self.items[candidate].order,
            ),
        )
        return self.items.pop(index)


class SimulatedTransport:
    mode = "simulate"

    def __init__(self) -> None:
        self.clock = 0.0
        self.estimated_airtime_s = 0.0
        self.transaction_times_ms: list[float] = []

    def now(self) -> float:
        return self.clock

    def idle_until(self, target: float) -> None:
        self.clock = max(self.clock, target)

    def transmit(self, item: WorkItem, frame: Frame) -> tuple[Frame, dict]:
        duration = lora_airtime_s(len(encode(frame)))
        self.estimated_airtime_s += duration
        self.clock += duration + 0.001
        self.transaction_times_ms.append((duration + 0.001) * 1000)
        return decode(encode(frame)), {"rssi": -55.0, "snr": 8.5}

    def close(self) -> None:
        return


class PhysicalTransport:
    mode = "physical"

    def __init__(self, base_port: str, robot_port: str) -> None:
        from .client import ModemClient

        self.base = None
        self.robot = None
        try:
            self.base = ModemClient(base_port, timeout=0)
            self.robot = ModemClient(robot_port, timeout=0)
            base_info = parse_info(self.base.hello().payload)
            robot_info = parse_info(self.robot.hello().payload)
            if not base_info[4] or not robot_info[4]:
                raise RuntimeError("one or both modems report radio_ready=0")
            self.device_info = {
                "base": _info_dict(base_info),
                "robot": _info_dict(robot_info),
            }
            self.status_start = {
                "base": parse_link_status(self.base.get_link_status().payload),
                "robot": parse_link_status(self.robot.get_link_status().payload),
            }
            self.diagnostics_start = {
                "base": parse_diagnostics(self.base.get_diagnostics().payload),
                "robot": parse_diagnostics(self.robot.get_diagnostics().payload),
            }
        except Exception:
            self.close()
            raise
        self.started = time.monotonic()
        self.estimated_airtime_s = 0.0
        self.transaction_times_ms: list[float] = []
        self.application_frames_decoded = 0

    def now(self) -> float:
        return time.monotonic() - self.started

    def idle_until(self, target: float) -> None:
        remaining = target - self.now()
        if remaining > 0:
            time.sleep(min(remaining, 0.010))

    def transmit(self, item: WorkItem, frame: Frame) -> tuple[Frame, dict]:
        sender, receiver = (
            (self.base, self.robot)
            if item.direction == BASE_TO_ROBOT
            else (self.robot, self.base)
        )
        wire = encode(frame)
        started = time.monotonic()
        sender.send(wire, deadline=2.0)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            event = receiver.take_radio_event(
                lambda candidate: parse_radio_rx(candidate.payload)[5] == wire
            )
            if event is not None:
                _, _, rssi, snr, _, payload = parse_radio_rx(event.payload)
                elapsed_ms = (time.monotonic() - started) * 1000
                self.transaction_times_ms.append(elapsed_ms)
                self.estimated_airtime_s += lora_airtime_s(len(wire))
                decoded = decode(payload)
                self.application_frames_decoded += 1
                return decoded, {"rssi": rssi, "snr": snr}
            time.sleep(0.001)
        raise TimeoutError(
            f"peer RADIO_RX timeout type={frame.typ:#x} id={frame.message_id}"
        )

    def finish_report(self) -> dict:
        status_end = {
            "base": parse_link_status(self.base.get_link_status().payload),
            "robot": parse_link_status(self.robot.get_link_status().payload),
        }
        diagnostics_end = {
            "base": parse_diagnostics(self.base.get_diagnostics().payload),
            "robot": parse_diagnostics(self.robot.get_diagnostics().payload),
        }
        return {
            "device_info": self.device_info,
            "status_start": {
                key: _status_dict(value) for key, value in self.status_start.items()
            },
            "status_end": {
                key: _status_dict(value) for key, value in status_end.items()
            },
            "status_delta": {
                key: _status_delta(self.status_start[key], status_end[key])
                for key in status_end
            },
            "diagnostics_delta": {
                key: _diagnostics_delta(
                    self.diagnostics_start[key], diagnostics_end[key]
                )
                for key in diagnostics_end
            },
            "host_counters": {
                "base": self.base.counters.copy(),
                "robot": self.robot.counters.copy(),
            },
            "application_frames_decoded": self.application_frames_decoded,
        }

    def close(self) -> None:
        if self.base is not None:
            self.base.close()
        if self.robot is not None:
            self.robot.close()


def _info_dict(info: tuple) -> dict:
    return {
        "device_id": info[0].hex(":"),
        "boot_id": info[1],
        "capabilities": info[2],
        "max_air_payload": info[3],
        "radio_ready": info[4],
        "profile_id": info[5],
        "uptime_ms": info[6],
    }


def _status_dict(status: tuple) -> dict:
    names = (
        "boot_id",
        "uptime_ms",
        "radio_state",
        "tx_ok",
        "rx_ok",
        "rx_bad",
        "usb_bad",
        "radio_errors",
        "usb_event_drops",
    )
    return dict(zip(names, status, strict=True))


def _status_delta(before: tuple, after: tuple) -> dict:
    names = ("tx_ok", "rx_ok", "rx_bad", "usb_bad", "radio_errors", "usb_event_drops")
    return {
        name: after[index] - before[index]
        for index, name in zip(range(3, 9), names, strict=True)
    }


def _diagnostics_delta(before: tuple, after: tuple) -> dict:
    counter_names = (
        "usb_frames",
        "usb_accepted",
        "send_accepted",
        "radio_tx_started",
        "radio_tx_completed",
        "radio_rx_received",
        "rx_event_queued",
        "rx_event_written",
        "usb_event_dropped",
    )
    result = {
        name: (after[index] - before[index]) % (2**32)
        for index, name in enumerate(counter_names)
    }
    result["usb_queue_depth_start"] = before[9]
    result["usb_queue_depth_end"] = after[9]
    return result


def _empty_traffic() -> dict:
    return {"offered": 0, "sent": 0, "delivered": 0, "dropped": 0, "bytes": 0}


class MixedRunner:
    def __init__(
        self,
        transport,
        profile: str,
        duration_s: float,
        status_interval_s: float,
        stop_interval_s: float,
        seed: int,
        rtcm_hz: float = 1.0,
    ) -> None:
        self.transport = transport
        self.profile = profile
        self.duration_s = duration_s
        self.status_interval_s = status_interval_s
        self.stop_interval_s = stop_interval_s
        self.rtcm_hz = rtcm_hz if profile == "nominal" else 0.1
        rng = random.Random(seed)
        self.base_session = rng.randrange(1, 2**32)
        self.robot_session = rng.randrange(1, 2**32)
        self.base_id = 0
        self.robot_id = 0
        self.order = 0
        self.scheduler = Scheduler()
        self.reassembler = Reassembler()
        self.stop_cache = StopCache(self.robot_session)
        self.traffic = {kind: _empty_traffic() for kind in KIND_BY_TYPE.values()}
        self.errors: list[str] = []
        self.rssi: list[float] = []
        self.snr: list[float] = []
        self.rtcm_offered = 0
        self.rtcm_completed = 0
        self.rtcm_completed_types: dict[int, int] = {}
        self.rtcm_meta: dict[tuple[int, int], tuple[int, int, bool]] = {}
        self.epoch_completed_types: dict[int, set[int]] = {}
        self.observation_epochs_offered = 0
        self.telemetry_offered = 0
        self.telemetry_delivered = 0
        self.telemetry_ages_ms: list[float] = []
        self.stop_unique = 0
        self.stop_attempts = 0
        self.stop_acks: set[tuple[int, int]] = set()
        self.stop_created: dict[tuple[int, int], float] = {}
        self.stop_latencies_ms: list[float] = []
        self.stop_duplicate_attempts = 0
        self.last_rtcm_complete_at: float | None = None
        self.next_epoch = 0.0
        self.next_telemetry = 0.0
        self.next_stop = stop_interval_s if stop_interval_s > 0 else math.inf
        self.next_status = status_interval_s if status_interval_s > 0 else math.inf
        self.epoch = 0
        self.low_duty_type_index = 0

    def _next_id(self, direction: str) -> int:
        if direction == BASE_TO_ROBOT:
            self.base_id += 1
            return self.base_id
        self.robot_id += 1
        return self.robot_id

    def _offer(
        self, frame: Frame, direction: str, created_at: float, rtcm_key=None
    ) -> None:
        self.order += 1
        item = WorkItem(frame, direction, created_at, self.order, rtcm_key)
        kind = KIND_BY_TYPE[frame.typ]
        metric = self.traffic[kind]
        metric["offered"] += 1
        metric["bytes"] += len(encode(frame))
        if not self.scheduler.push(item):
            metric["dropped"] += 1

    def _offer_hello(self, now: float) -> None:
        self._offer(
            Frame(
                HELLO,
                self.base_session,
                self._next_id(BASE_TO_ROBOT),
                0,
                encode_hello(ROLE_BASE, 0x000F),
            ),
            BASE_TO_ROBOT,
            now,
        )
        self._offer(
            Frame(
                HELLO,
                self.robot_session,
                self._next_id(ROBOT_TO_BASE),
                0,
                encode_hello(ROLE_ROBOT, 0x000F),
            ),
            ROBOT_TO_BASE,
            now,
        )

    def _offer_rtcm_raw(
        self, raw: bytes, epoch: int, observation: bool, now: float
    ) -> None:
        message_id = self._next_id(BASE_TO_ROBOT)
        key = (self.base_session, message_id)
        self.rtcm_offered += 1
        self.rtcm_meta[key] = (epoch, rtcm_type(raw), observation)
        for app_frame in fragment(raw, self.base_session, message_id):
            self._offer(app_frame, BASE_TO_ROBOT, now, key)

    def _schedule_due(self, now: float) -> None:
        if self.profile == "nominal":
            epoch_period_s = 1.0 / self.rtcm_hz
            while self.next_epoch < self.duration_s and now >= self.next_epoch:
                self.epoch += 1
                self.observation_epochs_offered += 1
                for raw in synthetic_epoch(self.epoch):
                    self._offer_rtcm_raw(
                        raw,
                        self.epoch,
                        rtcm_type(raw) in RTCM_OBSERVATION_SIZES,
                        self.next_epoch,
                    )
                self.next_epoch += epoch_period_s
            telemetry_period = 0.5
        else:
            while self.next_epoch < self.duration_s and now >= self.next_epoch:
                self.epoch += 1
                message_type = list(RTCM_OBSERVATION_SIZES)[
                    self.low_duty_type_index % 4
                ]
                self.low_duty_type_index += 1
                self._offer_rtcm_raw(
                    synthetic_rtcm(
                        message_type, RTCM_OBSERVATION_SIZES[message_type], self.epoch
                    ),
                    self.epoch,
                    False,
                    self.next_epoch,
                )
                self.next_epoch += 10.0
            telemetry_period = 2.0

        while self.next_telemetry < self.duration_s and now >= self.next_telemetry:
            rtcm_age = (
                0xFFFF
                if self.last_rtcm_complete_at is None
                else min(0xFFFF, int((now - self.last_rtcm_complete_at) * 1000))
            )
            sample = TelemetrySample(
                uptime_ms=min(0xFFFFFFFF, int(now * 1000)),
                latitude_e7=591234567,
                longitude_e7=181234567,
                speed_mm_s=321,
                battery_mv=25100,
                last_rtcm_transport_age_ms=rtcm_age,
                status_flags=(1 if self.stop_cache.state == STATE_STOPPED else 0)
                | (2 if rtcm_age < 1000 else 0),
                fix_type=4,
                satellites=27,
            )
            frame = Frame(
                TELEMETRY,
                self.robot_session,
                self._next_id(ROBOT_TO_BASE),
                0,
                encode_telemetry(sample),
            )
            self.telemetry_offered += 1
            self._offer(frame, ROBOT_TO_BASE, self.next_telemetry)
            self.next_telemetry += telemetry_period

        while self.next_stop < self.duration_s and now >= self.next_stop:
            self.stop_cache.reset_mock_state()
            frame = Frame(
                STOP_REQUEST,
                self.base_session,
                self._next_id(BASE_TO_ROBOT),
                0,
                encode_stop(StopRequestBody(self.robot_session, STOP_COMMAND, 1)),
            )
            key = (frame.sender_session, frame.message_id)
            self.stop_unique += 1
            self.stop_attempts += 1
            self.stop_created[key] = self.next_stop
            self._offer(frame, BASE_TO_ROBOT, self.next_stop)
            if self.stop_unique % 2 == 0:
                self.stop_attempts += 1
                self.stop_duplicate_attempts += 1
                self._offer(frame, BASE_TO_ROBOT, self.next_stop)
            self.next_stop += self.stop_interval_s

    def _deliver(self, item: WorkItem, received: Frame, now: float) -> None:
        kind = KIND_BY_TYPE[received.typ]
        self.traffic[kind]["delivered"] += 1
        if received.typ == HELLO:
            decode_hello(received.body)
        elif received.typ == TELEMETRY:
            decode_telemetry(received.body)
            self.telemetry_delivered += 1
            self.telemetry_ages_ms.append((now - item.created_at) * 1000)
        elif received.typ == RTCM_FRAGMENT:
            raw = self.reassembler.add(received, now)
            if raw is not None:
                self.rtcm_completed += 1
                message_type = rtcm_type(raw)
                self.rtcm_completed_types[message_type] = (
                    self.rtcm_completed_types.get(message_type, 0) + 1
                )
                key = (received.sender_session, received.message_id)
                epoch, _, observation = self.rtcm_meta[key]
                if observation:
                    self.epoch_completed_types.setdefault(epoch, set()).add(
                        message_type
                    )
                self.last_rtcm_complete_at = now
        elif received.typ == STOP_REQUEST:
            decision = self.stop_cache.apply(received)
            ack = CommandAckBody(
                received.sender_session,
                received.message_id,
                decision.result,
                decision.state,
            )
            ack_frame = Frame(
                COMMAND_ACK,
                self.robot_session,
                self._next_id(ROBOT_TO_BASE),
                0,
                encode_ack(ack),
            )
            self._offer(ack_frame, ROBOT_TO_BASE, now)
        elif received.typ == COMMAND_ACK:
            ack = decode_ack(received.body)
            key = (ack.request_session, ack.request_id)
            if key in self.stop_created and key not in self.stop_acks:
                self.stop_acks.add(key)
                self.stop_latencies_ms.append((now - self.stop_created[key]) * 1000)

    def _next_due(self) -> float:
        due = [self.next_epoch, self.next_telemetry, self.next_stop]
        return (
            min(value for value in due if value < self.duration_s)
            if any(value < self.duration_s for value in due)
            else self.duration_s
        )

    def run(self) -> dict:
        self._offer_hello(self.transport.now())
        drain_deadline = self.duration_s + 2.0
        while self.transport.now() < self.duration_s or self.scheduler.items:
            now = self.transport.now()
            if now < self.duration_s:
                self._schedule_due(now)
            item = self.scheduler.pop(now)
            if item is None:
                if now >= self.duration_s:
                    break
                self.transport.idle_until(self._next_due())
                continue
            if now > drain_deadline:
                self.errors.append("drain deadline exceeded")
                break
            age_ms = min(0xFFFF, max(0, int((now - item.created_at) * 1000)))
            frame = replace(item.frame, source_age_ms=age_ms)
            kind = KIND_BY_TYPE[frame.typ]
            self.traffic[kind]["sent"] += 1
            try:
                received, radio = self.transport.transmit(item, frame)
                self.rssi.append(radio["rssi"])
                self.snr.append(radio["snr"])
                self._deliver(item, received, self.transport.now())
            except (ApplicationError, OSError, RuntimeError, TimeoutError) as exc:
                self.errors.append(str(exc))
            if self.transport.now() >= self.next_status:
                self._print_status()
                self.next_status += self.status_interval_s

        self.reassembler.expire(self.transport.now() + 0.601)
        return self.report()

    def _print_status(self) -> None:
        print(
            "mixed_status"
            f" t={self.transport.now():.1f}s"
            f" queue={len(self.scheduler.items)}"
            f" rtcm={self.rtcm_completed}/{self.rtcm_offered}"
            f" telemetry={self.telemetry_delivered}/{self.telemetry_offered}"
            f" stop_ack={len(self.stop_acks)}/{self.stop_unique}"
            f" errors={len(self.errors)}"
        )

    def report(self) -> dict:
        completed_epochs = sum(
            RTCM_OBSERVATION_SIZES.keys() <= message_types
            for message_types in self.epoch_completed_types.values()
        )
        rtcm_ratio = (
            self.rtcm_completed / self.rtcm_offered if self.rtcm_offered else 1.0
        )
        telemetry_ratio = (
            self.telemetry_delivered / self.telemetry_offered
            if self.telemetry_offered
            else 1.0
        )
        epoch_ratio = (
            completed_epochs / self.observation_epochs_offered
            if self.observation_epochs_offered
            else 1.0
        )
        transport_report = {}
        if isinstance(self.transport, PhysicalTransport):
            transport_report = self.transport.finish_report()
        status_deltas = transport_report.get("status_delta", {})
        modem_errors = sum(
            delta.get("rx_bad", 0)
            + delta.get("usb_bad", 0)
            + delta.get("radio_errors", 0)
            + delta.get("usb_event_drops", 0)
            for delta in status_deltas.values()
        )
        passed = (
            not self.errors
            and rtcm_ratio >= 0.99
            and epoch_ratio >= 0.98
            and telemetry_ratio >= 0.99
            and len(self.stop_acks) == self.stop_unique
            and self.stop_cache.apply_count == self.stop_unique
            and modem_errors == 0
        )
        return {
            "mode": self.transport.mode,
            "profile": self.profile,
            "rtcm_hz": self.rtcm_hz,
            "duration_s": self.duration_s,
            "pass": passed,
            "traffic": self.traffic,
            "rtcm": {
                "logical_offered": self.rtcm_offered,
                "logical_completed": self.rtcm_completed,
                "completion_ratio": rtcm_ratio,
                "completed_by_type": self.rtcm_completed_types,
                "observation_epochs_offered": self.observation_epochs_offered,
                "observation_epochs_complete": completed_epochs,
                "epoch_completion_ratio": epoch_ratio,
                "reassembly_timeouts": self.reassembler.timeout_count,
                "reassembly_conflicts": self.reassembler.conflict_count,
                "stale_fragment_drops": self.scheduler.stale_rtcm_drops,
            },
            "telemetry": {
                "offered": self.telemetry_offered,
                "delivered": self.telemetry_delivered,
                "delivery_ratio": telemetry_ratio,
                "replaced": self.scheduler.telemetry_replaced,
                "age_p95_ms": percentile(self.telemetry_ages_ms, 0.95),
                "age_max_ms": max(self.telemetry_ages_ms, default=None),
            },
            "stop": {
                "unique": self.stop_unique,
                "attempts": self.stop_attempts,
                "duplicate_attempts": self.stop_duplicate_attempts,
                "applied": self.stop_cache.apply_count,
                "acked": len(self.stop_acks),
                "latency_p95_ms": percentile(self.stop_latencies_ms, 0.95),
                "latency_max_ms": max(self.stop_latencies_ms, default=None),
            },
            "scheduler": {
                "queue_high_water": self.scheduler.high_water,
                "queue_drops": self.scheduler.queue_drops,
            },
            "transport": {
                "estimated_airtime_s": self.transport.estimated_airtime_s,
                "transaction_p95_ms": percentile(
                    self.transport.transaction_times_ms, 0.95
                ),
                **transport_report,
            },
            "radio_metrics": {
                "rssi_min": min(self.rssi, default=None),
                "rssi_max": max(self.rssi, default=None),
                "snr_min": min(self.snr, default=None),
                "snr_max": max(self.snr, default=None),
            },
            "errors": self.errors,
        }


def run_fault_test() -> dict:
    session = 1
    raw = synthetic_rtcm(1077, RTCM_OBSERVATION_SIZES[1077], 1)
    pieces = fragment(raw, session, 1)
    reordered = Reassembler()
    result = None
    for piece in (pieces[2], pieces[0], pieces[0], pieces[1]):
        candidate = reordered.add(piece, 0.1)
        if candidate is not None:
            result = candidate

    missing = Reassembler()
    missing.add(pieces[0], 0.0)
    timeout_count = missing.expire(0.6)

    conflict = Reassembler()
    conflict.add(pieces[0], 0.0)
    changed = bytearray(pieces[0].body)
    changed[-1] ^= 1
    conflict_detected = False
    try:
        conflict.add(replace(pieces[0], body=bytes(changed)), 0.1)
    except ApplicationError:
        conflict_detected = True

    encoded = bytearray(encode(Frame(TELEMETRY, 1, 9, 0, bytes(24))))
    encoded[-1] ^= 1
    crc_detected = False
    try:
        decode(bytes(encoded))
    except ApplicationError:
        crc_detected = True

    next_raw = synthetic_rtcm(1087, RTCM_OBSERVATION_SIZES[1087], 2)
    recovered = None
    for piece in fragment(next_raw, session, 2):
        recovered = conflict.add(piece, 0.2)

    passed = (
        result == raw
        and reordered.duplicate_count == 1
        and timeout_count == 1
        and conflict_detected
        and crc_detected
        and recovered == next_raw
    )
    return {
        "mode": "fault-test",
        "pass": passed,
        "reordered_complete": result == raw,
        "duplicate_ignored": reordered.duplicate_count == 1,
        "missing_fragment_timed_out": timeout_count == 1,
        "conflict_detected": conflict_detected,
        "crc_detected": crc_detected,
        "next_frame_recovered": recovered == next_raw,
    }


def validate_physical_guard(profile: str, duration_s: float) -> None:
    if profile == "short-capacity" and duration_s > 20:
        raise ValueError("short-capacity physical runs are limited to 20 seconds")
    if profile == "low-duty" and duration_s > 900:
        raise ValueError("low-duty physical runs are limited to 900 seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PROTOTYPE mixed STOP/telemetry/RTCM bench; STOP is mock-only"
    )
    parser.add_argument("--mode", choices=("simulate", "physical"), default="simulate")
    parser.add_argument("--profile", choices=("nominal", "low-duty"), default="nominal")
    parser.add_argument("--duration-s", type=float, default=60)
    parser.add_argument("--status-interval", type=float, default=60)
    parser.add_argument("--stop-interval", type=float)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--rtcm-hz",
        type=float,
        default=1.0,
        help="nominal-profile synthetic RTCM epoch rate (default: 1.0 Hz)",
    )
    parser.add_argument("--fault-test", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--base-port")
    parser.add_argument("--robot-port")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.duration_s <= 0:
        parser.error("duration must be positive")
    if not math.isfinite(args.rtcm_hz) or args.rtcm_hz <= 0:
        parser.error("RTCM rate must be finite and positive")
    if args.profile != "nominal" and args.rtcm_hz != 1.0:
        parser.error("--rtcm-hz applies only to the nominal profile")
    if args.fault_test:
        report = run_fault_test()
    else:
        stop_interval = args.stop_interval or (30 if args.profile == "nominal" else 60)
        transport = None
        try:
            if args.mode == "physical":
                if not args.base_port or not args.robot_port:
                    parser.error("physical mode requires --base-port and --robot-port")
                physical_profile = (
                    "short-capacity" if args.profile == "nominal" else "low-duty"
                )
                try:
                    validate_physical_guard(physical_profile, args.duration_s)
                except ValueError as exc:
                    parser.error(str(exc))
                transport = PhysicalTransport(args.base_port, args.robot_port)
            else:
                transport = SimulatedTransport()
            runner = MixedRunner(
                transport,
                args.profile,
                args.duration_s,
                args.status_interval,
                stop_interval,
                args.seed,
                args.rtcm_hz,
            )
            report = runner.run()
        finally:
            if transport is not None:
                transport.close()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
