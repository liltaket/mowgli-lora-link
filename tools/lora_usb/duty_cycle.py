"""Conservative sliding-window airtime budget, as a reference for firmware.

The budget answers one question before each transmission: could this packet
make the cumulative on-air time inside *any* window of ``window_s`` exceed
``budget_s``?  Usage is kept in fixed time buckets so firmware can hold it in a
small array.  Every approximation errs towards refusing a transmission:

* a packet counts in full in each window it merely touches;
* a bucket is kept until no window that could overlap any packet in it remains;
* usage from before a restart is either assumed to be the whole budget at the
  instant of boot (``prior="full"``) or restored from a snapshot that is
  re-anchored as if the shutdown happened at boot (``prior="snapshot"``).

``prior="none"`` exists only for simulations; it cannot know about airtime
used before the process started and therefore proves nothing about a band.
"""

from __future__ import annotations

import math
from collections import OrderedDict


class AirtimeBudget:
    def __init__(
        self,
        budget_s: float,
        window_s: float = 3600.0,
        bucket_s: float = 60.0,
        *,
        now: float = 0.0,
        max_packet_s: float = 4.0,
        prior: str = "full",
        snapshot: list[float] | None = None,
        unsaved_margin_s: float = 0.0,
    ) -> None:
        if budget_s <= 0 or window_s <= 0 or bucket_s <= 0 or max_packet_s < 0:
            raise ValueError("budget, window and bucket must be positive")
        if prior not in ("full", "snapshot", "none"):
            raise ValueError("prior must be full, snapshot or none")
        if prior == "snapshot" and snapshot is None:
            raise ValueError("snapshot prior requires a snapshot")
        self.budget_s = budget_s
        self.window_s = window_s
        self.bucket_s = bucket_s
        self.max_packet_s = max_packet_s
        self.used: OrderedDict[int, float] = OrderedDict()
        self.refused = 0
        boot_bucket = self._bucket(now)
        if prior == "full":
            self._charge(boot_bucket, budget_s)
        elif prior == "snapshot":
            # snapshot[-1] is the shutdown bucket.  Re-anchor it on the boot
            # bucket: the real off time is unknown, so assume it was zero.
            for offset, seconds in enumerate(reversed(snapshot)):
                if seconds > 0:
                    self._charge(boot_bucket - offset, seconds)
            if unsaved_margin_s > 0:
                self._charge(boot_bucket, unsaved_margin_s)

    def _bucket(self, t: float) -> int:
        return math.floor(t / self.bucket_s)

    def _charge(self, bucket: int, seconds: float) -> None:
        self.used[bucket] = self.used.get(bucket, 0.0) + seconds
        self.used.move_to_end(bucket)

    def _expire(self, now: float) -> None:
        # A packet starting in bucket b ends before (b + 1) * bucket_s +
        # max_packet_s.  Any window touching it starts before that instant.
        while self.used:
            bucket = next(iter(self.used))
            if (bucket + 1) * self.bucket_s + self.max_packet_s + self.window_s > now:
                break
            self.used.popitem(last=False)

    def used_s(self, now: float) -> float:
        self._expire(now)
        return sum(self.used.values())

    def available_s(self, now: float) -> float:
        return max(0.0, self.budget_s - self.used_s(now))

    def try_transmit(self, now: float, duration_s: float) -> bool:
        if duration_s < 0 or duration_s > self.max_packet_s:
            raise ValueError("packet duration outside 0..max_packet_s")
        if self.used_s(now) + duration_s > self.budget_s + 1e-12:
            self.refused += 1
            return False
        self._charge(self._bucket(now), duration_s)
        return True

    def snapshot(self, now: float) -> list[float]:
        """Per-bucket usage up to the bucket holding ``now``, oldest first."""
        self._expire(now)
        last = self._bucket(now)
        if not self.used:
            return []
        first = next(iter(self.used))
        return [self.used.get(b, 0.0) for b in range(first, last + 1)]


def max_window_usage(
    transmissions: list[tuple[float, float]], window_s: float = 3600.0
) -> float:
    """Exact worst cumulative on-air time over all windows of ``window_s``.

    The maximum of the sliding overlap is reached when a window starts at a
    transmission start or ends at a transmission end, so only those are tried.
    """
    starts = [start for start, _ in transmissions]
    ends = [start + duration - window_s for start, duration in transmissions]
    best = 0.0
    for left in starts + ends:
        right = left + window_s
        total = 0.0
        for start, duration in transmissions:
            total += max(0.0, min(right, start + duration) - max(left, start))
        best = max(best, total)
    return best
