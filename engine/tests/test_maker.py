"""B3 maker path: the resting half of execution.

The four scenarios PEER_INFRA.md S7 names as the deliverable -- post->fill,
post->timeout->cancel, post->partial fill, and post->cancel LOSES the race and
it fills anyway -- are `test_quote_*` below. Each of them is a place where
Hummingbot's XEMM executor loses a hedge leg or a position; the assertions say
what must happen instead.

Run:  python3 -m pytest tests/  (or  python3 tests/test_maker.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import maker as mk                            # noqa: E402
from entropy_arb.book import OrderBook, plan_maker             # noqa: E402
from entropy_arb.config import load_config                     # noqa: E402
from entropy_arb.engine import Engine                          # noqa: E402
from entropy_arb.maker import MakerOrder                       # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


# --------------------------------------------------------------- fake venue

class FakeVenue:
    """A venue whose exchange side the test moves by hand.

    `ex_status` / `ex_filled` are what the exchange would report; the tests
    change them between polls to script a race.
    """

    kind = "fake"

    def __init__(self, key, label, fee=0.0, maker_fee=0.0, cap=1e6,
                 min_quote=1.0):
        self.key, self.name = key, label
        self.fee_bps, self.maker_fee_bps = fee, maker_fee
        self.cap_usd = cap
        self.orders_per_min = 60
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, min_quote
        self.position = self.cash = self.volume_usd = 0.0
        self.chain_position = None   # set to diverge from the local view
        self.equity = self.free = self.start_equity = None
        self.last_traded_ts = 0.0
        self.book = OrderBook()
        # what the exchange currently says about our resting order
        self.ex_status = "open"
        self.ex_filled = 0.0
        self.poll_count = 0
        self.on_poll = None          # (venue, poll_count) -> None
        self.on_cancel = None        # (venue) -> None
        self.poll_answers = True     # False = the exchange tells us nothing
        self.send_maker_result = None
        self.cancel_result = {"status": "accepted", "err": None}
        self.taker_result = None
        self.sent_makers, self.sent_takers, self.cancels = [], [], []

    # -- market data -------------------------------------------------------
    def set_book(self, bid, ask, sz=100.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])

    def ready_to_trade(self):
        return True

    def px_round(self, px, round_up):
        f = 100.0
        import math
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    def px_tick(self, px):
        return 0.01

    # -- orders ------------------------------------------------------------
    async def send_maker(self, *, is_buy, qty, limit_px, reduce_only=False):
        self.sent_makers.append((is_buy, qty, limit_px))
        if self.send_maker_result is not None:
            r = dict(self.send_maker_result)
            r.setdefault("handle", 1)
            r.setdefault("filled_base", 0.0)
            r.setdefault("avg_px", None)
            r.setdefault("err", None)
            r.setdefault("unresolved", False)
            return r
        return {"status": "resting", "handle": 1, "filled_base": 0.0,
                "avg_px": None, "err": None, "unresolved": False}

    async def poll_order(self, handle):
        self.poll_count += 1
        if self.on_poll:
            self.on_poll(self, self.poll_count)
        if not self.poll_answers:
            return {"status": "unknown", "filled_base": None, "avg_px": None,
                    "terminal": False, "err": None}
        return {"status": self.ex_status, "filled_base": self.ex_filled,
                "avg_px": None, "terminal": self.ex_status != "open",
                "err": None}

    async def cancel_order(self, handle):
        self.cancels.append(handle)
        if self.on_cancel:
            self.on_cancel(self)
        return dict(self.cancel_result)

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        self.sent_takers.append((is_buy, qty, limit_px))
        if self.taker_result is not None:
            return dict(self.taker_result)
        # a real IOC fills at the BOOK, not at the slippage bound we sent
        top = self.book.best_ask() if is_buy else self.book.best_bid()
        return {"status": "filled", "filled_base": qty,
                "avg_px": top if top else limit_px,
                "err": None, "unresolved": False}

    async def cancel_open_orders(self):
        return 0

    async def fetch_position(self):
        return (self.chain_position if self.chain_position is not None
                else self.position)


# ------------------------------------------------------------------ fixture

def make_cfg(**over):
    body = {
        "midline_bps": 0.0, "upper_bps": 2.0, "lower_bps": 2.0,
        "mode": "maker", "maker_timeout_sec": 0.15, "cancel_timeout_sec": 0.1,
        "maker_poll_sec": 0.01, "max_net_base": 0.003,
        "vol_window_sec": 30.0, "vol_max_move_bps": 0.0,
        "vol_cooldown_sec": 60.0,
    }
    body.update(over)
    # never write execution logs into the repo's own logs/ directory: the
    # recorders live there and a test run must not leave rows in them
    body["logdir"] = tempfile.mkdtemp(prefix="arb-test-").replace("\\", "/")
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {body['midline_bps']}
  upper_bps: {body['upper_bps']}
  lower_bps: {body['lower_bps']}
execution:
  mode: {body['mode']}
  maker_venue: entropy
  maker_timeout_sec: {body['maker_timeout_sec']}
  cancel_timeout_sec: {body['cancel_timeout_sec']}
  maker_poll_sec: {body['maker_poll_sec']}
  premium_persist_sec: 0.0
  net_tolerance_base: 0.001
sizing:
  min_order_notional_usd: 1.0
  max_order_notional_usd: 500.0
logging:
  trades_csv: {body['logdir']}/trades.csv
  maker_csv: {body['logdir']}/maker.csv
risk:
  max_net_base: {body['max_net_base']}
  vol_window_sec: {body['vol_window_sec']}
  vol_max_move_bps: {body['vol_max_move_bps']}
  vol_cooldown_sec: {body['vol_cooldown_sec']}
""")
    f.close()
    return load_config(f.name, NO_ENV, symbol="SNDK", hedge_venue="lighter-rh")


def make_engine(maker_min_quote=1.0, hedge_min_quote=1.0, **over):
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    eng = Engine(make_cfg(**over))
    eng.entropy = FakeVenue("entropy", "ENTROPY", min_quote=maker_min_quote)
    eng.hedge = FakeVenue("hedge", "RH", min_quote=hedge_min_quote)
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 1.0
    # entropy rich: resting an ASK on entropy and buying the hedge clears the
    # 2 bps hurdle with room to spare
    eng.entropy.set_book(100.00, 100.20)
    eng.hedge.set_book(99.90, 100.00)
    return eng


def run(coro):
    return asyncio.run(coro)


def approx(a, b, tol=1e-6):
    assert abs(a - b) <= tol, f"{a} != {b}"


# ------------------------------------------------- the state machine itself

def test_cancel_does_not_forget_the_order():
    """Must #1. XEMM sets maker_order = None on the line after cancel();
    a fill arriving after that has nowhere to go."""
    o = MakerOrder(venue_key="entropy", is_buy=False, qty=1.0, px=100.0,
                   sent_ts=0.0, handle="abc")
    o.apply("open")
    assert o.state == mk.RESTING
    o.request_cancel(now=1.0)
    assert o.state == mk.CANCELLING
    assert o.handle == "abc"          # still addressable
    assert not o.is_terminal          # still ours
    # the cancel lost the race and it filled
    gained = o.apply("filled", filled_base=1.0)
    approx(gained, 1.0)
    assert o.is_terminal and o.unhedged == 1.0


def test_only_exchange_truth_is_terminal():
    o = MakerOrder(venue_key="e", is_buy=True, qty=1.0, px=1.0, sent_ts=0.0)
    for weird in ("", "queued", "someNewStatusNobodyDocumented"):
        o.apply(weird)
        assert not o.is_terminal, weird
    o.apply("canceled-post-only")     # a Lighter cancel reason
    assert o.is_terminal


def test_fills_are_monotonic():
    o = MakerOrder(venue_key="e", is_buy=True, qty=2.0, px=1.0, sent_ts=0.0)
    approx(o.apply("open", filled_base=1.0), 1.0)
    o.mark_applied(1.0)
    o.mark_hedged(1.0)
    # a stale frame arriving late must not resurrect an already-hedged fill
    approx(o.apply("open", filled_base=0.4), 0.0)
    approx(o.filled_base, 1.0)
    approx(o.unhedged, 0.0)


def test_unconfirmed_cancel_is_pessimistic_and_not_final():
    """Must #3: unknown means POSSIBLY FILLED, and it is not an ending."""
    o = MakerOrder(venue_key="e", is_buy=True, qty=1.0, px=1.0, sent_ts=0.0)
    o.apply("open")
    o.request_cancel(now=10.0)
    assert not o.cancel_overdue(10.5, budget_sec=1.0)
    assert o.cancel_overdue(11.5, budget_sec=1.0)
    o.to_unknown("cancel unconfirmed")
    assert not o.is_terminal and o.needs_attention
    o.apply("canceled")               # the exchange finally answers
    assert o.is_terminal


def test_applied_and_hedged_advance_separately():
    o = MakerOrder(venue_key="e", is_buy=True, qty=1.0, px=1.0, sent_ts=0.0)
    o.apply("open", filled_base=0.3)
    o.mark_applied(0.3)               # booked as a position immediately
    approx(o.unapplied, 0.0)
    approx(o.unhedged, 0.3)           # but too small to hedge yet


# ------------------------------------------------------------ quote pricing

def _books(maker_bid, maker_ask, hedge_bid, hedge_ask, sz=100.0):
    m, h = OrderBook(), OrderBook()
    m.apply_hl([[{"px": str(maker_bid), "sz": str(sz)}],
                [{"px": str(maker_ask), "sz": str(sz)}]])
    h.apply_hl([[{"px": str(hedge_bid), "sz": str(sz)}],
                [{"px": str(hedge_ask), "sz": str(sz)}]])
    return m, h


def _round2(px, up):
    import math
    return round(math.ceil(px * 100 - 1e-9) / 100 if up
                 else math.floor(px * 100 + 1e-9) / 100, 8)


def plan(maker_is_buy=False, thr=2.0, maker_fee=0.0, hedge_fee=0.0,
         cap=500.0, hedge_sz=100.0, **bk):
    m, h = _books(bk.get("maker_bid", 100.00), bk.get("maker_ask", 100.20),
                  bk.get("hedge_bid", 99.90), bk.get("hedge_ask", 100.00))
    if hedge_sz != 100.0:
        h.apply_hl([[{"px": str(bk.get("hedge_bid", 99.90)),
                      "sz": str(hedge_sz)}],
                    [{"px": str(bk.get("hedge_ask", 100.00)),
                      "sz": str(hedge_sz)}]])
    return plan_maker(m, h, maker_is_buy=maker_is_buy, threshold_bps=thr,
                      maker_fee_bps=maker_fee, hedge_fee_bps=hedge_fee,
                      take_fraction=0.5, cap_notional=cap, min_base=1e-4,
                      min_notional=1.0, size_step=1e-4, px_round=_round2,
                      tick=0.01)


def test_quote_price_improves_the_bbo_without_crossing():
    p, why = plan(maker_is_buy=False)     # rest an ASK on the maker venue
    assert p is not None, why
    # best ask is 100.20 and the hurdle allows better, so we post one tick
    # inside it -- top of our own side of the book, never across the bid
    approx(p.maker_px, 100.19)
    assert p.maker_px > 100.00
    assert p.edge_bps > 2.0


def test_quote_price_never_breaks_the_hurdle():
    # hedge ask is 100.00; a 2 bps hurdle means we may not sell below
    # 100.02, which is INSIDE the spread, so the hurdle price wins
    p, why = plan(maker_is_buy=False, maker_bid=99.00, maker_ask=100.19,
                  hedge_bid=99.90, hedge_ask=100.00, thr=15.0)
    assert p is not None, why
    assert p.maker_px >= 100.15, p.maker_px
    assert p.edge_bps >= 15.0 - 1e-6


def test_quote_refused_when_even_the_bbo_loses_money():
    p, why = plan(maker_is_buy=False, maker_bid=99.00, maker_ask=99.50,
                  hedge_bid=99.90, hedge_ask=100.00)
    assert p is None and why == "no_edge"


def test_quote_size_bounded_by_hedge_depth():
    """Never post what you cannot hedge."""
    p, _ = plan(maker_is_buy=False, hedge_sz=1.0)
    assert p is not None
    approx(p.qty, 0.5)                  # take_fraction of 1.0 hedgeable
    p2, _ = plan(maker_is_buy=False, hedge_sz=100.0)
    assert p2.qty > p.qty


def test_quote_refuses_a_crossed_book():
    p, why = plan(maker_is_buy=False, maker_bid=100.50, maker_ask=100.20)
    assert p is None and why == "crossed_book"


# --------------------------------------------- the four required scenarios

def _drive(eng, hook=None):
    """Plan one quote and run it to a resolved end."""
    async def go():
        import time
        # first pass arms the direction, second passes the persistence gate
        eng._scan_maker(time.time())
        best = eng._scan_maker(time.time())
        assert best is not None, "no quote planned"
        maker_is_buy, dkey, p = best
        maker_v, taker_v = eng._maker_legs()
        order = MakerOrder(venue_key=maker_v.key, is_buy=maker_is_buy,
                           qty=p.qty, px=p.maker_px, sent_ts=time.time())
        order.stats["dkey"] = dkey
        eng._maker_open[maker_v.key] = order
        if hook:
            hook(eng, maker_v, taker_v, p, order)
        await eng._execute_maker(maker_v, taker_v, p, order)
        return p, order
    return run(go())


def test_quote_fills_then_hedges():
    """post -> fill. The hedge leg must be sent for the filled size."""
    eng = make_engine()

    def hook(eng, m, t, p, o):
        def on_poll(v, n):
            if n >= 1:
                v.ex_status, v.ex_filled = "filled", p.qty
        m.on_poll = on_poll
    p, order = _drive(eng, hook)
    assert order.is_terminal and order.filled_base == p.qty
    assert len(eng.hedge.sent_takers) == 1
    is_buy, qty, _ = eng.hedge.sent_takers[0]
    assert is_buy is True                      # we sold on the maker venue
    approx(qty, p.qty)
    approx(eng.entropy.position, -p.qty)       # short the maker leg
    approx(eng.hedge.position, p.qty)          # long the hedge leg
    approx(eng.entropy.position + eng.hedge.position, 0.0)
    assert eng.maker_fills == 1 and eng.maker_posts == 1
    assert not eng._maker_open                 # resolved, so it is released


def test_quote_times_out_and_cancels():
    """post -> nothing -> cancel. No position, no hedge, no leftovers."""
    eng = make_engine()

    def hook(eng, m, t, p, o):
        m.on_cancel = lambda v: setattr(v, "ex_status", "canceled")
    p, order = _drive(eng, hook)
    assert order.is_terminal and order.filled_base == 0.0
    assert eng.entropy.cancels, "the quote was never cancelled"
    assert not eng.hedge.sent_takers
    approx(eng.entropy.position, 0.0)
    assert eng.maker_cancels == 1 and eng.maker_fills == 0
    assert not eng._maker_open


def test_quote_partial_fill_is_hedged_before_it_completes():
    """post -> HALF fills. XEMM v2 waits for OrderCompletedEvent and leaves
    that half naked; this must hedge it the moment it is reported."""
    eng = make_engine()
    seen = {}

    def hook(eng, m, t, p, o):
        def on_poll(v, n):
            if n == 1:
                v.ex_filled = round(p.qty / 2, 4)      # still open
            elif n >= 3:
                v.ex_status, v.ex_filled = "filled", p.qty
            # record whether the hedge went out while still resting
            if n == 2:
                seen["hedged_while_open"] = len(t.sent_takers)
        m.on_poll = on_poll
    p, order = _drive(eng, hook)
    assert seen.get("hedged_while_open") == 1, \
        "the first half was not hedged until the order completed"
    assert len(eng.hedge.sent_takers) == 2
    approx(sum(q for _, q, _ in eng.hedge.sent_takers), p.qty)
    approx(eng.entropy.position + eng.hedge.position, 0.0)


def test_quote_cancel_loses_the_race_and_still_hedges():
    """post -> cancel -> it filled anyway.

    This is the XEMM bug verbatim (PEER_INFRA S3.1): they null the local
    order at cancel time, so the fill that arrives afterwards finds nothing
    and the hedge is never sent. Here the fill must still be hedged.
    """
    eng = make_engine()

    def hook(eng, m, t, p, o):
        def on_cancel(v):
            # the taker crossed us in the same instant the cancel went out
            v.ex_status, v.ex_filled = "filled", p.qty
        m.on_cancel = on_cancel
        m.cancel_result = {"status": "gone",
                           "err": "Order was never placed, already canceled, "
                                  "or filled"}
    p, order = _drive(eng, hook)
    assert eng.entropy.cancels, "no cancel was attempted"
    assert order.is_terminal and order.filled_base == p.qty
    assert len(eng.hedge.sent_takers) == 1, \
        "the fill that beat the cancel was never hedged"
    approx(eng.entropy.position + eng.hedge.position, 0.0)


# ------------------------------------------------- the pessimistic branches

def test_cancel_that_never_confirms_goes_unknown():
    eng = make_engine()
    eng.stop.set()          # bound the test: shutdown gives it a deadline

    def hook(eng, m, t, p, o):
        m.poll_answers = False                      # the venue says nothing
        m.cancel_result = {"status": "unresolved", "err": None}
    p, order = _drive(eng, hook)
    assert order.state == mk.UNKNOWN
    assert order.cancel_attempts >= 1
    assert eng.maker_unknown == 1
    assert not order.is_terminal                    # never silently retired


def test_unresolved_order_blocks_the_next_quote():
    eng = make_engine()
    import time
    stuck = MakerOrder(venue_key="entropy", is_buy=False, qty=1.0, px=100.19,
                       sent_ts=time.time())
    stuck.to_unknown("cancel unconfirmed")
    eng._maker_open["entropy"] = stuck
    assert eng._scan_maker(time.time()) is None


def test_send_with_unknown_outcome_is_treated_as_live():
    """A send that timed out may be resting. It must be cancelled, never
    assumed away."""
    eng = make_engine()

    def hook(eng, m, t, p, o):
        m.send_maker_result = {"status": "send-unresolved", "handle": 7,
                               "unresolved": True, "err": "timeout"}
        m.on_cancel = lambda v: setattr(v, "ex_status", "canceled")
    p, order = _drive(eng, hook)
    assert eng.entropy.cancels == [7], "an unknown send was not cancelled"
    assert order.is_terminal


def test_post_only_rejection_is_not_an_error():
    eng = make_engine()

    def hook(eng, m, t, p, o):
        m.send_maker_result = {"status": "post-only-reject", "handle": 3}
    p, order = _drive(eng, hook)
    assert eng.maker_rejects == 1
    assert eng.consec_errors == 0            # not a failure, just a miss
    assert not eng.hedge.sent_takers
    assert not eng._maker_open


def test_edge_decay_cancels_the_quote():
    """The hedge moved against us: a quote we can no longer hedge at a
    profit is an adverse-selection machine, so it comes off the book."""
    eng = make_engine(maker_timeout_sec=30.0)   # long enough to prove intent

    def hook(eng, m, t, p, o):
        def on_poll(v, n):
            if n == 1:
                # the hedge ask jumps above our resting ask: buying there
                # to cover a fill would now lose money
                t.set_book(100.50, 100.60)
        m.on_poll = on_poll
        m.on_cancel = lambda v: setattr(v, "ex_status", "canceled")
    p, order = _drive(eng, hook)
    assert eng.entropy.cancels, "a decayed quote was left resting"
    assert "edge decayed" in order.stats.get("cancel_reason", "")


def test_partial_below_hedge_minimum_accumulates():
    """A fill too small for the other venue's minimum is carried, not sent
    as an order that would be rejected -- and it is NOT marked hedged."""
    eng = make_engine(hedge_min_quote=1e6, maker_timeout_sec=0.15,
                      max_net_base=1e6)

    def hook(eng, m, t, p, o):
        def on_poll(v, n):
            if n == 1:
                v.ex_filled = round(p.qty / 2, 4)
            elif n >= 3:
                v.ex_status = "canceled"
        m.on_poll = on_poll
        m.on_cancel = lambda v: None
    p, order = _drive(eng, hook)
    half = round(p.qty / 2, 4)
    assert not eng.hedge.sent_takers, "sent an order below the venue minimum"
    assert order.unhedged > 0, "pretended an unhedgeable fill was hedged"
    approx(order.applied_base, half)          # booked as a position anyway
    # and the net-delta hedge, which owns "the legs do not match", flattens
    # it on the venue that carries it rather than leaving it naked
    assert eng.entropy.sent_takers, "the unhedgeable fill was left naked"
    is_buy, qty, _ = eng.entropy.sent_takers[0]
    assert is_buy is True and abs(qty - half) < 1e-6
    approx(eng.entropy.position + eng.hedge.position, 0.0)


def test_halt_takes_the_quote_off_the_book():
    eng = make_engine(maker_timeout_sec=30.0)

    def hook(eng, m, t, p, o):
        def on_poll(v, n):
            if n == 1:
                eng._risk_halt("test")
        m.on_poll = on_poll
        m.on_cancel = lambda v: setattr(v, "ex_status", "canceled")
    p, order = _drive(eng, hook)
    assert eng.entropy.cancels, "a halt left a live quote on the book"
    assert order.is_terminal


def test_reconcile_supersedes_maker_accounting():
    """A chain read is complete: after adopting it, the same fill may not be
    booked a second time from the order's own counters."""
    eng = make_engine()
    import time
    o = MakerOrder(venue_key="entropy", is_buy=False, qty=1.0, px=100.19,
                   sent_ts=time.time())
    o.apply("open", filled_base=1.0)          # exchange says filled, unbooked
    eng._maker_open["entropy"] = o
    eng.entropy.chain_position = -1.0         # the chain already has it
    approx(eng.entropy.position, 0.0)         # our local view does not

    async def go():
        eng.entropy.last_traded_ts = 0.0
        await eng._reconcile_venue(eng.entropy, strict=False)
    run(go())
    approx(o.unapplied, 0.0)
    approx(o.unhedged, 0.0)
    approx(eng.entropy.position, -1.0)        # booked exactly once


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:52s} OK")


def test_status_matching_is_case_insensitive():
    """Today HL sends lower/camelCase and Lighter lowercase, so exact
    matching happens to work. The failure mode if that ever changes is the
    worst one here: an unrecognised status is (correctly) not terminal, the
    order sits in `unknown` forever, and an order in `unknown` blocks every
    future quote. A venue renaming "filled" to "FILLED" would stop the
    engine dead, quietly."""
    for s in ("FILLED", "Filled", "CANCELED", "CANCELLED", "REJECTED",
              "Canceled-Post-Only"):
        assert mk.is_terminal_status(s), s
    for s in ("OPEN", "New", "PARTIALLY_FILLED", "Pending"):
        assert not mk.is_terminal_status(s), s


def test_a_partial_fill_status_keeps_the_order_resting():
    """PARTIALLY_FILLED means still on the book with some of it done --
    terminal only to a matcher that has never seen the word."""
    o = MakerOrder(venue_key="e", is_buy=True, qty=1.0, px=1.0, sent_ts=0.0)
    o.apply("PARTIALLY_FILLED", filled_base=0.4)
    assert not o.is_terminal
    approx(o.unhedged, 0.4)


def test_an_unknown_status_still_never_retires_an_order():
    o = MakerOrder(venue_key="e", is_buy=True, qty=1.0, px=1.0, sent_ts=0.0)
    o.apply("SOME_STATUS_NOBODY_DOCUMENTED")
    assert not o.is_terminal
