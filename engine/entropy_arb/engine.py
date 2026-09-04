"""Two-venue arbitrage engine: Entropy vs one hedge venue.

The signal is a fixed band around a configured midline (config.yaml):

    SELL entropy / BUY hedge  when executable premium >= midline + upper (+fees)
    BUY entropy / SELL hedge  when executable premium <= midline - lower (+fees)

Around the signal: per-direction persistence arming,
per-venue inventory ladder + position caps, per-venue order budgets and
reactive rate-limit exclusion, net-delta hedging, venue-outage pausing with
probing, and periodic on-chain reconciliation. There is no paper mode: the
bot either trades live or runs --record-only (data collection, no strategy).
Both venues' books are recorded to 1-minute CSV bars throughout.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from collections import deque
from typing import Dict, List, Optional

import aiohttp

from . import maker as mk
from .book import (ArbPlan, MakerPlan, floor_step, maker_edge_bps, plan_arb,
                   plan_maker)
from .config import Config
from .maker import MakerOrder
from .metrics import LatencyBook
from .recorder import MinuteRecorder
from .volatility import VolatilityBreaker
from .venue_hl import HLVenue
from .venue_lighter import LighterVenue

log = logging.getLogger("engine")

CSV_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
              "buy_limit", "sell_limit", "buy_notional", "sell_notional",
              "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
              "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
              "buy_status", "sell_status", "fill_edge_usd"]
# B3: resting quotes need columns an IOC pair has no use for. M2 (fill rate)
# is outcome over rows; M4 (two-leg latency) is first_fill_ms + hedge_ms; M3
# (adverse selection) is mid_at_fill compared against later minute bars.
MAKER_CSV_HEADER = ["ts", "direction", "maker_venue", "hedge_venue", "side",
                    "px", "qty", "filled", "hedged", "status", "outcome",
                    "edge_bps", "exp_edge_usd", "fill_edge_usd", "rest_ms",
                    "first_fill_ms", "hedge_ms", "cancel_ms",
                    "cancel_attempts", "cancel_reason", "mid_at_fill"]
# Shadow mode (2026-09-04). NOT a paper mode: the README's "there is no paper
# mode -- validate with recorded data and tiny position caps, not with
# simulated fills" still stands, and nothing here simulates a fill, a position
# or a PnL. It records the DECISION and stops at the send boundary, which is
# the one step between --record-only (no strategy at all) and real money.
SHADOW_CSV_HEADER = ["ts", "action", "direction", "venue", "other_venue",
                     "side", "qty", "px", "notional", "edge_bps",
                     "exp_edge_usd", "note"]
BALANCE_POLL_SEC = 30.0


class Engine:
    def __init__(self, cfg: Config, record_only: bool = False,
                 shadow: bool = False) -> None:
        self.cfg = cfg
        self.record_only = record_only
        # Shadow: run the whole strategy, send nothing. See SHADOW_CSV_HEADER.
        self.shadow = shadow
        self.shadow_decisions = 0
        self.session: Optional[aiohttp.ClientSession] = None
        self.entropy = None
        self.hedge = None
        self.venues: Dict[str, object] = {}
        self.recorder: Optional[MinuteRecorder] = None
        self.markets_ready = False
        self.stop = asyncio.Event()
        self._update_evt = asyncio.Event()
        self._reconcile_evt = asyncio.Event()
        # per-venue locks: an execution holds both; a reconcile holds one, so
        # a chain read can never race an in-flight order on that venue
        self._venue_locks: Dict[str, asyncio.Lock] = {}
        self._exec_tasks: set = set()
        self.halted = False
        # B5: operator pause is NOT a halt. It stops opening; it never stops
        # hedging, flattening, self-rescue or reconcile, and it is reversible
        # from the control channel. `halted` is not.
        self.paused_by_operator = False
        self.pause_source = ""
        self.flatten_request = False
        self._flatten_evt = asyncio.Event()
        self.control = None
        self._recorder_dead: Optional[str] = None   # G5: sidecar liveness
        self._stale_streak = 0            # B4: consecutive stale evaluations
        # self-rescue state: consecutive flatten attempts that made NO
        # progress, the |net| they were measured against, and a slow-retry
        # tick used after the budget is exhausted.
        self._halt_flattens = 0
        self._halt_last_net: Optional[float] = None
        self._halt_stuck_logged = False
        self._halt_ticks = 0
        self._absurd_skips = 0            # edges refused as too-good-to-be-true
        self.unexplained_events = 0       # position moves we did not cause
        # Round-trip latency, kept as percentiles. The one that matters for
        # this strategy is `cancel`: pulling a quote before it is picked off
        # IS the adverse-selection cost (M3), and an average hides the tail
        # that does the damage.
        self.lat = LatencyBook()
        # Volatility breaker: the only switch here that lifts itself. It
        # stops NEW exposure while a book is moving too fast and leaves
        # hedging, flattening and reconcile alone.
        self.vol = VolatilityBreaker(cfg.vol_window_sec, cfg.vol_max_move_bps,
                                     cfg.vol_cooldown_sec)
        self.consec_errors = 0
        self.last_trade_ts = 0.0
        self.trades = 0
        self.hedges = 0
        self.total_exp_edge = 0.0
        self.total_fill_edge = 0.0
        self.start_ts = time.time()
        self._last_skiplog = 0.0
        self._poke_due: Optional[float] = None
        # per-direction persistence arming: direction key -> first-seen ts
        self._armed: Dict[str, Optional[float]] = {"sell_entropy": None,
                                                   "buy_entropy": None}
        self._step = 1e-4
        self._min_base = 0.0
        self._min_notional = 10.0
        self._mtm_baseline: Optional[float] = None
        # proactive per-venue send budget: timestamps of recent order sends
        self._sends: Dict[str, deque] = {}
        # reactive per-venue throttle: venue key -> excluded until
        self._venue_limited_until: Dict[str, float] = {}
        # venue outage tracking: key -> down-since ts; a down venue pauses
        # trading and is probed every venue_probe_sec until it answers
        self._venue_down: Dict[str, float] = {}
        self._venue_probe_at: Dict[str, float] = {}
        self._venue_fetch_fails: Dict[str, int] = {}
        # per-execution records for the dashboard (newest last)
        self.recent_trades: deque = deque(maxlen=50)
        # B3 maker path: venue key -> the one order resting there. An order
        # stays in this map until the EXCHANGE resolves it, so an unconfirmed
        # cancel blocks the next quote instead of being quietly forgotten.
        self._maker_open: Dict[str, MakerOrder] = {}
        self.maker_posts = 0        # quotes attempted
        self.maker_rested = 0       # quotes confirmed ON the book -> M2 denom
        self.maker_fills = 0        # quotes that got any fill -> M2 numerator
        self.maker_cancels = 0      # quotes cancelled unfilled
        self.maker_rejects = 0      # post-only rejections (book moved)
        self.maker_unknown = 0      # cancels that blew their budget

    # ------------------------------------------------------------- utilities

    def _vlock(self, key: str) -> asyncio.Lock:
        lock = self._venue_locks.get(key)
        if lock is None:
            lock = self._venue_locks[key] = asyncio.Lock()
        return lock

    def _venue_rate_ok(self, v) -> bool:
        """True while the venue is under its max_orders_per_min (sliding 60s)."""
        dq = self._sends.setdefault(v.key, deque())
        now = time.time()
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        return len(dq) < v.orders_per_min

    def _venue_limited(self, v) -> bool:
        return time.time() < self._venue_limited_until.get(v.key, 0.0)

    def _mark_limited(self, v) -> None:
        self._venue_limited_until[v.key] = time.time() + self.cfg.rate_limit_pause_sec
        log.warning("[%s] rate limited — trading paused for %.0fs",
                    v.name, self.cfg.rate_limit_pause_sec)

    def _record_send(self, v) -> None:
        self._sends.setdefault(v.key, deque()).append(time.time())

    def request_stop(self) -> None:
        self.stop.set()
        self._update_evt.set()
        self._reconcile_evt.set()
        self._flatten_evt.set()

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        # Long keepalive so order-path connections survive quiet spells; the
        # keepalive loop pings inside this window to hold them open.
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
            keepalive_timeout=75.0, ttl_dns_cache=300))
        try:
            await self._run_inner()
        finally:
            await self.session.close()

    # Startup market load, with retries. A single transient 403/405 from a
    # CDN in front of a venue used to be fatal: the process died before it
    # could do anything, and on a VPS that means waiting for a watchdog.
    # Measured 2026-09-05: Lighter's REST answered 405 for ~minutes (burst
    # rate limiting) and then went back to 200 on its own. That is a wait,
    # not a failure.
    MARKET_LOAD_TRIES = 5

    async def _load_markets(self) -> None:
        delay = 2.0
        for attempt in range(1, self.MARKET_LOAD_TRIES + 1):
            try:
                await asyncio.gather(self.entropy.load_market(),
                                     self.hedge.load_market())
                if attempt > 1:
                    log.warning("markets loaded on attempt %d", attempt)
                return
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                raise      # "not found" / "delisted" -- retrying cannot help
            except Exception as e:                              # noqa: BLE001
                if attempt == self.MARKET_LOAD_TRIES:
                    raise RuntimeError(
                        f"could not load markets after "
                        f"{self.MARKET_LOAD_TRIES} attempts: {e!r}") from e
                log.warning("market load attempt %d/%d failed (%r) — "
                            "retrying in %.0fs", attempt,
                            self.MARKET_LOAD_TRIES, e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def _make_venue(self, vc):
        if vc.kind == "lighter":
            return LighterVenue(vc, self.session, self.cfg.settle_timeout_sec)
        return HLVenue(vc, self.cfg.hl_api_url, self.cfg.hl_ws_url,
                       self.session, self.cfg.settle_timeout_sec)

    async def _run_inner(self) -> None:
        cfg = self.cfg
        self.entropy = self._make_venue(cfg.entropy)
        self.hedge = self._make_venue(cfg.hedge)
        self.venues = {"entropy": self.entropy, "hedge": self.hedge}
        await self._load_markets()
        self.markets_ready = True

        live = not self.record_only
        if live and self.shadow:
            # A rehearsal that cannot start is not a rehearsal, so shadow
            # warns where live refuses -- but it says exactly what live would
            # have refused on, because finding that out at go-live is the
            # failure this mode exists to prevent.
            try:
                self._require_armed_risk_block()
            except RuntimeError as e:
                log.warning("[SHADOW] live would REFUSE to start — %s",
                            str(e).replace(chr(10), " | "))
        elif live:
            self._require_armed_risk_block()
            if not cfg.creds_complete:
                raise RuntimeError(
                    "live trading needs credentials for both venues in .env "
                    "(see .env.example); use --record-only to run without "
                    "them / 实盘需要在 .env 中配置两个交易所的密钥，仅采集数据"
                    "请用 --record-only")
            self.entropy.init_signer()
            self.hedge.init_signer()
            if self.hedge.kind == "hl":
                self.entropy.share_nonces_with(self.hedge)
        if live and self.shadow:
            log.warning("SHADOW — the full strategy runs and NOTHING is sent. "
                        "No fills are simulated, no positions are invented, no "
                        "PnL is claimed: decisions go to %s and that is all. / "
                        "影子模式：策略照跑，一张单都不送；不模拟成交、不虚构"
                        "持仓、不宣称损益。", cfg.shadow_csv)
        if (self.hedge.kind == "hl"
                and self.entropy._query_address()
                and self.entropy._query_address() == self.hedge._query_address()):
            self.hedge.include_core_equity = False  # shared account: count once

        self._step = 10 ** -min(self.entropy.size_decimals,
                                self.hedge.size_decimals)
        self._min_base = max(self.entropy.min_base, self.hedge.min_base,
                             self._step)
        self._min_notional = max(cfg.min_order_notional,
                                 self.entropy.min_quote, self.hedge.min_quote)
        log.info("pair ENTROPY(%s)-%s(%s): midline=%+.2fbps band=[-%.2f, +%.2f] "
                 "fees=%.2f+%.2f step=%g min_ntl=$%g",
                 self.entropy.conf.symbol, self.hedge.name,
                 self.hedge.conf.symbol, cfg.midline_bps, cfg.lower_bps,
                 cfg.upper_bps, self.entropy.fee_bps, self.hedge.fee_bps,
                 self._step, self._min_notional)

        if self.record_only:
            log.warning("RECORD-ONLY — collecting minute data, no strategy, "
                        "no orders")
        elif self.shadow:
            # Read real positions if the credentials happen to be there --
            # it proves the read path works -- but never insist, and never
            # touch the book with a cancel sweep.
            try:
                await self._reconcile_positions(hedge=False)
            except Exception as e:                              # noqa: BLE001
                log.warning("[SHADOW] could not read starting positions "
                            "(%r) — continuing with zero", e)
        else:
            log.warning("LIVE — real orders will be sent (use --record-only "
                        "for credential-less data collection)")
            await self._cancel_stale_orders()
            await self._reconcile_positions(hedge=False, strict=True)
            log.info("starting positions: %s (net %+.6g)",
                     " ".join(f"{v.name}={v.position:+.6g}"
                              for v in self.venues.values()),
                     sum(v.position for v in self.venues.values()))

        tasks: List[asyncio.Task] = []
        for v in self.venues.values():
            tasks += v.start_tasks(self.stop, self._update_evt.set, live)
        if cfg.recorder_enabled or self.record_only:
            # Funding poller (local patch 2026-09-01): the price spread must
            # CONVERGE to pay; the funding differential pays for HOLDING.
            # Recording both is what lets the verdict tell a stable carry
            # from a number that flips daily. Never fatal — a poller that
            # cannot start just leaves the columns blank.
            funding = None
            try:
                from entropy_arb.funding import FundingPoller
                if self.hedge.kind == "lighter":
                    funding = FundingPoller(
                        hl_coin=self.entropy.conf.symbol,
                        hl_dex=self.entropy.conf.hl_dex,
                        lighter_venue=cfg.hedge_venue,
                        lighter_symbol=self.hedge.conf.symbol,
                        entropy_lighter_venue=(
                            self.entropy.conf.lighter_venue
                            if self.entropy.kind == "lighter" else None))
                    funding.start()
            except Exception as e:                       # noqa: BLE001
                log.warning("funding poller unavailable (columns blank): %r", e)
                funding = None
            self.recorder = MinuteRecorder(cfg.recorder_csv, self.entropy.book,
                                           self.hedge.book, cfg.staleness_sec,
                                           funding=funding)
            rec_task = asyncio.create_task(self.recorder.run(self.stop),
                                           name="recorder")
            # G5 (2026-09-04, principle 7): an asyncio task that dies of an
            # unhandled exception does so SILENTLY -- the engine would keep
            # trading while believing the recorder is still writing. That is
            # this project's oldest disease (process alive, function dead).
            # The callback does not stop trading: a sidecar failure must not
            # take down the hot path. It only makes the death visible.
            rec_task.add_done_callback(self._recorder_died)
            tasks.append(rec_task)
        if not self.record_only:
            tasks.append(asyncio.create_task(self._strategy_loop(),
                                             name="strategy"))
            tasks.append(asyncio.create_task(self._balance_loop(),
                                             name="balances"))
            tasks.append(asyncio.create_task(self._http_keepalive_loop(),
                                             name="keepalive"))
        tasks.append(asyncio.create_task(self._status_loop(), name="status"))
        if live:
            tasks.append(asyncio.create_task(self._reconcile_loop(),
                                             name="reconcile"))
            tasks.append(asyncio.create_task(self._flatten_loop(),
                                             name="flatten"))
            if cfg.control_enabled:
                from .control import ControlChannel, make_relay
                self.control = ControlChannel(self, cfg)
                if cfg.control_notify_critical:
                    # Every guard in this engine shouts at CRITICAL. Relaying
                    # those to the same channel is the difference between a
                    # switch that fired and a switch you find out about
                    # tomorrow.
                    self.control.relay = make_relay()
                    logging.getLogger().addHandler(self.control.relay)
                tasks.append(asyncio.create_task(
                    self.control.run(self.stop), name="control"))
                log.info("control channel: %s + command file %s",
                         "telegram" if cfg.tg_bot_token and cfg.tg_chat_id
                         else "NO telegram credentials",
                         cfg.control_command_file)

        await self.stop.wait()
        if self._exec_tasks:  # let in-flight executions settle, never cancel
            log.info("waiting for %d in-flight execution(s) to settle",
                     len(self._exec_tasks))
            await asyncio.wait(
                self._exec_tasks,
                # a resting quote has to be cancelled AND confirmed before
                # this process may exit, so its budget is part of the wait
                timeout=(cfg.settle_timeout_sec
                         + (cfg.cancel_timeout_sec * 2 + 3.0
                            if cfg.mode == "maker" else 0.0) + 2.0))
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for v in self.venues.values():
            await v.close()
        log.info("shutdown — %d trades, %d hedges, exp edge $%.4f, "
                 "fill edge $%.4f", self.trades, self.hedges,
                 self.total_exp_edge, self.total_fill_edge)

    # --------------------------------------------------------------- signals

    def _inv_add_bps(self, buy, sell) -> float:
        """Inventory ladder: a surcharge that grows once a venue's position
        passes floor_frac of its cap in the direction the trade would add to
        (buying adds when that venue is >= flat long; selling adds when the
        venue is <= flat short). Max of the two venues' ramps."""
        scale = self.cfg.inventory_scale_bps
        if scale <= 0:
            return 0.0
        floor = min(max(self.cfg.inventory_floor_frac, 0.0), 0.99)

        def ramp(v, adding: bool) -> float:
            if not adding:
                return 0.0
            ref = v.book.mid()
            if ref is None:
                return 0.0
            u = min(abs(v.position) * ref / v.cap_usd, 1.0)
            if u <= floor:
                return 0.0
            return scale * (u - floor) / (1.0 - floor)

        return max(ramp(buy, buy.position >= 0), ramp(sell, sell.position <= 0))

    def _eff_threshold(self, buy, sell) -> float:
        """Net hurdle (bps, on top of fees) for the direction buy->sell.

        selling entropy: executable premium must clear midline + upper;
        buying entropy: the reverse premium must clear lower - midline."""
        if sell.key == "entropy":
            base = self.cfg.midline_bps + self.cfg.upper_bps
        else:
            base = self.cfg.lower_bps - self.cfg.midline_bps
        return base + self._inv_add_bps(buy, sell)

    def _headroom(self, buy, sell, ref_px: float) -> float:
        hb = buy.cap_usd - buy.position * ref_px
        hs = sell.cap_usd + sell.position * ref_px
        return min(hb, hs)

    def _plan(self, buy, sell, cap_notional: float):
        return plan_arb(
            buy.book, sell.book,
            threshold_bps=self._eff_threshold(buy, sell),
            buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
            take_fraction=self.cfg.take_fraction,
            cap_notional=cap_notional,
            min_base=self._min_base,
            min_notional=self._min_notional,
            size_step=self._step,
        )

    # -------------------------------------------------------------- strategy

    async def _strategy_loop(self) -> None:
        while not self.stop.is_set():
            await self._update_evt.wait()
            self._update_evt.clear()
            if self.stop.is_set():
                break
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("evaluate failed")

    def _schedule_poke(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + max(delay, 0.01)
        if self._poke_due is not None and self._poke_due <= due + 0.02:
            return

        def _fire() -> None:
            self._poke_due = None
            self._update_evt.set()

        self._poke_due = due
        loop.call_at(due, _fire)

    def _skiplog(self, fmt: str, *args) -> None:
        now = time.time()
        if now - self._last_skiplog >= 2.0:
            self._last_skiplog = now
            log.info(fmt, *args)

    async def _evaluate(self) -> None:
        cfg = self.cfg
        if self.halted:
            return
        now = time.time()
        # Only fresh books are sampled. During an outage the mid does not
        # change but time does, and measuring the jump on reconnection would
        # report how far the market moved while we were blind -- a fact
        # about the connection, not about volatility.
        for v in self.venues.values():
            if v.book.is_fresh(cfg.staleness_sec):
                self.vol.observe(v.key, v.book.mid(), now)
        tripped = self.vol.check(now)
        if tripped:
            log.critical(
                "VOLATILITY PAUSE: %s — opening no NEW exposure for %.0fs. "
                "Hedging, flattening, self-rescue and reconcile continue "
                "unchanged. / 波动熔断：暂停开新仓 %.0f 秒；对冲、平仓、"
                "自救与对帐不受影响。", tripped, cfg.vol_cooldown_sec,
                cfg.vol_cooldown_sec)
        if self.paused_by_operator:
            self._skiplog("paused by %s — opening nothing; hedging, "
                          "flattening and reconcile continue",
                          self.pause_source or "operator")
            return
        if self.vol.paused(now):
            left = self.vol.remaining(now)
            self._skiplog("volatility pause: %.0fs left — %s", left,
                          self.vol.reason)
            self._schedule_poke(min(max(left, 0.1), 5.0))
            return
        if now - self.last_trade_ts < cfg.cooldown_sec:
            self._schedule_poke(cfg.cooldown_sec - (now - self.last_trade_ts))
            return
        if cfg.mode == "maker":
            await self._evaluate_maker(now)
            return
        best = self._scan(now)
        if best is None:
            return
        buy, sell, plan = best
        # _scan verified both locks free and nothing ran since (no awaits),
        # so these acquires take the no-suspension fast path
        await self._vlock(buy.key).acquire()
        await self._vlock(sell.key).acquire()
        # run as a task so a shutdown cancels the strategy loop's await, never
        # the in-flight execution itself (both legs must settle)
        t = asyncio.create_task(self._execute_locked(buy, sell, plan))
        self._exec_tasks.add(t)
        t.add_done_callback(self._exec_tasks.discard)
        await asyncio.shield(t)

    async def _execute_locked(self, buy, sell, plan: ArbPlan) -> None:
        """Run one execution while holding both venue locks (acquired by the
        caller), then release them and settle the aftermath: unresolved
        outcomes escalate to reconcile, everything else gets a net-delta
        check."""
        unresolved = False
        try:
            unresolved = await self._execute(buy, sell, plan)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("execute failed")
        finally:
            self._vlock(buy.key).release()
            self._vlock(sell.key).release()
        if unresolved:
            self._reconcile_evt.set()
        else:
            await self._maybe_hedge()
        self._update_evt.set()  # freed venues may have a queued opportunity

    def _scan(self, now: float):
        """Evaluate both directions; returns the best executable
        (buy, sell, plan), or None."""
        cfg = self.cfg
        best = None
        for buy, sell, dkey in ((self.hedge, self.entropy, "sell_entropy"),
                                (self.entropy, self.hedge, "buy_entropy")):
            fresh = (buy.book.is_fresh(cfg.staleness_sec)
                     and sell.book.is_fresh(cfg.staleness_sec))
            if fresh:
                self._stale_streak = 0
            else:
                self._stale_streak += 1
                if (cfg.max_consecutive_stale
                        and self._stale_streak >= cfg.max_consecutive_stale):
                    self._risk_halt(
                        f"books stale {self._stale_streak} evaluations in a "
                        f"row (> {cfg.staleness_sec:.0f}s each) — a dead feed "
                        f"looks exactly like a quiet market")
                    return None
            if not fresh:
                continue
            if not (self._ready(buy) and self._ready(sell)):
                continue
            if self._venue_down:
                continue  # a venue in outage pauses the (only) pair
            if self._vlock(buy.key).locked() or self._vlock(sell.key).locked():
                continue  # mid-execution or mid-reconcile
            if self._venue_limited(buy) or self._venue_limited(sell):
                continue  # reactive 429 exclusion
            if not (self._venue_rate_ok(buy) and self._venue_rate_ok(sell)):
                self._skiplog("%s deferred: venue order budget exhausted", dkey)
                continue
            # never refire into books that predate the venue's own last trade
            if (buy.book.last_update_ts <= buy.last_traded_ts
                    or sell.book.last_update_ts <= sell.last_traded_ts):
                continue
            plan, reason = self._plan(buy, sell, cfg.max_order_notional)
            edge_present = reason not in ("no_edge", "empty_book")
            if not edge_present:
                self._armed[dkey] = None
                continue
            armed = self._armed.get(dkey)
            if armed is None:
                # premium persistence: only fire if the edge survives
                # premium_persist_sec (filters one-tick phantoms)
                self._armed[dkey] = now
                self._schedule_poke(cfg.premium_persist_sec)
                continue
            if now - armed < cfg.premium_persist_sec:
                self._schedule_poke(cfg.premium_persist_sec - (now - armed))
                continue
            if plan is None:
                continue
            # too good to be true (2026-09-04). Measured bands on this family
            # are 2-15 bps; a reading in the hundreds means the book is wrong,
            # not that the market is generous. Two of our own instruments were
            # delisted mid-recording, which is exactly the shape that leaves a
            # wide, stale book still answering REST.
            ceil_bps = cfg.max_edge_bps
            if ceil_bps > 0 and plan.top_premium_bps > ceil_bps:
                self._absurd_skips += 1
                self._skiplog("%s REFUSED: premium %.1f bps exceeds "
                              "max_edge_bps %.1f — treating the book as wrong, "
                              "not the market as generous (skips=%d)",
                              dkey, plan.top_premium_bps, ceil_bps,
                              self._absurd_skips)
                self._armed[dkey] = None
                continue
            headroom = self._headroom(buy, sell, plan.buy_limit)
            if headroom < plan.buy_notional:
                plan, _ = self._plan(buy, sell,
                                     min(cfg.max_order_notional, headroom))
                if plan is None:
                    self._skiplog("%s blocked by position caps (headroom $%.0f)",
                                  dkey, max(headroom, 0.0))
                    continue
            if best is None or plan.exp_edge_usd > best[2].exp_edge_usd:
                best = (buy, sell, plan)
        return best

    # ------------------------------------------------------------- execution

    async def _execute(self, buy, sell, plan: ArbPlan) -> bool:
        """Send both legs and settle the fills. Both venue locks are held by
        the caller. Returns True when an outcome is unresolved and the caller
        must escalate to reconcile."""
        if self.halted:
            return False
        cfg = self.cfg
        inv_bps = self._inv_add_bps(buy, sell)
        direction = "sell_entropy" if sell.key == "entropy" else "buy_entropy"
        self.last_trade_ts = time.time()
        log.info("[ARB] %s: BUY %s %.6g @<=%.6g | SELL %s @>=%.6g | "
                 "take $%.0f of $%.0f | prem %.2fbps | exp $%.4f",
                 direction, buy.name, plan.qty, plan.buy_limit, sell.name,
                 plan.sell_limit, plan.buy_notional, plan.q_max_notional,
                 plan.marginal_premium_bps, plan.exp_edge_usd)
        slip = cfg.leg_slippage_bps / 1e4
        buy_bound = buy.px_round(plan.buy_limit * (1 + slip), round_up=False)
        sell_bound = sell.px_round(plan.sell_limit * (1 - slip), round_up=True)
        if self._blocked("arb", buy, other=sell, side="BUY+SELL", qty=plan.qty,
                         px=plan.buy_limit, edge_bps=plan.marginal_premium_bps,
                         exp_edge_usd=plan.exp_edge_usd, note=direction):
            return False
        self._record_send(buy)
        self._record_send(sell)
        _t0 = time.time()
        res = await asyncio.gather(
            buy.send_taker(is_buy=True, qty=plan.qty, limit_px=buy_bound),
            sell.send_taker(is_buy=False, qty=plan.qty, limit_px=sell_bound),
            return_exceptions=True)
        self.lat.add("arb", (time.time() - _t0) * 1e3)
        binfo, sinfo = (r if isinstance(r, dict) else
                        {"status": "send-failed", "filled_base": 0.0,
                         "avg_px": None, "err": repr(r), "unresolved": False}
                        for r in res)
        for v, info, side in ((buy, binfo, "buy"), (sell, sinfo, "sell")):
            if info.get("err"):
                log.error("[%s] %s leg: %s", v.name, side, info["err"])
        bfill = binfo["filled_base"]
        sfill = sinfo["filled_base"]
        buy.position += bfill
        sell.position -= sfill
        if bfill:
            bpx = binfo.get("avg_px") or plan.buy_limit
            buy.cash -= bfill * bpx * (1 + plan.buy_fee)
            buy.volume_usd += bfill * bpx
        if sfill:
            spx = sinfo.get("avg_px") or plan.sell_limit
            sell.cash += sfill * spx * (1 - plan.sell_fee)
            sell.volume_usd += sfill * spx

        matched = min(bfill, sfill)
        fill_edge = 0.0
        if matched > 0 and binfo.get("avg_px") and sinfo.get("avg_px"):
            fill_edge = matched * (sinfo["avg_px"] * (1 - plan.sell_fee)
                                   - binfo["avg_px"] * (1 + plan.buy_fee))
            self.total_fill_edge += fill_edge
        log.info("[SETTLED] %s: buy %s %s %.6g/%.6g | sell %s %s %.6g/%.6g | "
                 "matched %.6g | fill edge $%.4f", direction,
                 buy.name, binfo["status"], bfill, plan.qty,
                 sell.name, sinfo["status"], sfill, plan.qty, matched, fill_edge)
        buy.last_traded_ts = sell.last_traded_ts = time.time()

        unresolved = binfo.get("unresolved") or sinfo.get("unresolved")
        # B1 audit G2 (2026-09-04): on an unresolved outcome filled_base is a
        # GUESS (send_taker returns 0.0 on settle timeout), so the local
        # position written above may be wrong until reconcile reads the
        # chain. Say so loudly; the reconcile triggered by the caller is what
        # repairs it. Never let a guessed fill look like a fact.
        if unresolved:
            log.warning("[UNRESOLVED] local positions may be stale until "
                        "reconcile (buy %s / sell %s)",
                        binfo.get("status"), sinfo.get("status"))
        hard_err = (binfo.get("err") is not None
                    or sinfo.get("err") is not None)
        rate_limited = False
        for v, info in ((buy, binfo), (sell, sinfo)):
            if str(info.get("err", "")).startswith("RATE_LIMITED"):
                rate_limited = True
                self._mark_limited(v)
            elif "margin" in str(info.get("status", "")).lower():
                log.warning("[%s] margin rejection — collateral exhausted, "
                            "pausing venue", v.name)
                self._mark_limited(v)
        sent_ok = not hard_err and not unresolved
        if sent_ok:
            self.consec_errors = 0
        elif not rate_limited:
            self.consec_errors += 1
            if self.consec_errors >= cfg.max_consecutive_errors:
                self.halted = True
                log.critical("HALTED after %d consecutive execution problems "
                             "— flatten manually and restart / 连续执行异常，"
                             "引擎已停止，请手动平仓后重启", self.consec_errors)
        if sent_ok:
            self.trades += 1
            self.total_exp_edge += plan.exp_edge_usd
        self._record_trade(direction, plan,
                           None if unresolved else fill_edge,
                           f"{binfo['status']}/{sinfo['status']}", sent_ok)
        self._log_csv(direction, buy, sell, plan, sent_ok, bfill, sfill,
                      binfo["status"], sinfo["status"], fill_edge, inv_bps)
        self.last_trade_ts = time.time()
        return bool(unresolved)

    def _record_trade(self, direction: str, plan: ArbPlan, fill_edge,
                      status: str, ok: bool) -> None:
        self.recent_trades.append({
            "ts": time.time(), "direction": direction, "qty": plan.qty,
            "notional": plan.buy_notional,
            "prem_bps": plan.marginal_premium_bps,
            "exp": plan.exp_edge_usd, "fill": fill_edge, "status": status,
            "ok": ok})

    # ------------------------------------------------------------ maker (B3)
    #
    # The taker path sends both legs at once and learns both outcomes in one
    # round trip. The maker path posts ONE leg, waits, and hedges whatever
    # comes back -- which means that for the whole life of the quote the
    # exchange, not this process, knows what our position is. Every method
    # below is written around that: local state is a shadow of exchange
    # reports (entropy_arb/maker.py), and nothing here concludes anything
    # from an action we took.

    def _maker_legs(self):
        """(the venue we rest on, the venue we hedge on)."""
        maker_v = (self.entropy if self.cfg.maker_venue == "entropy"
                   else self.hedge)
        taker_v = self.hedge if maker_v is self.entropy else self.entropy
        return maker_v, taker_v

    def _plan_quote(self, maker_v, taker_v, maker_is_buy, thr_bps, cap):
        ref = maker_v.book.mid()
        if not ref:
            return None, "empty_book"
        return plan_maker(
            maker_v.book, taker_v.book, maker_is_buy=maker_is_buy,
            threshold_bps=thr_bps,
            maker_fee_bps=maker_v.maker_fee_bps,
            hedge_fee_bps=taker_v.fee_bps,
            take_fraction=self.cfg.take_fraction, cap_notional=cap,
            min_base=self._min_base, min_notional=self._min_notional,
            size_step=self._step, px_round=maker_v.px_round,
            tick=maker_v.px_tick(ref))

    def _scan_maker(self, now):
        """Best post-only quote, or None.

        Same guards as _scan, plus two of its own: only one quote may be
        alive at a time (an unresolved order blocks the next one by design),
        and the size comes from plan_maker, which bounds it by the HEDGE
        venue's depth rather than our own.
        """
        cfg = self.cfg
        if self._maker_open:
            return None
        maker_v, taker_v = self._maker_legs()
        fresh = (maker_v.book.is_fresh(cfg.staleness_sec)
                 and taker_v.book.is_fresh(cfg.staleness_sec))
        if fresh:
            self._stale_streak = 0
        else:
            self._stale_streak += 1
            if (cfg.max_consecutive_stale
                    and self._stale_streak >= cfg.max_consecutive_stale):
                self._risk_halt(
                    f"books stale {self._stale_streak} evaluations in a row "
                    f"(> {cfg.staleness_sec:.0f}s each) — a dead feed looks "
                    f"exactly like a quiet market")
            return None
        if not (self._ready(maker_v) and self._ready(taker_v)):
            return None
        if self._venue_down:
            return None
        if self._vlock(maker_v.key).locked() or self._vlock(taker_v.key).locked():
            return None
        if self._venue_limited(maker_v) or self._venue_limited(taker_v):
            return None
        if not (self._venue_rate_ok(maker_v) and self._venue_rate_ok(taker_v)):
            self._skiplog("maker quote deferred: venue order budget exhausted")
            return None
        best = None
        for maker_is_buy in (True, False):
            # posting a bid on venue M means we BUY on M and SELL on the
            # other; the hurdle is the same band the taker path uses.
            buy_v, sell_v = ((maker_v, taker_v) if maker_is_buy
                             else (taker_v, maker_v))
            dkey = "sell_entropy" if sell_v.key == "entropy" else "buy_entropy"
            plan, reason = self._plan_quote(maker_v, taker_v, maker_is_buy,
                                            self._eff_threshold(buy_v, sell_v),
                                            cfg.max_order_notional)
            if plan is None:
                if reason in ("no_edge", "empty_book", "crossed_book",
                              "no_hedge_depth", "would_cross"):
                    self._armed[dkey] = None
                continue
            armed = self._armed.get(dkey)
            if armed is None:
                self._armed[dkey] = now
                self._schedule_poke(cfg.premium_persist_sec)
                continue
            if now - armed < cfg.premium_persist_sec:
                self._schedule_poke(cfg.premium_persist_sec - (now - armed))
                continue
            ceil_bps = cfg.max_edge_bps
            if ceil_bps > 0 and plan.top_premium_bps > ceil_bps:
                self._absurd_skips += 1
                self._skiplog("%s REFUSED: premium %.1f bps exceeds "
                              "max_edge_bps %.1f — treating the book as wrong, "
                              "not the market as generous (skips=%d)",
                              dkey, plan.top_premium_bps, ceil_bps,
                              self._absurd_skips)
                self._armed[dkey] = None
                continue
            headroom = self._headroom(buy_v, sell_v, plan.maker_px)
            if headroom < plan.maker_notional:
                plan, _ = self._plan_quote(
                    maker_v, taker_v, maker_is_buy,
                    self._eff_threshold(buy_v, sell_v),
                    min(cfg.max_order_notional, headroom))
                if plan is None:
                    self._skiplog("%s quote blocked by position caps "
                                  "(headroom $%.0f)", dkey, max(headroom, 0.0))
                    continue
            if best is None or plan.exp_edge_usd > best[2].exp_edge_usd:
                best = (maker_is_buy, dkey, plan)
        return best

    async def _evaluate_maker(self, now: float) -> None:
        best = self._scan_maker(now)
        if best is None:
            return
        maker_is_buy, dkey, plan = best
        maker_v, taker_v = self._maker_legs()
        order = MakerOrder(venue_key=maker_v.key, is_buy=maker_is_buy,
                           qty=plan.qty, px=plan.maker_px, sent_ts=now)
        order.stats["dkey"] = dkey
        # Registered BEFORE the first await. _scan_maker refuses to plan a
        # second quote while one exists, and that check must not be able to
        # race the task that creates the first.
        self._maker_open[maker_v.key] = order
        t = asyncio.create_task(
            self._execute_maker(maker_v, taker_v, plan, order),
            name="maker")
        self._exec_tasks.add(t)
        t.add_done_callback(self._exec_tasks.discard)
        await asyncio.shield(t)
        self._update_evt.set()

    async def _execute_maker(self, maker_v, taker_v, plan: MakerPlan,
                             order: MakerOrder) -> None:
        """One quote, start to finish. The wrapper exists so that the order
        is retired through exactly one path: a crash inside the order loop
        must not leave a possibly-live quote counted as nothing."""
        try:
            await self._quote(maker_v, taker_v, plan, order)
        except asyncio.CancelledError:
            raise
        except Exception:                                       # noqa: BLE001
            log.exception("[QUOTE] order loop crashed — %s", order.describe())
            self._risk_halt("the maker order loop crashed while an order may "
                            "still be live on the venue")
        finally:
            await self._finish_maker(maker_v, taker_v, plan, order)

    async def _quote(self, maker_v, taker_v, plan: MakerPlan,
                     order: MakerOrder) -> None:
        cfg = self.cfg
        dkey = order.stats.get("dkey", "?")
        self.maker_posts += 1
        self.last_trade_ts = time.time()
        log.info("[QUOTE] %s: rest %s %.6g @%.6g on %s | hedge on %s @~%.6g | "
                 "edge %.2fbps | exp $%.4f | hedgeable %.6g",
                 dkey, "BUY" if order.is_buy else "SELL", plan.qty,
                 plan.maker_px, maker_v.name, taker_v.name, plan.hedge_limit,
                 plan.edge_bps, plan.exp_edge_usd, plan.hedge_depth)
        if self._blocked("quote", maker_v, other=taker_v,
                         side="BUY" if order.is_buy else "SELL", qty=plan.qty,
                         px=plan.maker_px, edge_bps=plan.edge_bps,
                         exp_edge_usd=plan.exp_edge_usd, note=dkey):
            order.apply("canceled")     # nothing rested; retire it cleanly
            return
        _t0 = time.time()
        try:
            async with self._vlock(maker_v.key):
                self._record_send(maker_v)
                res = await maker_v.send_maker(is_buy=order.is_buy,
                                               qty=plan.qty,
                                               limit_px=plan.maker_px)
            self.lat.add("quote", (time.time() - _t0) * 1e3)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001
            log.exception("[QUOTE] %s: send raised on %s", dkey, maker_v.name)
            res = {"status": "send-unresolved", "handle": None,
                   "filled_base": 0.0, "avg_px": None, "err": repr(e),
                   "unresolved": True}
        order.on_handle(res.get("handle"))
        status = str(res.get("status") or "")
        err = res.get("err")
        if str(err or "").startswith("RATE_LIMITED"):
            self._mark_limited(maker_v)

        if status == "post-only-reject":
            # The book moved between planning and sending. Not a failure --
            # it is the normal cost of insisting on being the passive side.
            self.maker_rejects += 1
            self._skiplog("[QUOTE] %s: post-only rejected on %s — the book "
                          "moved between planning and sending (x%d)",
                          dkey, maker_v.name, self.maker_rejects)
            order.apply("canceled-post-only")
            return
        if status == "send-failed":
            log.error("[QUOTE] %s: send failed on %s: %s", dkey,
                      maker_v.name, err)
            if not str(err or "").startswith("RATE_LIMITED"):
                self.consec_errors += 1
                if self.consec_errors >= cfg.max_consecutive_errors:
                    self._risk_halt(f"{self.consec_errors} consecutive "
                                    f"execution problems")
            # A rejected send never reached the book: the exchange told us so
            # in the same response. This is the ONE place local state may
            # retire an order without a poll, and only because the rejection
            # IS the exchange's report.
            order.apply("rejected")
            return
        if status == "filled":
            order.apply("filled", res.get("filled_base"), res.get("avg_px"))
        elif status == "resting":
            order.apply("open")
        elif status == "send-unresolved":
            # We do not know whether it is on the book. The handle is ours
            # and already allocated, so the pessimistic action is available:
            # cancel it. Cancelling an order that was never placed costs one
            # request; assuming it was never placed costs a naked position.
            log.warning("[QUOTE] %s: send outcome UNKNOWN on %s (%s) — "
                        "treating the order as LIVE and cancelling it",
                        dkey, maker_v.name, err)
            order.stats["force"] = "send outcome unknown"
            if order.handle is None:
                # Both venues allocate the id before the network call, so
                # this only happens when the venue adapter itself blew up.
                # We may have an order on the book that we cannot address --
                # there is no pessimistic ACTION available, only a halt.
                self._risk_halt(
                    "a maker send failed before the order could be named — "
                    "an order may be live on %s with no id to cancel it by; "
                    "check the venue by hand / 挂单在拿到编号前就失败，可能"
                    "有无法撤销的挂单，请手动到交易所确认" % maker_v.name)
                return
        await self._maker_lifecycle(maker_v, taker_v, plan, order)

    async def _maker_lifecycle(self, maker_v, taker_v, plan: MakerPlan,
                               order: MakerOrder) -> None:
        """Watch one resting order to a resolved end.

        The loop does exactly four things per turn, in this order: read the
        exchange, act on any new fill, decide whether the quote should still
        be alive, and enforce the cancel's own budget.
        """
        cfg = self.cfg
        base_poll = max(cfg.maker_poll_sec, 0.05)
        deadline = order.sent_ts + cfg.maker_timeout_sec
        cancel_backoff = max(base_poll, 0.5)
        last_cancel_send = 0.0
        last_unknown_log = 0.0
        stop_deadline = None
        while True:
            now = time.time()
            info = await self._poll_maker(maker_v, order)
            if info is not None:
                order.apply(info["status"], info.get("filled_base"),
                            info.get("avg_px"))
            # The venue lock is held across booking AND hedging: between
            # writing the fill into maker_v.position and the hedge landing,
            # the net-delta hedge would otherwise see a one-sided position
            # and reduce it -- and then our hedge would land on top, leaving
            # the imbalance mirrored instead of removed. _hedge skips a
            # locked venue and carries to the next reconcile, which is the
            # right outcome for those few hundred milliseconds.
            async with self._vlock(maker_v.key):
                await self._consume_maker_fill(maker_v, taker_v, order)
            if order.is_terminal:
                break
            reason = self._maker_cancel_reason(maker_v, taker_v, order, now,
                                               deadline)
            if reason is not None:
                first = order.cancel_ts is None
                if first:
                    order.stats["cancel_reason"] = reason
                    log.info("[QUOTE] cancelling — %s | %s", reason,
                             order.describe())
                if first or now - last_cancel_send >= cancel_backoff:
                    order.request_cancel(now)
                    last_cancel_send = now
                    if not first:
                        cancel_backoff = min(cancel_backoff * 2, 5.0)
                    await self._send_maker_cancel(maker_v, order)
                # PEER_INFRA S7 must #3. An unconfirmed cancel is not a
                # cancelled order; it is an order that may have filled. The
                # engine says so out loud and goes to the chain for the
                # answer -- reconcile reads real positions, and the net-delta
                # hedge flattens whatever it finds.
                if (order.state != mk.UNKNOWN
                        and order.cancel_overdue(now, cfg.cancel_timeout_sec)):
                    order.to_unknown(f"cancel unconfirmed after "
                                     f"{cfg.cancel_timeout_sec:.1f}s")
                    self.maker_unknown += 1
                    log.critical(
                        "MAKER ORDER UNRESOLVED: %s — cancel not confirmed "
                        "inside its %.1fs budget. Treating it as POSSIBLY "
                        "FILLED: reconciling against the venue and holding "
                        "off new quotes until it resolves. / 撤单在预算内未"
                        "确认，按「可能已成交」处理，改以对帐结果为准",
                        order.describe(), cfg.cancel_timeout_sec)
                    self._reconcile_evt.set()
                    last_unknown_log = now
            if order.state == mk.UNKNOWN and now - last_unknown_log > 60.0:
                last_unknown_log = now
                log.critical("MAKER ORDER STILL UNRESOLVED (%d cancel "
                             "attempts): %s — still retrying, the engine does "
                             "not give up", order.cancel_attempts,
                             order.describe())
                self._reconcile_evt.set()
            if self.stop.is_set():
                if stop_deadline is None:
                    stop_deadline = now + max(cfg.cancel_timeout_sec * 2, 1.0)
                elif now > stop_deadline:
                    log.critical(
                        "SHUTTING DOWN WITH AN UNRESOLVED MAKER ORDER: %s — "
                        "check the venue by hand before restarting / 关机时仍"
                        "有未解决的挂单，请手动到交易所确认", order.describe())
                    break
            await asyncio.sleep(base_poll if order.state != mk.UNKNOWN
                                else max(base_poll, 2.0))

    async def _poll_maker(self, maker_v, order: MakerOrder):
        """One read of exchange truth, or None when we learned nothing.

        "Learned nothing" is a real answer and it is never upgraded into
        "the order is gone" -- that is the assumption this whole path exists
        to avoid.
        """
        if order.handle is None:
            return None
        try:
            info = await maker_v.poll_order(order.handle)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001
            log.warning("[%s] order poll failed: %r", maker_v.name, e)
            return None
        if not info or info.get("status") in (None, "", "unknown"):
            return None
        return info

    async def _send_maker_cancel(self, maker_v, order: MakerOrder) -> None:
        # Counts toward the venue's send budget but is never blocked by it:
        # an engine that cannot cancel is an engine that cannot stop.
        self._record_send(maker_v)
        _t0 = time.time()
        try:
            async with self._vlock(maker_v.key):
                res = await maker_v.cancel_order(order.handle)
            self.lat.add("cancel", (time.time() - _t0) * 1e3)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001
            log.warning("[%s] cancel raised: %r", maker_v.name, e)
            return
        st = res.get("status")
        if st == "rejected":
            log.warning("[%s] cancel rejected: %s", maker_v.name, res.get("err"))
        elif st == "gone":
            # Off the book -- but "gone" does not say whether it filled or
            # was cancelled, so it resolves nothing. The next poll does.
            log.info("[%s] cancel: order already off the book (%s) — polling "
                     "for its outcome", maker_v.name, res.get("err"))
        if str(res.get("err") or "").startswith("RATE_LIMITED"):
            self._mark_limited(maker_v)

    def _maker_cancel_reason(self, maker_v, taker_v, order: MakerOrder,
                             now: float, deadline: float):
        """Why this quote should come off the book, or None to leave it."""
        cfg = self.cfg
        forced = order.stats.get("force")
        if forced:
            return forced
        if self.stop.is_set():
            return "shutdown"
        if self.halted:
            return "engine halted — no new exposure"
        if self.flatten_request:
            return "flatten requested"
        if self.paused_by_operator:
            return f"paused by {self.pause_source or 'operator'}"
        if self.vol.paused(now):
            # A quote left resting through a fast move is the definition of
            # being picked off: our price is the one that stopped updating.
            return f"volatility breaker — {self.vol.reason}"
        if now >= deadline:
            return f"unfilled after {cfg.maker_timeout_sec:.1f}s"
        if maker_v.key in self._venue_down or taker_v.key in self._venue_down:
            return "venue outage"
        if not taker_v.book.is_fresh(cfg.staleness_sec):
            return "hedge book stale — a fill we could not hedge"
        if not maker_v.book.is_fresh(cfg.staleness_sec):
            return "own book stale — quoting blind"
        if self._venue_limited(taker_v):
            return "hedge venue rate limited"
        residual = order.residual
        if residual <= 0:
            return None
        edge = maker_edge_bps(order.px, order.is_buy, taker_v.book,
                              maker_fee_bps=maker_v.maker_fee_bps,
                              hedge_fee_bps=taker_v.fee_bps, qty=residual)
        if edge is None:
            return "hedge depth gone"
        if edge < cfg.maker_min_edge_bps:
            return (f"edge decayed to {edge:+.2f} bps "
                    f"(floor {cfg.maker_min_edge_bps:+.2f})")
        # Stolen from XEMM and pointed at a RESTING order, where it belongs:
        # an edge that suddenly looks wonderful usually means our own quote
        # is the stale one and somebody is about to take it.
        if cfg.max_edge_bps > 0 and edge > cfg.max_edge_bps:
            return (f"edge {edge:.1f} bps exceeds max_edge_bps "
                    f"{cfg.max_edge_bps:.1f} — the book is wrong, and a stale "
                    f"book means WE are the stale quote")
        return None

    async def _consume_maker_fill(self, maker_v, taker_v,
                                  order: MakerOrder) -> None:
        """Book any new fill, then hedge what is hedgeable. Must #2."""
        new = order.unapplied
        if new > 0:
            px = order.fill_px
            fee = maker_v.maker_fee_bps / 1e4
            if order.is_buy:
                maker_v.position += new
                maker_v.cash -= new * px * (1.0 + fee)
            else:
                maker_v.position -= new
                maker_v.cash += new * px * (1.0 - fee)
            maker_v.volume_usd += new * px
            maker_v.last_traded_ts = time.time()
            order.mark_applied(new)
            order.stats.setdefault("first_fill_ts", time.time())
            if order.stats.get("mid_at_fill") is None:
                order.stats["mid_at_fill"] = taker_v.book.mid()
            log.warning("[QUOTE FILL] %s %s %.6g of %.6g @%.6g (%s) — hedging",
                        maker_v.name, "BUY" if order.is_buy else "SELL",
                        order.filled_base, order.qty, px, order.status or "-")
        pend = order.unhedged
        if pend <= 0:
            return
        px = order.fill_px
        # The floor here is the HEDGE VENUE's minimum, not the strategy's
        # min_order_notional: this is not a decision to open a position, it
        # is the completion of one that already exists.
        too_small = (pend < max(taker_v.min_base, self._step)
                     or pend * px < taker_v.min_quote)
        if too_small:
            if not order.is_terminal:
                return          # let later fills accumulate past the minimum
            log.warning("[QUOTE] %.6g unhedged on %s is below %s's minimum "
                        "order size — the net-delta hedge owns it from here",
                        pend, maker_v.name, taker_v.name)
            self._reconcile_evt.set()
            return
        order.mark_hedged(pend)   # attempted exactly once, whatever happens
        await self._hedge_maker_fill(maker_v, taker_v, order, pend)

    async def _hedge_maker_fill(self, maker_v, taker_v, order: MakerOrder,
                                qty: float) -> None:
        """Take the other leg for a fill we just received. Never retries:
        a hedge that fails hands the imbalance to the net-delta path, which
        is the audited owner of "the legs do not match"."""
        cfg = self.cfg
        is_buy = not order.is_buy
        slip = cfg.leg_slippage_bps / 1e4
        ref = taker_v.book.best_ask() if is_buy else taker_v.book.best_bid()
        if ref is None:
            log.critical("[QUOTE HEDGE] no book on %s for %.6g — leaving it "
                         "to the net-delta hedge", taker_v.name, qty)
            self._reconcile_evt.set()
            return
        limit = (taker_v.px_round(ref * (1 + slip), True) if is_buy
                 else taker_v.px_round(ref * (1 - slip), False))
        if self._blocked("quote-hedge", taker_v, side="BUY" if is_buy else "SELL",
                         qty=qty, px=limit):
            return
        t0 = time.time()
        try:
            async with self._vlock(taker_v.key):
                self._record_send(taker_v)
                info = await taker_v.send_taker(is_buy=is_buy, qty=qty,
                                                limit_px=limit)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001
            info = {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": repr(e), "unresolved": True}
        order.stats["hedge_ms"] = (time.time() - t0) * 1e3
        self.lat.add("quote-hedge", order.stats["hedge_ms"])
        fill = float(info.get("filled_base") or 0.0)
        if fill:
            px = info.get("avg_px") or limit
            fee = taker_v.fee_bps / 1e4
            if is_buy:
                taker_v.position += fill
                taker_v.cash -= fill * px * (1.0 + fee)
            else:
                taker_v.position -= fill
                taker_v.cash += fill * px * (1.0 - fee)
            taker_v.volume_usd += fill * px
            matched = min(qty, fill)
            mpx, hpx = order.fill_px, (info.get("avg_px") or limit)
            mfee, hfee = maker_v.maker_fee_bps / 1e4, fee
            edge = (matched * (mpx * (1.0 - mfee) - hpx * (1.0 + hfee))
                    if not order.is_buy else
                    matched * (hpx * (1.0 - hfee) - mpx * (1.0 + mfee)))
            order.stats["fill_edge"] = order.stats.get("fill_edge", 0.0) + edge
            self.total_fill_edge += edge
        taker_v.last_traded_ts = time.time()
        log.info("[QUOTE HEDGE] %s %s %.6g/%.6g %s in %.0f ms",
                 taker_v.name, "BUY" if is_buy else "SELL", fill, qty,
                 info.get("status"), order.stats["hedge_ms"])
        if info.get("err") or info.get("unresolved"):
            order.stats["hedge_error"] = True
            log.error("[QUOTE HEDGE] %s: %s — the net-delta hedge takes over",
                      taker_v.name, info.get("err") or "unresolved")
            if str(info.get("err") or "").startswith("RATE_LIMITED"):
                self._mark_limited(taker_v)
            else:
                self.consec_errors += 1
                if self.consec_errors >= cfg.max_consecutive_errors:
                    self._risk_halt(f"{self.consec_errors} consecutive "
                                    f"execution problems")
            self._reconcile_evt.set()

    async def _finish_maker(self, maker_v, taker_v, plan: MakerPlan,
                            order: MakerOrder) -> None:
        """Retire one quote. The order leaves _maker_open ONLY when the
        exchange resolved it -- an `unknown` order keeps blocking new quotes,
        which is the intended consequence of not knowing."""
        filled = order.filled_base
        if order.is_terminal:
            if filled > 0:
                outcome = "filled"
            elif order.status == "canceled-post-only":
                outcome = "post-only-reject"   # never reached the book
            elif order.status == "rejected":
                outcome = "send-failed"
            else:
                outcome = "cancelled"
            self._maker_open.pop(maker_v.key, None)
        elif self.stop.is_set():
            outcome = "unresolved-at-shutdown"
            self._maker_open.pop(maker_v.key, None)
        else:
            outcome = "unresolved"
        rested = (any(st in mk.OPEN_STATUSES for st in order.seen)
                  or filled > 0)
        if rested:
            self.maker_rested += 1
        if (outcome in ("filled", "cancelled", "post-only-reject")
                and not order.stats.get("hedge_error")):
            # A quote that reached a clean end -- including one that simply
            # never filled -- is evidence the execution path works. A send
            # that merely left the building is not.
            self.consec_errors = 0
        if filled > 0:
            self.maker_fills += 1
            self.trades += 1
            self.total_exp_edge += plan.exp_edge_usd * (filled / plan.qty
                                                        if plan.qty else 0.0)
        elif outcome == "cancelled":
            self.maker_cancels += 1
        log.info("[QUOTE DONE] %s — %s", outcome, order.describe())
        self.recent_trades.append({
            "ts": time.time(), "direction": order.stats.get("dkey", "?"),
            "qty": filled or plan.qty, "notional": filled * order.fill_px,
            "prem_bps": plan.edge_bps, "exp": plan.exp_edge_usd,
            "fill": order.stats.get("fill_edge"),
            "status": f"maker/{outcome}", "ok": outcome != "unresolved"})
        self._log_maker_csv(maker_v, taker_v, plan, order, outcome)
        self.last_trade_ts = time.time()
        # A resolved quote is a good moment to check the legs against each
        # other -- and the only moment that matters after a partial fill.
        await self._maybe_hedge()

    def _log_maker_csv(self, maker_v, taker_v, plan: MakerPlan,
                       order: MakerOrder, outcome: str) -> None:
        st = order.stats
        now = time.time()
        first_fill = st.get("first_fill_ts")
        self._append_csv(self.cfg.maker_csv, MAKER_CSV_HEADER, [
            f"{now:.3f}", st.get("dkey", "?"), maker_v.name, taker_v.name,
            "BUY" if order.is_buy else "SELL",
            f"{order.px:.8g}", f"{order.qty:.8g}", f"{order.filled_base:.8g}",
            f"{order.hedged_base:.8g}", order.status or "-", outcome,
            f"{plan.edge_bps:.3f}", f"{plan.exp_edge_usd:.4f}",
            f"{st.get('fill_edge', 0.0):.4f}",
            f"{(now - order.sent_ts) * 1e3:.0f}",
            f"{(first_fill - order.sent_ts) * 1e3:.0f}" if first_fill else "",
            f"{st.get('hedge_ms', 0.0):.0f}" if st.get("hedge_ms") else "",
            f"{(now - order.cancel_ts) * 1e3:.0f}" if order.cancel_ts else "",
            order.cancel_attempts, st.get("cancel_reason", ""),
            f"{st['mid_at_fill']:.8g}" if st.get("mid_at_fill") else "",
        ])

    async def _cancel_stale_orders(self) -> None:
        """Startup sweep: nobody may trade on top of orders they do not own.

        A process that died with a quote on the book leaves an order that
        will fill with no one to hedge it -- the same naked exposure as a
        lost cancel, minus the log line. Failure here is fatal by design:
        the alternative is trading beside an order we cannot account for.
        """
        for v in self.venues.values():
            n = await v.cancel_open_orders()
            if n:
                log.critical("[%s] cancelled %d pre-existing resting order(s) "
                             "before start", v.name, n)

    # ---------------------------------------------------------- control (B5)
    #
    # Four verbs, applied from outside the process (entropy_arb/control.py).
    # The methods below are the whole surface the channel may touch: flags
    # and a flatten request. It cannot place an order, change a threshold, or
    # clear `halted` -- a halt needs a restart, because a restart is what
    # re-reads real positions with strict=True.

    def set_operator_pause(self, on: bool, source: str) -> None:
        """pause / resume. Stops NEW exposure only: hedging, flattening,
        self-rescue and reconcile are untouched, and no position is closed."""
        if self.paused_by_operator == on:
            return                                   # idempotent
        self.paused_by_operator = on
        self.pause_source = source if on else ""
        log.critical("OPERATOR %s (%s) — %s / 操作员%s",
                     "PAUSE" if on else "RESUME", source,
                     "no new exposure; hedging and reconcile continue"
                     if on else "opening new exposure again",
                     "暂停开新仓（对冲与对帐照常）" if on else "恢复开仓")
        self._update_evt.set()

    def request_flatten(self) -> None:
        """flat: close everything, keep running.

        Implies a pause, and the pause OUTLIVES the flatten. Re-opening the
        moment the position is closed would undo the operator's instruction
        within one evaluation, so the engine waits for an explicit resume.
        """
        self.flatten_request = True
        self.set_operator_pause(True, "flatten")
        self._flatten_evt.set()
        log.critical("OPERATOR FLAT requested — positions will be VERIFIED "
                     "against the venues first, then closed reduce-only; the "
                     "engine stays paused afterwards / 平仓请求：先对帐核实"
                     "真实持仓再平，平完仍保持暂停")

    def describe_positions(self) -> str:
        return (" ".join(f"{v.name}={v.position:+.6g}"
                         for v in self.venues.values())
                + f" net={sum(v.position for v in self.venues.values()):+.6g}")

    def control_status(self) -> str:
        """One screen of truth for the phone. Read-only."""
        cfg = self.cfg
        now = time.time()
        pnl = self.session_pnl()
        bits = [
            f"{cfg.symbol} entropy/{cfg.hedge_venue} mode={cfg.mode}",
            self.describe_positions(),
            f"MTM {'$%+.4f' % pnl if pnl is not None else '—'} | "
            f"trades {self.trades} hedges {self.hedges}",
            "lat p50/p95/p99 " + (self.lat.line() or "—"),
        ]
        state = []
        if self.halted:
            state.append("HALTED (restart required)")
        if self.paused_by_operator:
            state.append(f"PAUSED by {self.pause_source}")
        if self.flatten_request:
            state.append("FLATTENING")
        if self.vol.paused(now):
            state.append(f"VOL-PAUSE {self.vol.remaining(now):.0f}s "
                         f"({self.vol.reason})")
        if self._venue_down:
            state.append("DOWN: " + ",".join(self._venue_down))
        for o in self._maker_open.values():
            state.append("RESTING " + o.describe())
        if self._recorder_dead:
            state.append("RECORDER DEAD")
        bits.append("state: " + ("; ".join(state) if state else "trading"))
        for v in self.venues.values():
            bits.append(f"{v.name} book {v.book.best_bid() or '—'}/"
                        f"{v.book.best_ask() or '—'}"
                        + ("" if v.book.is_fresh(cfg.staleness_sec)
                           else " STALE"))
        return "\n".join(bits)

    async def _flatten_step(self) -> bool:
        """One pass at closing every position. Returns True when flat.

        Sibling of _hedge(): same guards, different target. _hedge drives the
        NET to zero (the legs should cancel); this drives EACH LEG to zero
        (the operator wants out). Both are reduce-only and price-protected,
        and both refuse to act on a venue that is unreachable or blind.
        """
        cfg = self.cfg
        slip = cfg.hedge_slippage_bps / 1e4
        flat = True
        for v in self.venues.values():
            if abs(v.position) <= cfg.net_tolerance_base:
                continue
            flat = False
            if v.key in self._venue_down:
                log.warning("[FLAT] %s unreachable — retrying", v.name)
                continue
            if not v.book.is_fresh(cfg.staleness_sec):
                log.warning("[FLAT] %s book stale — will not close blind",
                            v.name)
                continue
            lk = self._vlock(v.key)
            if lk.locked():
                continue
            is_sell = v.position > 0
            qty = floor_step(abs(v.position), self._step)
            if qty < v.min_base:
                continue
            ref = v.book.best_bid() if is_sell else v.book.best_ask()
            if ref is None:
                continue
            limit = (v.px_round(ref * (1 - slip), False) if is_sell
                     else v.px_round(ref * (1 + slip), True))
            if qty * limit < v.min_quote:
                log.warning("[FLAT] %s residual %.6g is below the venue "
                            "minimum — cannot be closed by order", v.name, qty)
                continue
            if self._blocked("flat", v, side="SELL" if is_sell else "BUY",
                             qty=qty, px=limit):
                continue
            await lk.acquire()
            try:
                log.critical("[FLAT] %s %.6g on %s @%.6g",
                             "SELL" if is_sell else "BUY", qty, v.name, limit)
                self._record_send(v)   # counts toward the budget, never blocked
                info = await v.send_taker(is_buy=not is_sell, qty=qty,
                                          limit_px=limit, reduce_only=True)
                if info.get("err") or info.get("unresolved"):
                    log.error("[FLAT] %s: %s", v.name,
                              info.get("err") or "unresolved")
                    if str(info.get("err", "")).startswith("RATE_LIMITED"):
                        self._mark_limited(v)
                    self._reconcile_evt.set()
                else:
                    fill = info["filled_base"]
                    v.position += -fill if is_sell else fill
                    if fill:
                        px = info.get("avg_px") or limit
                        fee = v.fee_bps / 1e4
                        v.cash += (fill * px * (1 - fee) if is_sell
                                   else -fill * px * (1 + fee))
                        v.volume_usd += fill * px
                    log.info("[FLAT SETTLED] %s %s %.6g/%.6g", v.name,
                             info["status"], fill, qty)
                v.last_traded_ts = time.time()
            finally:
                lk.release()
        return flat

    async def _flatten_loop(self) -> None:
        """Owns the `flat` verb. Never gives up on its own -- only the
        operator or a shutdown ends it."""
        first = True
        while not self.stop.is_set():
            await self._flatten_evt.wait()
            if self.stop.is_set():
                break
            if first:
                # The scar rule (mistake.md 2026-06-07): anything that
                # changes real exchange state checks the real exchange
                # first. Flattening a position we only believe we have is
                # how you create an orphan.
                try:
                    await self._reconcile_positions(hedge=False)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("[FLAT] pre-flatten reconcile failed — "
                                  "closing on the last known positions")
                first = False
            try:
                done = await self._flatten_step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[FLAT] step failed")
                done = False
            if done:
                self._flatten_evt.clear()
                self.flatten_request = False
                first = True
                log.critical("FLAT COMPLETE: %s — the engine stays PAUSED; "
                             "send resume to trade again / 平仓完成，引擎维持"
                             "暂停，需 resume 才会恢复开仓",
                             self.describe_positions())
                await self.notify("flat complete: " + self.describe_positions()
                                  + "\nthe engine stays paused — /resume to "
                                    "trade again")
                continue
            await asyncio.sleep(1.0)

    async def notify(self, text: str) -> None:
        if self.control is not None:
            await self.control.notify(text)

    # Risk switches that must be explicitly chosen before real money moves.
    # None has a defensible universal default -- the dollar ones depend on
    # account size, and a wrong ceiling is worse than a missing one -- so
    # live trading refuses to start until the operator writes each number.
    REQUIRED_RISK: tuple = (
        ("max_net_base", "largest |legA+legB| before HALT"),
        ("max_gross_usd", "total |position x mid| ceiling across venues"),
        ("max_daily_loss_usd", "session mark-to-market floor"),
        ("max_consecutive_stale", "stale-book evaluations before HALT"),
        ("max_edge_bps", "premium above which the BOOK is assumed wrong"),
        ("vol_max_move_bps", "peak-to-trough move that pauses new exposure"),
    )

    def _require_armed_risk_block(self) -> None:
        """Refuse to trade with unarmed risk switches.

        A switch that exists and a switch that is armed are different facts,
        and nothing in a running system shows the difference. Checked here,
        beside the credentials check, because this is the last moment before
        real orders become possible.
        """
        missing = [(k, d) for k, d in self.REQUIRED_RISK
                   if not getattr(self.cfg, k, 0)]
        if not missing:
            return
        lines = "\n".join(f"  {k}: <value>   # {d}" for k, d in missing)
        raise RuntimeError(
            "live trading refused: these risk switches are unset (0 = "
            "disabled). Add them under `risk:` in the config file, then "
            "restart. Use --record-only to collect data without them.\n"
            "实盘拒绝启动：以下风控开关未设定（0 = 关闭），请在设定档的 "
            "`risk:` 区块写入数值后重启；仅采集资料请用 --record-only。\n"
            "risk:\n" + lines)

    def _recorder_died(self, task) -> None:
        """G5: the recorder task finished. Cancellation at shutdown is
        normal; anything else means the sidecar is gone and the operator
        must know. Trading continues by design (principle 7)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            if not self.stop.is_set():
                self._recorder_dead = "exited without error"
                log.critical("RECORDER STOPPED (no error) — trading continues "
                             "but minute data is NO LONGER being written")
            return
        self._recorder_dead = repr(exc)
        log.critical("RECORDER DIED: %r — trading continues but minute data "
                     "is NO LONGER being written", exc)

    # ----------------------------------------------------------- shadow (B?)

    def _ready(self, v) -> bool:
        """Shadow needs books, not signers."""
        return self.shadow or v.ready_to_trade()

    def _blocked(self, action: str, v, *, other=None, side: str = "",
                 qty: float = 0.0, px: float = 0.0, edge_bps=None,
                 exp_edge_usd=None, note: str = "") -> bool:
        """The send boundary. True when nothing may leave this process.

        Every path that can put an order on an exchange asks this first, so
        "shadow sends nothing" is one fact in one place rather than six
        promises spread over the file.
        """
        if not self.shadow:
            return False
        self.shadow_decisions += 1
        # Stamp the venues exactly as a real send would. Without this the
        # anti-refire guard in _scan ("never act on a book older than this
        # venue's own last trade") never engages, and shadow re-decides the
        # same plan on every loop turn -- 100 identical rows in 10 ms, which
        # is both useless as a record and a false picture of the live
        # cadence. A rehearsal that fires faster than the real thing is not
        # a rehearsal. Found by running it, 2026-09-05.
        now = time.time()
        self.last_trade_ts = now
        for venue in (v, other):
            if venue is not None:
                venue.last_traded_ts = now
        log.warning("[SHADOW] %s %s %s %.6g @%.6g%s — NOT SENT",
                    action, v.name if v is not None else "-", side, qty, px,
                    f" | edge {edge_bps:.2f}bps" if edge_bps is not None else "")
        self._append_csv(self.cfg.shadow_csv, SHADOW_CSV_HEADER, [
            f"{time.time():.3f}", action, note, v.name if v is not None else "",
            other.name if other is not None else "", side,
            f"{qty:.8g}", f"{px:.8g}", f"{qty * px:.2f}",
            f"{edge_bps:.3f}" if edge_bps is not None else "",
            f"{exp_edge_usd:.4f}" if exp_edge_usd is not None else "", note])
        return True

    def _risk_halt(self, reason: str) -> bool:
        """One place where every hard stop is raised. Constant cost, no I/O.
        Returns True if the engine is (now) halted."""
        if self.halted:
            return True
        self.halted = True
        n = self.cfg.halt_flatten_attempts
        log.critical("HALTED: %s — no NEW arbitrage. %s Check both venues; "
                     "restart re-reads real positions (strict).", reason,
                     (f"Will still attempt up to {n} reduce-only hedge(s) to "
                      f"flatten any imbalance." if n
                      else "Positions frozen as-is."))
        self._reconcile_evt.set()
        return True

    def _gross_usd(self) -> float:
        """Total |position x mid| across venues. O(venues), no I/O."""
        g = 0.0
        for v in self.venues.values():
            m = v.book.mid()
            if m:
                g += abs(v.position) * m
        return g

    # After the budget is spent, keep retrying this many reconcile cycles
    # apart. At the default reconcile_sec=15 that is once every ~5 minutes --
    # slow enough not to burn the order budget, persistent enough that a
    # venue which comes back an hour later still gets flattened.
    HALT_SLOW_RETRY_TICKS = 20

    async def _self_rescue(self, net: float) -> None:
        """Bring the position back to flat AFTER a halt, without a human.

        A halt stops new arbitrage; it must not stop the engine from
        unwinding exposure it already has. The budget below counts attempts
        that made NO PROGRESS -- any reduction in |net| resets it -- so a
        transient outage cannot exhaust it, and a genuinely stuck position
        degrades to a slow retry instead of silence.
        """
        cfg = self.cfg
        if abs(net) <= cfg.net_tolerance_base:
            if self._halt_last_net is not None:
                log.critical("SELF-RESCUE COMPLETE: net %+.6g within "
                             "tolerance after %d attempt(s) — exposure "
                             "unwound without intervention", net,
                             self._halt_flattens)
                self._halt_last_net = None
                self._halt_flattens = 0
                self._halt_stuck_logged = False
            return
        if not cfg.halt_flatten_attempts:
            return                                    # freeze mode, opted in
        # progress since the last attempt resets the budget
        prev = self._halt_last_net
        if prev is not None and abs(net) < abs(prev) - 1e-12:
            self._halt_flattens = 0
            self._halt_stuck_logged = False
        self._halt_last_net = net
        if self._halt_flattens < cfg.halt_flatten_attempts:
            self._halt_flattens += 1
            log.warning("[SELF-RESCUE %d/%d] reducing naked %+.6g",
                        self._halt_flattens, cfg.halt_flatten_attempts, net)
            await self._hedge(net)
            return
        # budget spent with no progress: slow down, never stop
        if not self._halt_stuck_logged:
            log.critical("SELF-RESCUE STUCK: %d attempts made no progress on "
                         "net %+.6g (venue down, book stale, or below the "
                         "minimum size). Still retrying every %d reconcile "
                         "cycles — the engine does not give up.",
                         self._halt_flattens, net, self.HALT_SLOW_RETRY_TICKS)
            self._halt_stuck_logged = True
        self._halt_ticks += 1
        if self._halt_ticks >= self.HALT_SLOW_RETRY_TICKS:
            self._halt_ticks = 0
            log.warning("[SELF-RESCUE retry] net %+.6g", net)
            await self._hedge(net)

    async def _maybe_hedge(self) -> None:
        net = sum(v.position for v in self.venues.values())
        # After a halt: do NOT open anything new (blocked in _evaluate /
        # _execute), but DO try to flatten an imbalance that already exists.
        # Halting because the legs drifted and then refusing to bring them
        # back together leaves naked exposure through exactly the move that
        # caused the halt. Bounded so a failing hedge cannot loop.
        if self.halted:
            await self._self_rescue(net)
            return
        # B4/G1 (2026-09-04): a hard ceiling on how far the legs may drift.
        # Checked BEFORE hedging, because the failure mode is "hedge keeps
        # failing while the imbalance keeps growing" -- exactly the shape
        # that has to stop by itself rather than be noticed by a human.
        # HALT is one-way: it needs a restart, and a restart re-reads the
        # real positions with strict=True.
        cap = self.cfg.max_net_base
        if cap > 0 and abs(net) > cap:
            self._risk_halt(f"net imbalance {net:+.6g} exceeds max_net_base "
                            f"{cap:.6g} — the legs are no longer hedging "
                            f"each other")
            return
        # B4 daily loss floor: session mark-to-market against the baseline
        # taken at first evaluation. O(1) over two venues, no I/O.
        gross_cap = self.cfg.max_gross_usd
        if gross_cap > 0:
            g = self._gross_usd()
            if g > gross_cap:
                self._risk_halt(f"gross exposure ${g:,.2f} exceeds "
                                f"max_gross_usd ${gross_cap:,.2f}")
                return
        floor = self.cfg.max_daily_loss_usd
        if floor > 0:
            pnl = self.session_pnl()
            if pnl is not None and pnl < -floor:
                self._risk_halt(f"session PnL ${pnl:+.4f} below floor "
                                f"-${floor:.2f}")
                return
        if abs(net) > self.cfg.net_tolerance_base:
            await self._hedge(net)

    async def _hedge(self, net: float) -> None:
        """Reduce the venue that carries the imbalance back toward net zero
        (reduce-only taker with hedge_slippage_bps price protection)."""
        cfg = self.cfg
        is_sell = net > 0
        sgn = 1.0 if net > 0 else -1.0
        slip = cfg.hedge_slippage_bps / 1e4
        for v in sorted(self.venues.values(),
                        key=lambda x: (self._venue_limited(x), -x.position * sgn)):
            if v.position * sgn <= 0:
                continue
            if v.key in self._venue_down \
                    or not v.book.is_fresh(cfg.staleness_sec):
                continue  # unreachable or blind: cannot hedge here
            lk = self._vlock(v.key)
            if lk.locked():
                continue
            qty = floor_step(min(abs(net), abs(v.position)), self._step)
            if qty < v.min_base:
                continue
            ref = v.book.best_bid() if is_sell else v.book.best_ask()
            if ref is None:
                continue
            limit = v.px_round(ref * (1 - slip), False) if is_sell \
                else v.px_round(ref * (1 + slip), True)
            if qty * limit < max(cfg.min_order_notional, v.min_quote):
                continue
            if self._blocked("hedge", v, side="SELL" if is_sell else "BUY",
                             qty=qty, px=limit, note=f"net {net:+.6g}"):
                return
            await lk.acquire()  # verified free, no awaits since: fast path
            try:
                log.warning("[HEDGE] net %+.6g — %s %.6g on %s @%.6g",
                            net, "SELL" if is_sell else "BUY", qty, v.name, limit)
                self.hedges += 1
                self._record_send(v)  # counts toward the budget, never blocked
                _t0 = time.time()
                info = await v.send_taker(is_buy=not is_sell, qty=qty,
                                          limit_px=limit, reduce_only=True)
                self.lat.add("hedge", (time.time() - _t0) * 1e3)
                if info.get("err") or info.get("unresolved"):
                    log.error("[HEDGE] %s: %s", v.name,
                              info.get("err") or "unresolved")
                    if str(info.get("err", "")).startswith("RATE_LIMITED"):
                        self._mark_limited(v)
                    self._reconcile_evt.set()
                else:
                    fill = info["filled_base"]
                    v.position += -fill if is_sell else fill
                    if fill:
                        px = info.get("avg_px") or limit
                        fee = v.fee_bps / 1e4
                        v.cash += fill * px * (1 - fee) if is_sell \
                            else -fill * px * (1 + fee)
                        v.volume_usd += fill * px
                    log.info("[HEDGE SETTLED] %s %s %.6g/%.6g",
                             v.name, info["status"], fill, qty)
                v.last_traded_ts = time.time()
            finally:
                lk.release()
            return
        log.warning("[HEDGE] net %+.6g below hedgeable minimum — carrying "
                    "(next reconcile retries)", net)

    # --------------------------------------------------- reconcile / status

    # Lighter's REST account state lags its ws settlements; overwriting a
    # venue that traded seconds ago "restores" stale positions and triggers
    # phantom hedge oscillations. Grace-guard + venue lock prevent that.
    RECONCILE_GRACE_SEC = 5.0

    async def _reconcile_positions(self, hedge: bool,
                                   strict: bool = False) -> None:
        now = time.time()
        vs = []
        for v in self.venues.values():
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                continue  # just traded: chain read would be stale
            if v.key in self._venue_down \
                    and now < self._venue_probe_at.get(v.key, 0.0):
                continue  # down venue: probe only every venue_probe_sec
            vs.append(v)
        if not vs:
            return
        got = await asyncio.gather(
            *(self._reconcile_venue(v, strict) for v in vs),
            return_exceptions=True)
        for r in got:
            if isinstance(r, BaseException):
                raise r  # strict startup: fail loudly
        if hedge:
            await self._maybe_hedge()

    async def _reconcile_venue(self, v, strict: bool) -> None:
        async with self._vlock(v.key):
            now = time.time()
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                return  # traded while waiting for the lock
            try:
                r = await v.fetch_position()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if strict:
                    raise RuntimeError(
                        f"[{v.name}] cannot fetch starting position: {e!r}")
                # exchange unreachable (e.g. scheduled maintenance): pause
                # trading and keep probing until it answers again
                # Name the cause. Lighter sits behind CloudFront + AWS WAF:
                # a burst answers 429, and then 405 with the header
                # `x-amzn-waf-action: captcha` -- which is a CHALLENGE, not a
                # broken route. Nothing here tries to answer it; the only
                # correct response is to slow down, which the probe cadence
                # below already does. Measured 2026-09-05.
                why = str(e)
                if "429" in why or "Too Many Requests" in why:
                    why = "rate limited (429)"
                elif "405" in why and "waf" in why.lower():
                    why = "WAF captcha challenge — back off, do not retry hard"
                elif "405" in why:
                    why = "405 (CloudFront/WAF, usually a burst limit)"
                n = self._venue_fetch_fails.get(v.key, 0) + 1
                self._venue_fetch_fails[v.key] = n
                self._venue_probe_at[v.key] = now + self.cfg.venue_probe_sec
                if n >= 3 and v.key not in self._venue_down:
                    self._venue_down[v.key] = now
                    log.critical("[%s] API unreachable (%d attempts) — "
                                 "trading PAUSED; probing every %.0fs until "
                                 "it recovers", v.name, n,
                                 self.cfg.venue_probe_sec)
                elif v.key not in self._venue_down:
                    log.warning("[%s] position fetch failed (%d): %s",
                                v.name, n, why)
                return
            if v.key in self._venue_down:
                log.warning("[%s] API recovered after %.0fs outage — "
                            "trading RESUMED", v.name,
                            now - self._venue_down.pop(v.key))
                self._update_evt.set()
            self._venue_fetch_fails[v.key] = 0
            delta = r - v.position
            # --------------------------------------------------- liquidation
            # A liquidation leaves no message. Neither does an ADL, a manual
            # trade on the same account, or a second bot. What all four DO
            # leave is a position that moved while this engine sent nothing --
            # and the engine knows exactly when it last sent something, so
            # "we did not cause this" is a fact here, not an inference.
            #
            # Reconcile still adopts the chain (truth is truth); the halt is
            # about what happens NEXT. Carrying on would mean sizing, hedging
            # and risk-capping against a position somebody else is also
            # moving. HALT still self-rescues, so the exposure gets flattened.
            if (self.cfg.unexplained_position_halt
                    and not strict
                    and abs(delta) > self.cfg.net_tolerance_base
                    and not self._we_touched(v, now)):
                self.unexplained_events += 1
                self._risk_halt(
                    f"[{v.name}] position moved {delta:+.6g} with no order "
                    f"from us in {now - v.last_traded_ts:.0f}s — liquidation, "
                    f"ADL, or someone else trading this account. Whatever it "
                    f"was, the position is no longer only ours to manage / "
                    f"持仓在我们没下单的情况下变动，可能是强平/自动减仓/"
                    f"帐户被别人动过")
            if abs(delta) > 1e-12:
                if abs(delta) > self.cfg.net_tolerance_base:
                    log.warning("[%s] reconcile: chain %+.6g vs local %+.6g "
                                "— adopting chain", v.name, r, v.position)
                mid = v.book.mid()
                if mid is not None:
                    v.cash -= delta * mid
                v.position = r
                # B3: a chain read is COMPLETE as of the moment it was taken,
                # so after adopting it no per-order delta may be added on top
                # -- that would book the same fill twice. Any fill the chain
                # already contains but we had not hedged becomes the
                # net-delta hedge's problem, and _maybe_hedge runs right
                # after this in _reconcile_positions.
                order = self._maker_open.get(v.key)
                if order is not None and (order.unapplied or order.unhedged):
                    log.warning("[%s] reconcile supersedes maker accounting "
                                "(%.6g unbooked, %.6g unhedged): %s",
                                v.name, order.unapplied, order.unhedged,
                                order.describe())
                    order.mark_applied(order.unapplied)
                    order.mark_hedged(order.unhedged)

    # How long after our own order a position change is still attributable to
    # it. The order may settle late (an unresolved send returns filled_base 0
    # and the chain learns the truth first), so this must cover a full settle
    # plus the reconcile that chases it.
    def _attribution_window(self) -> float:
        return self.cfg.settle_timeout_sec + self.cfg.reconcile_sec + 5.0

    def _we_touched(self, v, now: float) -> bool:
        """True when a position change on this venue could plausibly be ours."""
        if v.key in self._maker_open:
            return True          # a resting quote can fill at any moment
        # No lock check here: the only caller already HOLDS this venue's
        # lock, so "is an order in flight" would always answer yes. It is
        # also unnecessary -- an in-flight order holds the lock, which is
        # exactly what keeps reconcile out of this venue in the first place.
        return now - v.last_traded_ts <= self._attribution_window()

    async def _reconcile_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self._reconcile_evt.wait(),
                                       timeout=self.cfg.reconcile_sec)
                self._reconcile_evt.clear()
                await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                pass
            if self.stop.is_set():
                break
            try:
                await self._reconcile_positions(hedge=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile failed")

    async def _balance_loop(self) -> None:
        while not self.stop.is_set():
            for v in self.venues.values():
                try:
                    got = await v.fetch_equity()
                    if got is not None:
                        v.equity, v.free = got
                        if v.start_equity is None:
                            v.start_equity = v.equity
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("[%s] equity poll failed: %r", v.name, e)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=BALANCE_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _http_keepalive_loop(self) -> None:
        if self.cfg.http_keepalive_sec <= 0:
            return
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       timeout=self.cfg.http_keepalive_sec)
                return
            except asyncio.TimeoutError:
                pass
            await asyncio.gather(*(v.warm_http() for v in self.venues.values()),
                                 return_exceptions=True)

    def account_delta(self) -> Optional[float]:
        """Change in real account equity since start (both venues)."""
        total = 0.0
        for v in self.venues.values():
            if v.equity is None or v.start_equity is None:
                return None
            total += v.equity - v.start_equity
        return total

    def session_pnl(self) -> Optional[float]:
        total = 0.0
        for v in self.venues.values():
            m = v.book.mid()
            if m is None:
                return None
            total += v.cash + v.position * m
        if self._mtm_baseline is None:
            self._mtm_baseline = total
        return total - self._mtm_baseline

    def premium_bps(self) -> Optional[float]:
        em, hm = self.entropy.book.mid(), self.hedge.book.mid()
        if not (em and hm):
            return None
        return (em / hm - 1.0) * 1e4

    async def _status_loop(self) -> None:
        cfg = self.cfg
        while not self.stop.is_set():
            try:
                await asyncio.sleep(cfg.status_interval_sec)
            except asyncio.CancelledError:
                raise
            books = " | ".join(
                f"{v.name} {v.book.best_bid() or '—'}/{v.book.best_ask() or '—'}"
                + ("" if v.book.is_fresh(cfg.staleness_sec) else " STALE")
                + (" RATE-LTD" if self._venue_limited(v) else "")
                + (" DOWN" if v.key in self._venue_down else "")
                for v in self.venues.values())
            prem = self.premium_bps()
            prem_s = f"{prem:+.2f}" if prem is not None else "—"
            pos = " ".join(f"{v.name} {v.position:+.6g}"
                           for v in self.venues.values())
            net = sum(v.position for v in self.venues.values())
            pnl = self.session_pnl()
            rec = (f" | rec {self.recorder.rows_written} rows"
                   if self.recorder else "")
            if cfg.mode == "maker":
                fill_rate = (100.0 * self.maker_fills / self.maker_rested
                             if self.maker_rested else 0.0)
                rec += (f" | quotes {self.maker_rested}/{self.maker_posts}"
                        f" fills {self.maker_fills}"
                        f" ({fill_rate:.0f}%) cx {self.maker_cancels}"
                        f" po-rej {self.maker_rejects}")
                for o in self._maker_open.values():
                    rec += f" | RESTING {o.describe()}"
                if self.maker_unknown:
                    rec += f" | *** UNRESOLVED CANCELS x{self.maker_unknown} ***"
            if self.paused_by_operator:
                rec += f" | PAUSED({self.pause_source})"
            if self.flatten_request:
                rec += " | *** FLATTENING ***"
            if self.vol.paused(time.time()):
                rec += (f" | *** VOL-PAUSE {self.vol.remaining(time.time()):.0f}s"
                        f" ({self.vol.reason}) ***")
            elif self.vol.trips:
                rec += f" | vol trips {self.vol.trips}"
            lat = self.lat.line()
            if lat:
                rec += f" | lat p50/p95/p99 {lat}"
            if self.unexplained_events:
                rec += (f" | *** UNEXPLAINED POSITION MOVES "
                        f"x{self.unexplained_events} ***")
            if self._absurd_skips:
                rec += f" | refused x{self._absurd_skips}"
            if self._stale_streak:
                rec += f" | stale x{self._stale_streak}"
            if self.halted and self._halt_last_net is not None:
                rec += (f" | RESCUING net {self._halt_last_net:+.6g} "
                        f"({self._halt_flattens}/"
                        f"{self.cfg.halt_flatten_attempts}"
                        + (" STUCK" if self._halt_stuck_logged else "") + ")")
            if self._recorder_dead:
                rec += f" *** RECORDER DEAD: {self._recorder_dead[:60]} ***"
            log.info("[status] %s | prem %s bps (band %+.2f..%+.2f) | pos %s "
                     "net %+.6g | trades %d hedges %d | MTM %s expEdge $%.4f "
                     "fillEdge $%.4f%s%s",
                     books, prem_s, cfg.midline_bps - cfg.lower_bps,
                     cfg.midline_bps + cfg.upper_bps, pos, net, self.trades,
                     self.hedges,
                     f"${pnl:+.4f}" if pnl is not None else "—",
                     self.total_exp_edge, self.total_fill_edge, rec,
                     " *** HALTED ***" if self.halted else "")

    def _append_csv(self, path: str, header: list, row: list) -> None:
        """Append one row, rotating the file if its header no longer matches.
        Never raises: a log write must not be able to stop trading."""
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh0:
                    if fh0.readline().strip() != ",".join(header):
                        os.replace(path, path + ".old")
            fresh = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if fresh:
                    w.writerow(header)
                w.writerow(row)
        except Exception:
            log.exception("csv write failed")

    def _log_csv(self, direction, buy, sell, plan: ArbPlan, ok: bool, bfill,
                 sfill, bstatus, sstatus, fill_edge, inv_bps) -> None:
        self._append_csv(self.cfg.trades_csv, CSV_HEADER, [
            f"{time.time():.3f}",
            direction, buy.name, sell.name, f"{plan.qty:.8g}",
            plan.buy_limit, plan.sell_limit,
            f"{plan.buy_notional:.2f}", f"{plan.sell_notional:.2f}",
            f"{plan.exp_edge_usd:.4f}", f"{plan.gross_edge_usd:.4f}",
            f"{plan.marginal_premium_bps:.3f}",
            f"{self.cfg.midline_bps:.3f}",
            f"{inv_bps:.3f}", int(ok), f"{bfill:.8g}",
            f"{sfill:.8g}", bstatus, sstatus, f"{fill_edge:.4f}"])
