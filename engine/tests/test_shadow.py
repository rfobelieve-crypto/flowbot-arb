"""Shadow mode: the whole strategy, nothing sent.

The property that matters is negative and therefore easy to lose: no path
may put an order on an exchange. There are six of them (arb, net-delta
hedge, flatten, maker quote, maker-fill hedge, and the cancel that follows a
quote), so each is asserted separately rather than trusting one flag.

It is NOT a paper mode. README: "there is no simulated-fill mode ... validate
with recorded data and tiny position caps, not with simulated fills." Nothing
here invents a fill, a position or a PnL -- the tests below check that too.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.engine import Engine                       # noqa: E402
from test_maker import FakeVenue, approx, make_cfg, run     # noqa: E402


def shadow_engine(**over):
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    eng = Engine(make_cfg(**over), shadow=True)
    eng.entropy = FakeVenue("entropy", "ENTROPY")
    eng.hedge = FakeVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 1.0
    eng.entropy.set_book(100.00, 100.20)
    eng.hedge.set_book(99.90, 100.00)
    return eng


def sent(eng):
    return (eng.entropy.sent_makers + eng.entropy.sent_takers
            + eng.entropy.cancels + eng.hedge.sent_makers
            + eng.hedge.sent_takers + eng.hedge.cancels)


def test_a_maker_quote_is_decided_but_not_sent():
    eng = shadow_engine()

    async def go():
        eng._scan_maker(__import__("time").time())
        await eng._evaluate()
    run(go())
    assert eng.shadow_decisions >= 1, "the strategy never reached a decision"
    assert sent(eng) == [], f"shadow sent something: {sent(eng)}"
    assert not eng._maker_open, "a quote that was never sent stayed open"


def test_a_taker_execution_is_decided_but_not_sent():
    eng = shadow_engine(mode="taker")
    # the maker path can post inside the spread; a taker pair needs the books
    # to actually cross
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)

    async def go():
        eng._scan(__import__("time").time())      # first pass arms
        await eng._evaluate()
    run(go())
    assert eng.shadow_decisions >= 1
    assert sent(eng) == []


def test_the_net_delta_hedge_is_not_sent():
    eng = shadow_engine(max_net_base=1e6)
    eng.entropy.position = 1.0
    run(eng._maybe_hedge())
    assert eng.shadow_decisions >= 1
    assert sent(eng) == []


def test_the_flatten_is_not_sent():
    eng = shadow_engine()
    eng.entropy.position = 1.0
    run(eng._flatten_step())
    assert eng.shadow_decisions >= 1
    assert sent(eng) == []


def test_no_position_is_invented():
    """The line between shadow and a paper mode: a decision is recorded, a
    fill is not imagined."""
    eng = shadow_engine()

    async def go():
        eng._scan_maker(__import__("time").time())
        await eng._evaluate()
    run(go())
    approx(eng.entropy.position, 0.0)
    approx(eng.hedge.position, 0.0)
    approx(eng.entropy.cash, 0.0)
    approx(eng.total_fill_edge, 0.0)
    assert eng.trades == 0, "shadow claimed a trade it never made"


def test_shadow_needs_no_signer():
    """Venues answer ready_to_trade() only once a signer exists; shadow needs
    books, not keys."""
    eng = shadow_engine()
    eng.entropy.ready_to_trade = lambda: False
    eng.hedge.ready_to_trade = lambda: False
    assert eng._ready(eng.entropy) and eng._ready(eng.hedge)

    async def go():
        import time
        eng._scan_maker(time.time())              # first pass arms
        return eng._scan_maker(time.time())
    assert run(go()) is not None, "no signer, but the strategy still ran"


def test_live_engine_still_sends():
    """The negative property must not be achieved by breaking the positive one."""
    from test_maker import make_engine, _drive
    eng = make_engine()

    def hook(eng, m, t, p, o):
        m.on_cancel = lambda v: setattr(v, "ex_status", "canceled")
    _drive(eng, hook)
    assert eng.entropy.sent_makers, "the non-shadow engine stopped sending"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:52s} OK")
