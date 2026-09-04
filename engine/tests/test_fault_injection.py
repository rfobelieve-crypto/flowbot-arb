"""Fault injection: make the outside world misbehave on purpose.

Every other test here asks "does the strategy compute the right thing". These
ask the question that actually costs money: **when the exchange lies, stalls,
or disappears, what does the engine do next?** The list is the one the
industry guide names (`docs/HFT_INDUSTRY_GUIDE.md` S13.1: 斷網、逾時、拒單、
延遲抖動), read against this project's own failure judgement -- the $1.1M
shape is "assume it did not fill, send it again", so every case below asserts
that the engine did NOT do that.

The percentile tests belong here too: an average round trip of 80 ms with a
p99 of 3 s is a system that loses money on 1% of its quotes and looks healthy.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb import maker as mk                              # noqa: E402
from entropy_arb.metrics import LatencyBook                      # noqa: E402
from test_maker import _drive, approx, make_engine, run          # noqa: E402


def taker_engine(**over):
    eng = make_engine(mode="taker", **over)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    return eng


def plan_taker(eng):
    # _scan schedules a poke on the running loop, so it has to be called
    # from inside one (first pass arms the direction, second fires)
    async def go():
        import time
        eng._scan(time.time())
        return eng._scan(time.time())
    return run(go())


# ------------------------------------------------------------- 逾時 / 未決

def test_an_unresolved_send_never_becomes_a_resend():
    """The $1.1M shape. A leg whose outcome is unknown must escalate to a
    position read, never to another order."""
    eng = taker_engine()
    eng.hedge.taker_result = {"status": "timeout", "filled_base": 0.0,
                              "avg_px": None, "err": None, "unresolved": True}
    best = plan_taker(eng)
    assert best is not None
    buy, sell, plan = best

    async def go():
        return await eng._execute(buy, sell, plan)
    unresolved = run(go())
    assert unresolved is True, "an unknown outcome was treated as resolved"
    sends = eng.hedge.sent_takers
    assert len(sends) == 1, f"the engine re-sent an unresolved leg: {sends}"


def test_an_unresolved_outcome_escalates_to_reconcile():
    eng = taker_engine()
    eng.entropy.taker_result = {"status": "timeout", "filled_base": 0.0,
                                "avg_px": None, "err": None, "unresolved": True}
    buy, sell, plan = plan_taker(eng)

    async def go():
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, plan)
    run(go())
    assert eng._reconcile_evt.is_set(), "no position read after an unknown fill"


# ------------------------------------------------------------------- 拒單

def test_repeated_rejections_halt_the_engine():
    eng = taker_engine(max_net_base=1e6)
    eng.entropy.taker_result = {"status": "send-failed", "filled_base": 0.0,
                                "avg_px": None, "err": "boom",
                                "unresolved": False}
    buy, sell, plan = plan_taker(eng)

    async def go():
        for _ in range(eng.cfg.max_consecutive_errors):
            await eng._execute(buy, sell, plan)
    run(go())
    assert eng.halted, "consecutive rejections did not halt the engine"


def test_a_rate_limit_pauses_the_venue_instead_of_halting():
    """429 is the venue asking for room, not the engine failing."""
    eng = taker_engine(max_net_base=1e6)
    eng.entropy.taker_result = {"status": "send-failed", "filled_base": 0.0,
                                "avg_px": None,
                                "err": "RATE_LIMITED: HTTP 429",
                                "unresolved": False}
    buy, sell, plan = plan_taker(eng)

    async def go():
        for _ in range(eng.cfg.max_consecutive_errors + 2):
            await eng._execute(buy, sell, plan)
    run(go())
    assert not eng.halted, "a rate limit halted the engine"
    assert eng._venue_limited(eng.entropy), "the venue was not paused"


# --------------------------------------------------------------- 斷網 / 過期

def test_a_stale_book_stops_new_exposure():
    eng = taker_engine()
    eng.entropy.book.alive_ts = 0.0
    assert plan_taker(eng) is None, "planned a trade on a stale book"


def test_books_stale_for_long_enough_halt():
    eng = taker_engine()
    eng.cfg.max_consecutive_stale = 3
    eng.entropy.book.alive_ts = 0.0

    async def go():
        import time
        for _ in range(4):
            eng._scan(time.time())
    run(go())
    assert eng.halted, "a permanently dead feed still looked like a quiet market"


def test_a_down_venue_is_not_hedged_into():
    """Unreachable is not the same as flat: do not send price-protected
    orders to a venue that is not answering."""
    eng = taker_engine(max_net_base=1e6)
    eng.entropy.position = 1.0
    eng._venue_down["entropy"] = 1.0
    run(eng._maybe_hedge())
    assert not eng.entropy.sent_takers


def test_a_venue_that_raises_is_contained():
    """An adapter that throws must not take the strategy loop with it."""
    eng = taker_engine()

    async def boom(**kw):
        raise ConnectionResetError("socket died")
    eng.hedge.send_taker = boom
    buy, sell, plan = plan_taker(eng)

    async def go():
        return await eng._execute(buy, sell, plan)
    run(go())            # must not raise
    assert eng.consec_errors >= 1


# ------------------------------------------------------- 一腿成交、對沖失敗

def test_a_failed_hedge_leaves_the_imbalance_to_the_net_delta_path():
    """Never retry a hedge inline: that is how a failing leg becomes a loop."""
    eng = taker_engine(max_net_base=1e6)
    eng.entropy.position = 1.0
    eng.entropy.taker_result = {"status": "send-failed", "filled_base": 0.0,
                                "avg_px": None, "err": "boom",
                                "unresolved": False}
    run(eng._maybe_hedge())
    assert len(eng.entropy.sent_takers) == 1, "the hedge was retried inline"
    assert eng._reconcile_evt.is_set()


def test_an_imbalance_past_the_cap_halts_and_still_flattens():
    eng = taker_engine()          # max_net_base 0.003
    eng.entropy.position = 1.0
    run(eng._maybe_hedge())
    assert eng.halted, "a naked leg past the cap did not halt"
    run(eng._maybe_hedge())       # halted engines still self-rescue
    assert eng.entropy.sent_takers, "the halt froze the position instead of "\
                                    "unwinding it"


# ----------------------------------------------------------------- 延遲抖動

def test_percentiles_expose_a_tail_an_average_would_hide():
    lat = LatencyBook()
    for _ in range(99):
        lat.add("cancel", 80.0)
    lat.add("cancel", 3000.0)      # one quote in a hundred is picked off
    s = lat.by_key["cancel"].summary()
    approx(s[0], 80.0)             # p50 calm
    assert s[2] >= 3000.0, f"p99 hid the tail: {s}"
    mean = sum(lat.by_key["cancel"].samples) / 100
    assert mean < 120, "the average really does look fine, which is the point"


def test_latency_is_recorded_for_the_cancel_round_trip():
    """The cancel is the latency this strategy pays for (M3)."""
    eng = make_engine(maker_timeout_sec=0.05)

    def hook(eng, m, t, p, o):
        m.on_cancel = lambda v: setattr(v, "ex_status", "canceled")
    _drive(eng, hook)
    assert "cancel" in eng.lat.by_key, "no cancel latency was measured"
    assert eng.lat.by_key["cancel"].n >= 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:52s} OK")
