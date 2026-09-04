"""Volatility circuit breaker (2026-09-04).

The one guard this project admitted it was missing, and the one the research
into other people's infrastructure did NOT hand us: Hummingbot has no
equivalent, so the design below is ours and the reasoning is written down
here rather than assumed.

**What it protects against.** Every other risk switch in this engine reacts
to something that has already gone wrong -- the legs drifted, the book went
stale, the session lost money. This one reacts to a market the strategy's
own assumptions stop being true in. The assumptions are: the two books
describe the same thing, a hedge sent now fills near the price we just read,
and a premium outside the measured band means an opportunity. During a fast
move all three fail at once, and they fail in a correlated way -- which is
exactly when a per-trade check is least able to see it.

**Why peak-to-trough range and not standard deviation.** What costs money is
the distance the price travelled between reading the book and the second leg
filling. A single jump inside an otherwise quiet window is the dangerous
shape, and it is the shape a standard deviation averages away. The range
over a short window is also the number an operator can reason about: "40 bps
in 30 seconds" is a sentence about the market, not about a formula.

**Why a PAUSE and not a HALT.** Every other switch here is one-way on
purpose: a halt needs a human and a restart, because the conditions that
trip it do not fix themselves. Volatility does fix itself. A breaker that
took the bot down for the day on every news spike would be turned off within
a week, and a switch that gets turned off is worse than one that is slightly
too lenient. So this one pauses NEW exposure and lifts itself once the
market has been calm for a cooldown.

**What it never stops.** Hedging, flattening, self-rescue and reconcile.
Refusing to open during a storm while also refusing to close is not caution,
it is the worst of both.

No I/O, no clock of its own: callers pass `now`.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, Optional, Tuple


class MoveMonitor:
    """Peak-to-trough range of one price over a rolling time window.

    O(1) amortised per sample (two monotonic deques), because this runs on
    every book update -- see HOTPATH_AUDIT.md for why that matters here.
    """

    def __init__(self, window_sec: float) -> None:
        self.window_sec = window_sec
        self._pts: deque = deque()      # (ts, px), in arrival order
        self._hi: deque = deque()       # (ts, px), decreasing px
        self._lo: deque = deque()       # (ts, px), increasing px
        self.last_ts = 0.0

    def clear(self) -> None:
        self._pts.clear()
        self._hi.clear()
        self._lo.clear()

    def observe(self, px: float, now: float) -> None:
        if px is None or px <= 0:
            return
        # A gap longer than the window is an OUTAGE, not a move. Measuring
        # across it would report how far the market travelled while we were
        # blind, which is a fact about our connection, not about volatility
        # -- and the staleness guards already refuse to trade through it.
        if self.last_ts and now - self.last_ts > self.window_sec:
            self.clear()
        self.last_ts = now
        self._pts.append((now, px))
        while self._hi and self._hi[-1][1] <= px:
            self._hi.pop()
        self._hi.append((now, px))
        while self._lo and self._lo[-1][1] >= px:
            self._lo.pop()
        self._lo.append((now, px))
        self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_sec
        for dq in (self._pts, self._hi, self._lo):
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    def range_bps(self, now: Optional[float] = None) -> float:
        """Peak-to-trough of the window, in bps of the trough."""
        if now is not None:
            self._evict(now)
        if not self._hi or not self._lo:
            return 0.0
        hi, lo = self._hi[0][1], self._lo[0][1]
        if lo <= 0:
            return 0.0
        return (hi / lo - 1.0) * 1e4

    @property
    def samples(self) -> int:
        return len(self._pts)


class VolatilityBreaker:
    """Pauses NEW exposure while any watched price is moving too fast.

    Trip:   range over `window_sec` exceeds `max_move_bps`.
    Resume: `cooldown_sec` after the last trip, and only once every watched
            range is back under the threshold. Both conditions, because a
            timer alone reopens into a move that never stopped.
    """

    def __init__(self, window_sec: float, max_move_bps: float,
                 cooldown_sec: float) -> None:
        self.window_sec = window_sec
        self.max_move_bps = max_move_bps
        self.cooldown_sec = cooldown_sec
        self.monitors: Dict[str, MoveMonitor] = {}
        self.paused_until = 0.0
        self.trips = 0
        self.reason = ""
        # smallest number of samples that can describe a range at all; below
        # it a "move" is one tick of a book we have only just connected to
        self.min_samples = 3

    @property
    def enabled(self) -> bool:
        return self.max_move_bps > 0

    def observe(self, key: str, px: Optional[float], now: float) -> None:
        if not self.enabled or px is None:
            return
        m = self.monitors.get(key)
        if m is None:
            m = self.monitors[key] = MoveMonitor(self.window_sec)
        m.observe(px, now)

    def worst(self, now: float) -> Tuple[Optional[str], float]:
        worst_key, worst_bps = None, 0.0
        for key, m in self.monitors.items():
            if m.samples < self.min_samples:
                continue
            r = m.range_bps(now)
            if r > worst_bps:
                worst_key, worst_bps = key, r
        return worst_key, worst_bps

    def check(self, now: float) -> Optional[str]:
        """Trip if anything is moving too fast. Returns a reason on the
        transition into paused, None otherwise."""
        if not self.enabled:
            return None
        key, bps = self.worst(now)
        if bps <= self.max_move_bps:
            return None
        was_paused = self.paused(now)
        self.paused_until = now + self.cooldown_sec
        self.reason = (f"{key} moved {bps:.1f} bps within "
                       f"{self.window_sec:.0f}s (limit {self.max_move_bps:.1f})")
        if was_paused:
            return None                    # already paused; just extended
        self.trips += 1
        return self.reason

    def paused(self, now: float) -> bool:
        if not self.enabled or not self.paused_until:
            return False
        if now < self.paused_until:
            return True
        # The cooldown has elapsed, but a timer alone would reopen into a
        # move that never stopped. Resume only into a calm market.
        _, bps = self.worst(now)
        if bps > self.max_move_bps:
            self.paused_until = now + self.cooldown_sec
            return True
        self.paused_until = 0.0
        self.reason = ""
        return False

    def remaining(self, now: float) -> float:
        return max(self.paused_until - now, 0.0)
