"""Automatic 1-minute orderbook data recorder.

While the bot runs (live or --record-only), both venues' actual order books
are sampled once per second and aggregated into one CSV row per minute.
This is the dataset users analyze (tools/analyze.py) to choose
thresholds.midline_bps / upper_bps / lower_bps for config.yaml.

Definitions (all in bps, fees NOT included — the engine adds fees on top):

    premium    = (entropy_mid / hedge_mid - 1) * 1e4
                 the mid-to-mid premium of Entropy over the hedge venue;
                 its long-run center is what midline_bps hardcodes.
    sell_edge  = (entropy_bid / hedge_ask - 1) * 1e4
                 the EXECUTABLE premium for SELL-entropy/BUY-hedge; the
                 engine fires this direction when sell_edge clears
                 midline_bps + upper_bps (plus fees).
    buy_edge   = (hedge_bid / entropy_ask - 1) * 1e4
                 the executable premium for BUY-entropy/SELL-hedge; fires
                 when buy_edge clears lower_bps - midline_bps (plus fees).

Bid/ask columns are the minute's last fresh sample (close). A row is only
written for minutes with at least one sample where both books were fresh;
`samples` says how many of the ~60 seconds qualified.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

from .book import OrderBook

log = logging.getLogger("recorder")

HEADER = ["minute_ts", "time_utc",
          "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
          "premium_open_bps", "premium_high_bps", "premium_low_bps",
          "premium_close_bps", "premium_mean_bps", "premium_std_bps",
          "sell_edge_mean_bps", "sell_edge_max_bps",
          "buy_edge_mean_bps", "buy_edge_max_bps", "samples",
          # 2026-08-28 local patch: a fat edge print is only honest if we
          # know (a) how much money actually sat at top-of-book at that
          # moment and (b) whether either book's PRICE was stale.
          # *_max_notional_usd = min of both legs' top-level notional at the
          # sample that set the minute's max edge; *_max_age_s = the older
          # book's price-data age at that same sample (last_update_ts, not
          # alive_ts — a ping keeps a dead price looking alive).
          "e_bid_sz", "e_ask_sz", "h_bid_sz", "h_ask_sz",
          "sell_max_notional_usd", "sell_max_age_s",
          "buy_max_notional_usd", "buy_max_age_s",
          # 2026-09-01 local patch: the OTHER way this pair can pay. The
          # price spread needs to CONVERGE to be worth anything; the funding
          # differential pays for HOLDING. Both normalised to bps per 8h
          # (HL publishes per hour, Lighter per 8h — see funding.py).
          # fund_diff = hedge - entropy: positive means long-entropy /
          # short-hedge collects.
          "fund_entropy_bps8h", "fund_hedge_bps8h", "fund_diff_bps8h",
          "fund_age_s",
          # 2026-09-12: depth BEYOND the touch. Cumulative USD notional
          # within N bps of that venue's own mid, per side, per venue.
          #
          # Why this was missing and why it matters: the capacity model in
          # §1.20 multiplies band x events x *top-of-book* depth, and that
          # top-of-book figure came out around $264 -- which made the whole
          # family look like loose change. But top-of-book is one price
          # level. Small Trader Alpha #2 ("Going Deeper") points out that in
          # illiquid books the size sits at a few DEEP levels ("monster
          # levels"), not at the touch, and whether you join the touch or
          # step back depends on WHY the mispricing exists. We had the full
          # book in memory the whole time and were writing only its first
          # level.
          #
          # Bands are 5/25/100 bps because this family's own bands span two
          # orders of magnitude (NBIS ~26 bps, ANTH ~464): a single band
          # would be too tight for some pairs and meaningless for others.
          # These are ORDER BOOK sizes, not traded volume -- traded volume
          # is a separate gap (flow_system TODO 1.22) that needs a trades
          # subscription neither feed currently makes.
          "e_bid_d5", "e_bid_d25", "e_bid_d100",
          "e_ask_d5", "e_ask_d25", "e_ask_d100",
          "h_bid_d5", "h_bid_d25", "h_bid_d100",
          "h_ask_d5", "h_ask_d25", "h_ask_d100"]

DEPTH_BPS = (5.0, 25.0, 100.0)


def depth_usd(levels: dict, mid: float, is_bid: bool, bands=DEPTH_BPS) -> list:
    """Cumulative USD notional within each band of `mid`, in `bands` order.

    `levels` is OrderBook.bids / .asks ({price: size}). Bids count levels at
    or above mid*(1-b); asks at or below mid*(1+b) -- i.e. how much you could
    hit (bids) or lift (asks) without going further from mid than b bps.

    Total, never raises: this runs once a second inside the live engine and
    one malformed tick must not be able to stop the recorder. A bad level is
    skipped, not fatal.
    """
    out = [0.0] * len(bands)
    if not levels or not (mid > 0):
        return out
    for px, sz in levels.items():
        try:
            px = float(px)
            sz = float(sz)
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        off = (mid - px) / mid * 1e4 if is_bid else (px - mid) / mid * 1e4
        if off < 0.0:                    # inside/crossed: treat as 0 bps away
            off = 0.0
        ntl = px * sz
        for i, b in enumerate(bands):
            if off <= b:
                out[i] += ntl
    return out


class _MinuteAgg:
    __slots__ = ("minute", "n", "p_open", "p_high", "p_low", "p_close",
                 "p_sum", "p_sumsq", "s_sum", "s_max", "b_sum", "b_max",
                 "e_bid", "e_ask", "h_bid", "h_ask",
                 "e_bid_sz", "e_ask_sz", "h_bid_sz", "h_ask_sz",
                 "s_max_ntl", "s_max_age", "b_max_ntl", "b_max_age",
                 "depth")

    def __init__(self, minute: int) -> None:
        self.minute = minute
        self.n = 0
        self.p_open = self.p_high = self.p_low = self.p_close = 0.0
        self.p_sum = self.p_sumsq = 0.0
        self.s_sum = 0.0
        self.s_max = -math.inf
        self.b_sum = 0.0
        self.b_max = -math.inf
        self.e_bid = self.e_ask = self.h_bid = self.h_ask = 0.0
        self.e_bid_sz = self.e_ask_sz = self.h_bid_sz = self.h_ask_sz = 0.0
        self.s_max_ntl = self.s_max_age = 0.0
        self.b_max_ntl = self.b_max_age = 0.0
        # 12 個深度欄的收盤值（e_bid/e_ask/h_bid/h_ask 各三個帶）。
        # 跟其他 *_sz 一樣取「這一分鐘最後一筆新鮮樣本」,不取平均 ——
        # 平均會把一次短暫的厚牆抹掉,而我們要問的正是「那一刻有沒有量」。
        self.depth = [0.0] * 12

    def add(self, e_bid: float, e_ask: float, h_bid: float, h_ask: float,
            e_bid_sz: float = 0.0, e_ask_sz: float = 0.0,
            h_bid_sz: float = 0.0, h_ask_sz: float = 0.0,
            e_age: float = 0.0, h_age: float = 0.0,
            depth: list = None) -> None:
        e_mid = (e_bid + e_ask) / 2.0
        h_mid = (h_bid + h_ask) / 2.0
        prem = (e_mid / h_mid - 1.0) * 1e4
        sell_edge = (e_bid / h_ask - 1.0) * 1e4
        buy_edge = (h_bid / e_ask - 1.0) * 1e4
        if self.n == 0:
            self.p_open = self.p_high = self.p_low = prem
        self.n += 1
        self.p_high = max(self.p_high, prem)
        self.p_low = min(self.p_low, prem)
        self.p_close = prem
        self.p_sum += prem
        self.p_sumsq += prem * prem
        self.s_sum += sell_edge
        if sell_edge > self.s_max:
            self.s_max = sell_edge
            # sell entropy at its bid / buy hedge at its ask
            self.s_max_ntl = min(e_bid_sz * e_bid, h_ask_sz * h_ask)
            self.s_max_age = max(e_age, h_age)
        self.b_sum += buy_edge
        if buy_edge > self.b_max:
            self.b_max = buy_edge
            # buy entropy at its ask / sell hedge at its bid
            self.b_max_ntl = min(e_ask_sz * e_ask, h_bid_sz * h_bid)
            self.b_max_age = max(e_age, h_age)
        self.e_bid, self.e_ask, self.h_bid, self.h_ask = e_bid, e_ask, h_bid, h_ask
        self.e_bid_sz, self.e_ask_sz = e_bid_sz, e_ask_sz
        self.h_bid_sz, self.h_ask_sz = h_bid_sz, h_ask_sz
        if depth is not None and len(depth) == 12:
            self.depth = depth

    def row(self) -> list:
        mean = self.p_sum / self.n
        var = max(self.p_sumsq / self.n - mean * mean, 0.0)
        ts = self.minute * 60
        return [ts,
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"{self.e_bid:.10g}", f"{self.e_ask:.10g}",
                f"{self.h_bid:.10g}", f"{self.h_ask:.10g}",
                f"{self.p_open:.3f}", f"{self.p_high:.3f}",
                f"{self.p_low:.3f}", f"{self.p_close:.3f}",
                f"{mean:.3f}", f"{math.sqrt(var):.3f}",
                f"{self.s_sum / self.n:.3f}", f"{self.s_max:.3f}",
                f"{self.b_sum / self.n:.3f}", f"{self.b_max:.3f}",
                self.n,
                f"{self.e_bid_sz:.6g}", f"{self.e_ask_sz:.6g}",
                f"{self.h_bid_sz:.6g}", f"{self.h_ask_sz:.6g}",
                f"{self.s_max_ntl:.2f}", f"{self.s_max_age:.1f}",
                f"{self.b_max_ntl:.2f}", f"{self.b_max_age:.1f}"]

    def depth_row(self) -> list:
        """深度那 12 欄**單獨出**，因為它們在 HEADER 裡排在資金費**之後**。

        `_flush_agg` 寫的是 `row() + 資金費 + depth_row()`。第一版把深度直接
        接在 `row()` 尾巴，於是檔案裡是「深度 12、資金費 4」而 HEADER 是
        「資金費 4、深度 12」——**整組錯位 4 格，每個深度欄的標題都指到別人
        的值**，而且 CSV 依然是合法的、欄數也對得上。
        抓到它的是「讀回檔案檢查數字」的那個測試，不是任何一次程式碼審視。
        """
        return [f"{x:.2f}" for x in self.depth]


class MinuteRecorder:
    def __init__(self, path: str, entropy_book: OrderBook, hedge_book: OrderBook,
                 staleness_sec: float, interval_sec: float = 1.0,
                 funding=None) -> None:
        self.path = path
        # optional FundingPoller; None -> the three funding columns are blank
        self.funding = funding
        self.entropy_book = entropy_book
        self.hedge_book = hedge_book
        self.staleness_sec = staleness_sec
        self.interval_sec = interval_sec
        self.rows_written = 0
        self._agg: Optional[_MinuteAgg] = None
        self._fh = None
        self._writer = None

    def _open(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            # never append rows under a different schema's header.
            #
            # Read the header and CLOSE the file before renaming. The rename
            # used to sit inside the `with open(...)` block, which works on
            # Linux (rename over an open fd is fine) and **can never work on
            # Windows**: Python opens without FILE_SHARE_DELETE, so a file
            # this process holds open cannot be renamed by this process.
            # Found 2026-09-12 by the depth-column schema change: every
            # recorder logged "rotated to ..." and then died on
            # PermissionError [WinError 32] once a minute, writing nothing.
            # The log line claimed a rotation that had not happened.
            with open(self.path, encoding="utf-8") as fh0:
                head = fh0.readline().strip()
            if head != ",".join(HEADER):
                # Timestamped so a SECOND schema change cannot overwrite
                # the first rotation's file (the 2026-08-28 upgrade left
                # 257 minutes in minutes.csv.old; os.replace would have
                # eaten them). Readers glob "<path>*.old".
                dst = f"{self.path}.{time.strftime('%Y%m%d%H%M')}.old"
                os.replace(self.path, dst)
                log.warning("%s had an old header — rotated to %s",
                            self.path, dst)
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if new:
            self._writer.writerow(HEADER)
            self._fh.flush()
        log.info("recording 1-minute orderbook data -> %s", self.path)

    def _flush_agg(self) -> None:
        if self._agg is None or self._agg.n == 0:
            self._agg = None
            return
        if self._writer is None:
            self._open()
        row = self._agg.row()
        # funding is a slow-moving venue property, not a per-sample quantity:
        # read it once at flush rather than aggregating it per second.
        fe = fh = fd = fa = ""
        if self.funding is not None:
            try:
                e, h, diff, age = self.funding.snapshot()
                fe = "" if e is None else f"{e:.4f}"
                fh = "" if h is None else f"{h:.4f}"
                fd = "" if diff is None else f"{diff:.4f}"
                fa = "" if age is None else f"{age:.0f}"
            except Exception:
                pass                      # never lose a minute over funding
        # 順序必須跟 HEADER 一致：… 資金費 4 欄，然後深度 12 欄。
        self._writer.writerow(row + [fe, fh, fd, fa] + self._agg.depth_row())
        self._fh.flush()
        self.rows_written += 1
        self._agg = None

    def sample(self, now: Optional[float] = None) -> None:
        """Take one sample; call ~1/sec. Rolls the minute over as needed."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        if self._agg is not None and self._agg.minute != minute:
            self._flush_agg()
        if not (self.entropy_book.is_fresh(self.staleness_sec)
                and self.hedge_book.is_fresh(self.staleness_sec)):
            return
        e_bid, e_ask = self.entropy_book.best_bid(), self.entropy_book.best_ask()
        h_bid, h_ask = self.hedge_book.best_bid(), self.hedge_book.best_ask()
        if None in (e_bid, e_ask, h_bid, h_ask):
            return
        if self._agg is None:
            self._agg = _MinuteAgg(minute)
        eb, hb = self.entropy_book, self.hedge_book
        # 深度用**各自場館自己的 mid**當錨,不是用溢價那條線 ——
        # 「離我這本簿口的中價幾 bps」才是掛單掛得到哪裡的問題。
        e_mid = (e_bid + e_ask) / 2.0
        h_mid = (h_bid + h_ask) / 2.0
        depth = (depth_usd(eb.bids, e_mid, True)
                 + depth_usd(eb.asks, e_mid, False)
                 + depth_usd(hb.bids, h_mid, True)
                 + depth_usd(hb.asks, h_mid, False))
        self._agg.add(
            e_bid, e_ask, h_bid, h_ask,
            eb.bids.get(e_bid, 0.0), eb.asks.get(e_ask, 0.0),
            hb.bids.get(h_bid, 0.0), hb.asks.get(h_ask, 0.0),
            max(0.0, now - eb.last_update_ts), max(0.0, now - hb.last_update_ts),
            depth)

    def close(self) -> None:
        """Flush the partial minute and close the file (call on shutdown)."""
        self._flush_agg()
        if self._fh is not None:
            self._fh.close()
            self._fh = self._writer = None

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("recorder sample failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.close()
            log.info("recorder stopped — %d minute row(s) written to %s",
                     self.rows_written, self.path)
