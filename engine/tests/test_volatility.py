"""Volatility circuit breaker: the one guard nobody else's code gave us.

The properties under test are the ones the design argues for in
entropy_arb/volatility.py: a JUMP trips it where an equal drift spread over
a longer window does not, an outage is not a move, it lifts itself but never
into a market that is still running, and it stops opening WITHOUT stopping
closing.

Run:  python3 -m pytest tests/  (or  python3 tests/test_volatility.py)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.volatility import MoveMonitor, VolatilityBreaker  # noqa: E402
from test_maker import make_engine, approx, run                    # noqa: E402


# ------------------------------------------------------------- the measure

def test_range_is_peak_to_trough():
    m = MoveMonitor(window_sec=30.0)
    for t, px in ((0.0, 100.0), (1.0, 100.5), (2.0, 99.5), (3.0, 100.0)):
        m.observe(px, t)
    # 99.5 -> 100.5 is 100.5/99.5 - 1 = 100.5 bps
    assert 100.0 < m.range_bps(3.0) < 101.0


def test_old_samples_leave_the_window():
    m = MoveMonitor(window_sec=10.0)
    m.observe(100.0, 0.0)
    m.observe(110.0, 1.0)
    assert m.range_bps(1.0) > 900
    for t in range(12, 20):
        m.observe(110.0, float(t))     # the spike ages out
    approx(m.range_bps(19.0), 0.0, tol=1e-6)


def test_a_gap_is_an_outage_not_a_move():
    """The feed died and came back somewhere else. That is a fact about the
    connection, and the staleness guards already refuse to trade through it."""
    m = MoveMonitor(window_sec=10.0)
    m.observe(100.0, 0.0)
    m.observe(100.0, 1.0)
    m.observe(140.0, 300.0)            # five minutes later, 40% away
    approx(m.range_bps(300.0), 0.0, tol=1e-6)
    assert m.samples == 1


# ------------------------------------------------------------- the breaker

def brk(max_bps=40.0, window=30.0, cooldown=60.0):
    return VolatilityBreaker(window_sec=window, max_move_bps=max_bps,
                             cooldown_sec=cooldown)


def test_a_jump_trips_it():
    b = brk()
    for t, px in ((0.0, 100.0), (1.0, 100.1), (2.0, 100.9)):
        b.observe("entropy", px, t)
    reason = b.check(2.0)
    assert reason and "entropy" in reason
    assert b.paused(2.0) and b.trips == 1


def test_the_same_move_spread_out_does_not():
    """90 bps over ten minutes is a market. 90 bps in 30 seconds is a
    different market, and only one of them is dangerous to arbitrage."""
    b = brk(window=30.0)
    px = 100.0
    for i in range(60):
        px *= 1.00015                  # ~1.5 bps per step, one step per 10s
        b.observe("entropy", px, i * 10.0)
        assert b.check(i * 10.0) is None, f"tripped at step {i}"
    assert px / 100.0 > 1.0089         # it really did travel ~90 bps
    assert not b.paused(600.0)


def test_one_sample_cannot_trip_it():
    b = brk()
    b.observe("entropy", 100.0, 0.0)
    b.observe("entropy", 200.0, 1.0)   # a book that just connected
    assert b.check(1.0) is None        # below min_samples
    b.observe("entropy", 200.0, 2.0)
    assert b.check(2.0) is not None


def test_it_lifts_itself_when_the_market_calms():
    b = brk(cooldown=60.0)
    for t, px in ((0.0, 100.0), (1.0, 100.2), (2.0, 100.9)):
        b.observe("entropy", px, t)
    assert b.check(2.0)
    assert b.paused(30.0)              # still inside the cooldown
    for t in range(40, 100, 5):        # quiet, and the spike ages out
        b.observe("entropy", 100.9, float(t))
    assert not b.paused(95.0)
    assert b.trips == 1


def test_it_does_not_lift_into_a_move_that_never_stopped():
    b = brk(cooldown=10.0, window=30.0)
    px = 100.0
    for i in range(20):
        px *= 1.0005                   # 5 bps every 2 seconds, relentless
        b.observe("entropy", px, i * 2.0)
        b.check(i * 2.0)
    assert b.paused(38.0), "reopened on a timer while the market was running"


def test_disabled_never_pauses():
    b = VolatilityBreaker(window_sec=30.0, max_move_bps=0.0, cooldown_sec=60.0)
    for t, px in ((0.0, 100.0), (1.0, 200.0), (2.0, 50.0)):
        b.observe("entropy", px, t)
    assert b.check(2.0) is None and not b.paused(2.0)


def test_the_worst_venue_wins():
    b = brk(max_bps=40.0)
    for t in range(4):
        b.observe("entropy", 100.0, float(t))
        b.observe("hedge", 100.0 + t * 0.2, float(t))
    reason = b.check(3.0)
    assert reason and "hedge" in reason


# ------------------------------------------------------- engine behaviour

def _storm(eng, at=None):
    """Push a trip-worthy move through the engine's own breaker."""
    import time
    now = at or time.time()
    for i, px in enumerate((100.0, 100.2, 101.0)):
        eng.vol.observe("entropy", px, now - 2 + i)
    assert eng.vol.check(now) is not None
    return now


def test_paused_engine_opens_nothing():
    eng = make_engine(vol_max_move_bps=40.0, vol_cooldown_sec=60.0)
    now = _storm(eng)
    assert eng.vol.paused(now)

    async def go():
        await eng._evaluate()
    run(go())
    assert not eng.entropy.sent_makers, "quoted into a storm"
    assert eng.maker_posts == 0


def test_paused_engine_still_flattens():
    """Refusing to open during a storm while also refusing to close is not
    caution, it is the worst of both."""
    eng = make_engine(vol_max_move_bps=40.0, max_net_base=1e6)
    now = _storm(eng)
    assert eng.vol.paused(now)
    eng.entropy.position = 1.0            # one naked LONG leg
    run(eng._maybe_hedge())
    assert eng.entropy.sent_takers, "the breaker blocked a flatten"
    is_buy, qty, _ = eng.entropy.sent_takers[0]
    assert is_buy is False and abs(qty - 1.0) < 1e-9   # sold to flatten


def test_a_storm_pulls_a_resting_quote():
    eng = make_engine(vol_max_move_bps=40.0, maker_timeout_sec=30.0)

    def hook(eng, m, t, p, o):
        def on_poll(v, n):
            if n == 1:
                _storm(eng)
        m.on_poll = on_poll
        m.on_cancel = lambda v: setattr(v, "ex_status", "canceled")
    from test_maker import _drive
    p, order = _drive(eng, hook)
    assert eng.entropy.cancels, "a quote was left resting through a storm"
    assert "volatility breaker" in order.stats.get("cancel_reason", "")


def test_breaker_is_required_before_live():
    eng = make_engine(vol_max_move_bps=0.0)
    try:
        eng._require_armed_risk_block()
    except RuntimeError as e:
        assert "vol_max_move_bps" in str(e)
        return
    raise AssertionError("live trading started with the breaker disarmed")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:52s} OK")
