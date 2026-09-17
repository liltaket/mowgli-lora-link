"""Record, inspect, and replay genuine RTCM3 bytes without fabricating captures."""

from __future__ import annotations

import argparse
import difflib
import json
import math
import struct
import sys
import time
from collections import Counter
from pathlib import Path

from .rtcm import RtcmStreamParser

MAGIC = b"MLR1"


def _records(path: Path):
    with path.open("rb") as source:
        if source.read(4) != MAGIC:
            raise ValueError("not a Mowgli LoRa RTCM recording")
        while header := source.read(10):
            if len(header) != 10:
                raise ValueError("truncated recording header")
            timestamp_ns, length = struct.unpack(">QH", header)
            raw = source.read(length)
            if len(raw) != length:
                raise ValueError("truncated recording frame")
            yield timestamp_ns, raw


def inspect_records(records: list[tuple[int | None, bytes]]) -> dict[str, object]:
    parser, sizes, types = RtcmStreamParser(), [], Counter()
    timed_samples: list[tuple[int, int]] = []
    for timestamp, raw in records:
        parsed = parser.feed(raw)
        for frame in parsed:
            sizes.append(len(frame.raw))
            types[frame.message_type] += 1
            if timestamp is not None:
                timed_samples.append((timestamp, len(frame.raw)))
    timestamps = [timestamp for timestamp, _ in timed_samples]
    timing = bool(timestamps)
    duration = (
        (timestamps[-1] - timestamps[0]) / 1_000_000_000 if len(timestamps) > 1 else 0.0
    )
    if timing and duration > 0:
        buckets: Counter[int] = Counter()
        origin = timestamps[0]
        for timestamp, size in timed_samples:
            buckets[(timestamp - origin) // 1_000_000_000] += size
        average_rate: float | None = sum(sizes) / duration
        peak_rate: int | None = max(buckets.values(), default=0)
    else:
        average_rate, peak_rate = (0.0, sum(sizes)) if timing else (None, None)
    return {
        "frames": len(sizes),
        "bytes": sum(sizes),
        "message_types": dict(sorted(types.items())),
        "average_frame_size": sum(sizes) / len(sizes) if sizes else 0.0,
        "max_frame_size": max(sizes, default=0),
        "crc_failures": parser.stats["crc_failures"],
        "timing_available": timing,
        "duration_seconds": duration if timing else None,
        "average_bytes_per_second": average_rate,
        "peak_bytes_per_second": peak_rate,
    }


def _load_frames(path: Path, recording: bool) -> tuple[list[bytes], int]:
    if recording:
        records = list(_records(path))
        return [raw for _, raw in records], 0
    parser = RtcmStreamParser()
    frames = [frame.raw for frame in parser.feed(path.read_bytes())]
    return frames, parser.stats["crc_failures"]


def compare_frames(source: list[bytes], received: list[bytes]) -> dict[str, object]:
    matcher = difflib.SequenceMatcher(None, source, received, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    source_count = len(source)
    return {
        "source_frames": source_count,
        "received_frames": len(received),
        "matched_frames_in_order": matched,
        "missing_frames": source_count - matched,
        "unexpected_frames": len(received) - matched,
        "delivery_ratio": matched / source_count if source_count else 1.0,
        "byte_exact_ordered_match": source == received,
    }


def main_record() -> None:
    parser = argparse.ArgumentParser(
        description="Record validated RTCM3 frames from stdin"
    )
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rtcm = RtcmStreamParser()
    with args.output.open("wb") as target:
        target.write(MAGIC)
        while chunk := sys.stdin.buffer.read1(4096):
            for frame in rtcm.feed(chunk):
                target.write(
                    struct.pack(">QH", time.time_ns(), len(frame.raw)) + frame.raw
                )


def main_inspect() -> None:
    parser = argparse.ArgumentParser(description="Inspect MLR1 recording or raw RTCM3")
    parser.add_argument("input", type=Path)
    parser.add_argument("--recording", action="store_true")
    args = parser.parse_args()
    records = (
        list(_records(args.input))
        if args.recording
        else [(None, args.input.read_bytes())]
    )
    print(json.dumps(inspect_records(records), sort_keys=True))


def main_compare() -> None:
    parser = argparse.ArgumentParser(
        description="Compare source and delivered RTCM3 frame streams"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("received", type=Path)
    parser.add_argument("--source-recording", action="store_true")
    parser.add_argument("--received-recording", action="store_true")
    args = parser.parse_args()
    source, source_crc_failures = _load_frames(args.source, args.source_recording)
    received, received_crc_failures = _load_frames(
        args.received, args.received_recording
    )
    report = compare_frames(source, received)
    report["source_crc_failures"] = source_crc_failures
    report["received_crc_failures"] = received_crc_failures
    print(json.dumps(report, sort_keys=True))
    if (
        source_crc_failures
        or received_crc_failures
        or not report["byte_exact_ordered_match"]
    ):
        raise SystemExit(2)


def main_replay() -> None:
    parser = argparse.ArgumentParser(description="Replay MLR1 RTCM recording to stdout")
    parser.add_argument("input", type=Path)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--drop-every", type=int, default=0)
    args = parser.parse_args()
    if not math.isfinite(args.speed) or args.speed <= 0:
        raise SystemExit("--speed must be finite and positive")
    if args.drop_every < 0:
        raise SystemExit("--drop-every must be non-negative")
    previous = None
    for index, (timestamp, raw) in enumerate(_records(args.input), 1):
        if previous is not None:
            time.sleep(max(0, timestamp - previous) / 1_000_000_000 / args.speed)
        previous = timestamp
        if not args.drop_every or index % args.drop_every:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
