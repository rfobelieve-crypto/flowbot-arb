"""Order book state and fee-aware arbitrage sizing.

One book class serves both feed protocols: zkLighter sends a snapshot plus
diffs (dict maintenance), Hyperliquid's l2Book sends full snapshots.
Freshness is connection-based (any inbound ws frame touches alive_ts): a quiet
market is not stale, only a dead feed is.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

Level = Tuple[float, float]


class OrderBook:
    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.ready = False
        self.last_update_ts = 0.0
        self.alive_ts = 0.0

    def touch(self) -> None:
        self.alive_ts = time.time()

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready = False

    # ---- zkLighter snapshot + diff ----
    def apply_lighter(self, ob: dict, snapshot: bool) -> None:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
        for name, side in (("bids", self.bids), ("asks", self.asks)):
            for lvl in ob.get(name) or []:
                px, sz = float(lvl["price"]), float(lvl["size"])
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
        self.ready = True
        self.last_update_ts = time.time()
        self.touch()

    # ---- Hyperliquid full snapshot ----
    def apply_hl(self, levels: list) -> None:
        self.bids = {float(l["px"]): float(l["sz"])
                     for l in levels[0] if float(l["sz"]) > 0}
        self.asks = {float(l["px"]): float(l["sz"])
                     for l in levels[1] if float(l["sz"]) > 0}
        self.ready = True
        self.last_update_ts = time.time()
        self.touch()

    def sorted_bids(self) -> List[Level]:
        return sorted(self.bids.items(), key=lambda kv: -kv[0])

    def sorted_asks(self) -> List[Level]:
        return sorted(self.asks.items())

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        if not (self.bids and self.asks):
            return None
        return (max(self.bids) + min(self.asks)) / 2.0

    def is_fresh(self, max_age_sec: float) -> bool:
        """Recent enough to describe the market. Says nothing about whether
        it makes SENSE -- see is_crossed(). Deliberately unchanged: the
        recorder counts `samples` with this, and the recording family is
        mid-gate; moving what counts as a sample would move the instrument
        under a measurement already in flight."""
        return self.ready and bool(self.bids) and bool(self.asks) and (
            time.time() - self.alive_ts <= max_age_sec)

    def is_crossed(self) -> bool:
        """Best bid at or above best ask: this book is broken.

        No venue quotes a crossed book. When ours shows one it is our copy
        that is wrong -- a diff whose deletion we missed, a partially applied
        update, a level that should have gone. Acting on it means acting on a
        fiction, and the fiction is specifically the kind that LOOKS like free
        money (a negative spread reads as an instant profit).

        Found 2026-09-05 by reading perp-dex-tools, which refuses a crossed
        book before every price decision (`trading_bot.py:458`). We refused
        it only when placing a maker quote, and only on the venue we were
        quoting -- the taker, hedge and flatten paths did not check at all.
        """
        if not self.bids or not self.asks:
            return False
        return max(self.bids) >= min(self.asks)

    def tradeable(self, max_age_sec: float) -> bool:
        """Fresh AND sane. Every path that sends an order asks this."""
        return self.is_fresh(max_age_sec) and not self.is_crossed()


def floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 12)


def crossable_base(asks: List[Level], bids: List[Level], threshold: float,
                   buy_fee: float = 0.0, sell_fee: float = 0.0) -> Tuple[float, float]:
    """Walk both books level by level and return (base qty, buy notional) that
    can be crossed while every marginal slice still clears fees + threshold."""
    qty = 0.0
    buy_notional = 0.0
    i = j = 0
    a_px = a_rem = 0.0
    b_px = b_rem = 0.0
    while True:
        if a_rem <= 0:
            if i >= len(asks):
                break
            a_px, a_rem = asks[i]
            i += 1
        if b_rem <= 0:
            if j >= len(bids):
                break
            b_px, b_rem = bids[j]
            j += 1
        if b_px * (1.0 - sell_fee) < a_px * (1.0 + buy_fee) * (1.0 + threshold):
            break
        take = min(a_rem, b_rem)
        qty += take
        buy_notional += take * a_px
        a_rem -= take
        b_rem -= take
    return qty, buy_notional


def walk_depth(levels: List[Level], qty: float) -> Tuple[float, float]:
    remaining = qty
    notional = 0.0
    marginal_px = levels[0][0]
    for px, sz in levels:
        take = min(remaining, sz)
        notional += take * px
        marginal_px = px
        remaining -= take
        if remaining <= 1e-12:
            break
    return marginal_px, notional


@dataclass
class ArbPlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    q_max: float
    q_max_notional: float
    top_premium_bps: float
    marginal_premium_bps: float
    buy_fee: float
    sell_fee: float

    @property
    def gross_edge_usd(self) -> float:
        return self.sell_notional - self.buy_notional

    @property
    def exp_edge_usd(self) -> float:
        return (self.sell_notional * (1.0 - self.sell_fee)
                - self.buy_notional * (1.0 + self.buy_fee))


def plan_arb(buy_book: OrderBook, sell_book: OrderBook, *, threshold_bps: float,
             buy_fee_bps: float, sell_fee_bps: float, take_fraction: float,
             cap_notional: float, min_base: float, min_notional: float,
             size_step: float):
    """Size a two-leg taker slice: buy on buy_book, sell on sell_book.

    A slice qualifies when the executable premium (sell bid over buy ask)
    clears both venues' taker fees plus threshold_bps. Returns
    (ArbPlan | None, reason).
    """
    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    if buy_book.is_crossed() or sell_book.is_crossed():
        # Defence in depth: the callers gate on tradeable(), but a crossed
        # book reads as free money and must not be one guard deep.
        return None, "crossed_book"
    threshold = threshold_bps / 1e4
    buy_fee = buy_fee_bps / 1e4
    sell_fee = sell_fee_bps / 1e4
    top_premium_bps = (bids[0][0] / asks[0][0] - 1.0) * 1e4
    if bids[0][0] * (1.0 - sell_fee) < asks[0][0] * (1.0 + buy_fee) * (1.0 + threshold):
        return None, "no_edge"
    q_max, q_max_notional = crossable_base(asks, bids, threshold, buy_fee, sell_fee)
    if q_max <= 0:
        return None, "no_edge"
    target = min(q_max * take_fraction, cap_notional / asks[0][0])
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if buy_notional < min_notional or sell_notional < min_notional:
        return None, "below_min_notional"
    return ArbPlan(
        qty=target, buy_limit=buy_limit, sell_limit=sell_limit,
        buy_notional=buy_notional, sell_notional=sell_notional,
        q_max=q_max, q_max_notional=q_max_notional,
        top_premium_bps=top_premium_bps,
        marginal_premium_bps=(sell_limit / buy_limit - 1.0) * 1e4,
        buy_fee=buy_fee, sell_fee=sell_fee,
    ), "ok"


# ------------------------------------------------------------- maker sizing
#
# B3 (2026-09-04). The taker planner asks "how much of this crossable edge can
# I take?". The maker planner asks a different and stricter question: "at what
# price may I REST, and how much of that may I promise, given that whatever
# fills I must immediately hedge on the other venue?"
#
# The stricter half is the size. A taker slice that fails to fill costs
# nothing; a maker fill that cannot be hedged is a naked position. So the size
# is bounded by the HEDGE venue's depth, never by the maker venue's.


def hedgeable_base(levels: List[Level], accept) -> Tuple[float, float]:
    """Depth on the hedge side, walked while `accept(price)` holds.

    Unlike crossable_base this compares every level against a FIXED maker
    price (ours, already decided), because a resting order's price does not
    move as the hedge book is consumed.
    """
    qty = 0.0
    notional = 0.0
    for px, sz in levels:
        if not accept(px):
            break
        qty += sz
        notional += sz * px
    return qty, notional


def depth_base(levels: List[Level]) -> float:
    return sum(sz for _, sz in levels)


@dataclass
class MakerPlan:
    maker_is_buy: bool        # side WE rest on the maker venue
    maker_px: float           # post-only limit price
    qty: float
    maker_notional: float
    hedge_limit: float        # marginal hedge price at qty (pre-slippage)
    hedge_notional: float
    hedge_depth: float        # hedgeable base at an acceptable price
    top_premium_bps: float    # the premium we would be acting on
    edge_bps: float           # net of BOTH fees, at the posted price
    maker_fee: float
    hedge_fee: float

    @property
    def exp_edge_usd(self) -> float:
        if self.maker_is_buy:
            return (self.hedge_notional * (1.0 - self.hedge_fee)
                    - self.maker_notional * (1.0 + self.maker_fee))
        return (self.maker_notional * (1.0 - self.maker_fee)
                - self.hedge_notional * (1.0 + self.hedge_fee))


def maker_edge_bps(maker_px: float, maker_is_buy: bool, hedge_book: OrderBook,
                   *, maker_fee_bps: float, hedge_fee_bps: float,
                   qty: float) -> Optional[float]:
    """Net bps left in an ALREADY RESTING quote if its fill were hedged now.

    Returns None when the hedge side cannot absorb `qty` at all -- which the
    caller must read as "cancel", not as "zero edge": a quote we cannot hedge
    is worse than a quote that makes no money.
    """
    levels = hedge_book.sorted_bids() if maker_is_buy else hedge_book.sorted_asks()
    if not levels or maker_px <= 0:
        return None
    if depth_base(levels) < qty - 1e-12:
        return None
    hedge_px, _ = walk_depth(levels, max(qty, 0.0))
    mf, hf = maker_fee_bps / 1e4, hedge_fee_bps / 1e4
    if maker_is_buy:
        return (hedge_px * (1.0 - hf) / (maker_px * (1.0 + mf)) - 1.0) * 1e4
    return (maker_px * (1.0 - mf) / (hedge_px * (1.0 + hf)) - 1.0) * 1e4


def plan_maker(maker_book: OrderBook, hedge_book: OrderBook, *,
               maker_is_buy: bool, threshold_bps: float, maker_fee_bps: float,
               hedge_fee_bps: float, take_fraction: float,
               cap_notional: float, min_base: float, min_notional: float,
               size_step: float, px_round, tick: float):
    """Price and size one post-only quote. Returns (MakerPlan | None, reason).

    Price: the best price on our own side of the maker book that still clears
    the hurdle -- top of the queue when the hurdle allows it, the hurdle price
    when it does not, and nothing at all when even joining the BBO would lose
    money. Rounding is always in the direction that protects the hurdle (a
    resting bid rounds down, a resting ask rounds up), so the grid can cost us
    fill probability but never edge.

    Size: `take_fraction` of the HEDGE side's acceptable depth, capped by
    notional. Never post what you cannot hedge.
    """
    m_bid, m_ask = maker_book.best_bid(), maker_book.best_ask()
    if m_bid is None or m_ask is None:
        return None, "empty_book"
    if m_bid >= m_ask:
        # A crossed or locked book is a broken book. Resting inside one is
        # how you get picked off by whatever is broken about it.
        return None, "crossed_book"
    levels = hedge_book.sorted_bids() if maker_is_buy else hedge_book.sorted_asks()
    if not levels:
        return None, "empty_book"
    if tick <= 0:
        return None, "bad_tick"

    thr = threshold_bps / 1e4
    mf = maker_fee_bps / 1e4
    hf = hedge_fee_bps / 1e4
    h0 = levels[0][0]

    if maker_is_buy:
        # We rest a BID and will SELL into the hedge bid once filled.
        p_lim = h0 * (1.0 - hf) / ((1.0 + mf) * (1.0 + thr))   # most we may pay
        improve = m_bid + tick
        if improve >= m_ask:              # one-tick spread: no room to improve
            improve = m_bid               # join the queue instead
        px = px_round(min(p_lim, improve), False)              # never overpay
        if px < m_bid - 1e-12:
            return None, "no_edge"        # hurdle needs a price behind the BBO
        if px >= m_ask:
            return None, "would_cross"    # px_round misbehaved; refuse to post
    else:
        # We rest an ASK and will BUY on the hedge ask once filled.
        p_lim = h0 * (1.0 + hf) * (1.0 + thr) / (1.0 - mf)  # least we may take
        improve = m_ask - tick
        if improve <= m_bid:
            improve = m_ask
        px = px_round(max(p_lim, improve), True)               # never undersell
        if px > m_ask + 1e-12:
            return None, "no_edge"
        if px <= m_bid:
            return None, "would_cross"

    if maker_is_buy:
        def accept(level_px: float) -> bool:
            return level_px * (1.0 - hf) >= px * (1.0 + mf) * (1.0 + thr)
    else:
        def accept(level_px: float) -> bool:
            return px * (1.0 - mf) >= level_px * (1.0 + hf) * (1.0 + thr)

    hedge_depth, _ = hedgeable_base(levels, accept)
    if hedge_depth <= 0:
        return None, "no_hedge_depth"
    qty = floor_step(min(hedge_depth * take_fraction, cap_notional / px),
                     size_step)
    if qty < min_base:
        return None, "below_min_base"
    hedge_limit, hedge_notional = walk_depth(levels, qty)
    maker_notional = qty * px
    if maker_notional < min_notional or hedge_notional < min_notional:
        return None, "below_min_notional"

    if maker_is_buy:
        sell_px, buy_px, sell_fee, buy_fee = hedge_limit, px, hf, mf
    else:
        sell_px, buy_px, sell_fee, buy_fee = px, hedge_limit, mf, hf
    return MakerPlan(
        maker_is_buy=maker_is_buy, maker_px=px, qty=qty,
        maker_notional=maker_notional, hedge_limit=hedge_limit,
        hedge_notional=hedge_notional, hedge_depth=hedge_depth,
        top_premium_bps=(sell_px / buy_px - 1.0) * 1e4,
        edge_bps=(sell_px * (1.0 - sell_fee)
                  / (buy_px * (1.0 + buy_fee)) - 1.0) * 1e4,
        maker_fee=mf, hedge_fee=hf,
    ), "ok"
