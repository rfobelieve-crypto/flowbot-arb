"""Latency percentiles (2026-09-04).

The engine used to have no number for how long its own orders take. That is
survivable while the only mode is --record-only and fatal once a resting
quote has to be pulled before it is picked off: the cancel round trip IS the
adverse-selection cost (LIVE_50U_SPEC M3), and it is the one latency this
strategy's regime actually cares about (HOTPATH_AUDIT: the opportunity lives
5-15 minutes, so entry latency does not matter -- getting OUT does).

**Percentiles, not averages.** An average round trip of 80 ms with a p99 of
3 seconds is a system that loses money on 1% of its quotes and looks healthy
on the dashboard. The tail is the thing being measured; the mean hides it.

Cost: one append per order on the hot path, one sort per status line off it.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, Optional, Tuple


class Latencies:
    """Round-trip milliseconds for one (venue, action), bounded."""

    def __init__(self, maxlen: int = 512) -> None:
        self.samples: deque = deque(maxlen=maxlen)
        self.worst = 0.0
        self.n = 0

    def add(self, ms: float) -> None:
        self.samples.append(ms)
        self.worst = max(self.worst, ms)
        self.n += 1

    def pct(self, p: float) -> Optional[float]:
        if not self.samples:
            return None
        xs = sorted(self.samples)
        # nearest-rank: with 20 samples a "p99" from interpolation is a
        # fiction; this returns a value that was actually observed.
        i = min(len(xs) - 1, max(0, int(round(p * len(xs) + 0.5)) - 1))
        return xs[i]

    def summary(self) -> Optional[Tuple[float, float, float]]:
        if not self.samples:
            return None
        return self.pct(0.50), self.pct(0.95), self.pct(0.99)


class LatencyBook:
    def __init__(self) -> None:
        self.by_key: Dict[str, Latencies] = {}

    def add(self, key: str, ms: float) -> None:
        lat = self.by_key.get(key)
        if lat is None:
            lat = self.by_key[key] = Latencies()
        lat.add(ms)

    def line(self, keys=None) -> str:
        """One compact status-line fragment. Empty when nothing was measured."""
        out = []
        for key in (keys or sorted(self.by_key)):
            lat = self.by_key.get(key)
            s = lat.summary() if lat else None
            if s:
                out.append(f"{key} {s[0]:.0f}/{s[1]:.0f}/{s[2]:.0f}ms"
                           f"(n{lat.n})")
        return " ".join(out)

    def snapshot(self) -> Dict[str, dict]:
        out = {}
        for key, lat in self.by_key.items():
            s = lat.summary()
            if s:
                out[key] = {"p50": s[0], "p95": s[1], "p99": s[2],
                            "max": lat.worst, "n": lat.n}
        return out
