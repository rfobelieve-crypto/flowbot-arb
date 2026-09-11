# -*- coding: utf-8 -*-
"""Funding-rate poller for the two legs (local patch 2026-09-01).

Why: the recorder answers "is there a price spread and does it converge".
It cannot answer the OTHER way this pair can pay — the funding differential,
which is what MOB's own Delta-Neutral card calls "the return" (long one DEX,
short the other, Δ≈0, collect the spread). A single-point snapshot on
2026-09-01 showed BTC +4.8%/yr and HYPE +10.95%/yr between HL and Lighter RH
while five other pairs sat at +0.44% — but a snapshot cannot tell a stable
carry from a number that flips daily. That needs a time series, so the
recorder starts writing one.

UNITS — the one thing that must not be wrong here:
  * Hyperliquid publishes funding PER HOUR (`funding` in metaAndAssetCtxs;
    142/233 coins sit at its 1.25e-5/hr baseline).
  * Lighter's /api/v1/funding-rates publishes PER 8 HOURS. Anchor: the same
    endpoint's `exchange: "binance"` rows cluster on 1e-4, which is
    Binance's canonical 0.01%/8h baseline.
Everything below is normalised to **bps per 8h** so the two are comparable,
and `diff_bps_8h` is (hedge − entropy): positive means the HEDGE leg pays
more, i.e. short-hedge / long-entropy collects.

Failure policy: this is decoration on a recorder whose job is not to miss
minutes. Every fetch is wrapped; on any error the last good value is kept
and `age_s` grows, so a stale funding number is visible as stale rather
than silently frozen (the 2026-09-01 flow_system lesson).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

HL_INFO = "https://api.hyperliquid.xyz/info"
LIGHTER_BASE = {
    "lighter": "https://mainnet.zklighter.elliot.ai",
    "lighter-rh": "https://api.rh.lighter.xyz",
}
POLL_SEC = 300.0          # funding moves hourly at best; 5 min is plenty
TIMEOUT = 15.0


def fetch_hl_all(dex: str = "") -> dict:
    """HL 的 {ticker: bps/8h}。**一次請求拿整個宇宙** —— 端點本來就是全量的,
    所以 150 個配對跟 1 個配對的成本一樣。

    2026-09-11 抽出來的理由:原本這段解析住在 `FundingPoller._fetch_hl` 裡面、
    抓全量之後只挑一個幣。宇宙級錄製器需要全部,而**再寫一份解析就是第二份
    實作**(mistake.md 2026-08-26)。所以改成這裡是唯一解析處,
    `FundingPoller._fetch_hl` 改成呼叫它再挑一個。
    """
    body = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    r = requests.post(HL_INFO, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    meta, ctxs = r.json()[0], r.json()[1]
    out = {}
    for a_, c in zip(meta.get("universe", []), ctxs):
        name = a_.get("name", "").split(":")[-1]
        if not name:
            continue
        # HL 的資金費是**每小時**的 -> x8 換成 8h 慣例
        out[name] = float(c.get("funding") or 0.0) * 8.0 * 1e4
    return out


def fetch_lighter_all(venue: str) -> dict:
    """某個 Lighter 部署的 {symbol: bps/8h}。同樣是一次請求拿全部。

    **只取該場館自己的費率** —— 這個端點還會鏡射 binance/bybit/hyperliquid
    的參考費率,混進來就不是這個場館的了(原本那段篩選的理由,照抄)。
    """
    base = LIGHTER_BASE.get(venue)
    if not base:
        return {}
    r = requests.get(base + "/api/v1/funding-rates", timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for row in r.json().get("funding_rates", []):
        if row.get("exchange") != "lighter":
            continue
        sym = row.get("symbol")
        if sym:
            out[sym] = float(row.get("rate") or 0.0) * 1e4      # 本來就是 /8h
    return out


class FundingPoller:
    """Background poller exposing the latest funding of both legs.

    entropy leg = Hyperliquid (core dex "" or a HIP-3 dex such as "io")
    hedge leg   = a Lighter deployment
    """

    def __init__(self, hl_coin: str, hl_dex: str,
                 lighter_venue: str, lighter_symbol: str,
                 entropy_lighter_venue: Optional[str] = None) -> None:
        self.hl_coin = hl_coin.split(":")[-1]     # "io:SNDK" -> "SNDK"
        self.hl_dex = hl_dex or ""
        self.lighter_venue = lighter_venue
        self.lighter_symbol = lighter_symbol
        # Local patch 2026-09-04: when leg A is a Lighter chain too, its
        # funding comes from that chain's endpoint, not from HL.
        self.entropy_lighter_venue = entropy_lighter_venue
        self._lock = threading.Lock()
        self._e: Optional[float] = None           # bps / 8h
        self._h: Optional[float] = None           # bps / 8h
        self._ts: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── fetchers ─────────────────────────────────────────────────────────

    def _fetch_hl(self) -> Optional[float]:
        # 解析住在 fetch_hl_all()（唯一一處）；這裡只挑自己那一個。
        return fetch_hl_all(self.hl_dex).get(self.hl_coin)

    def _fetch_lighter(self, venue: Optional[str] = None,
                       symbol: Optional[str] = None) -> Optional[float]:
        venue = venue or self.lighter_venue
        symbol = symbol or self.lighter_symbol
        # 同上：解析住在 fetch_lighter_all()，這裡只挑自己那一個。
        return fetch_lighter_all(venue).get(symbol)

    # ── loop ─────────────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        e = h = None
        try:
            e = (self._fetch_lighter(self.entropy_lighter_venue, self.hl_coin)
                 if self.entropy_lighter_venue else self._fetch_hl())
        except Exception as exc:
            log.debug("funding: leg-A fetch failed: %r", exc)
        try:
            h = self._fetch_lighter()
        except Exception as exc:
            log.debug("funding: lighter fetch failed: %r", exc)
        if e is None and h is None:
            return                       # keep the previous values, age grows
        with self._lock:
            if e is not None:
                self._e = e
            if h is not None:
                self._h = h
            self._ts = time.time()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(POLL_SEC)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._poll_once()                # have a value before minute one
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="funding-poller")
        self._thread.start()
        with self._lock:
            log.info("[funding] %s(dex=%s) vs %s %s -> "
                     "entropy %.4f / hedge %.4f bps per 8h",
                     self.hl_coin, self.hl_dex or "core", self.lighter_venue,
                     self.lighter_symbol,
                     self._e if self._e is not None else float("nan"),
                     self._h if self._h is not None else float("nan"))

    def stop(self) -> None:
        self._stop.set()

    # ── read side (called once per recorded minute) ──────────────────────

    def snapshot(self) -> tuple:
        """(entropy_bps_8h, hedge_bps_8h, diff_bps_8h, age_s) — Nones when
        never fetched. diff = hedge − entropy: positive means shorting the
        hedge leg and going long the entropy leg collects."""
        with self._lock:
            e, h, ts = self._e, self._h, self._ts
        diff = (h - e) if (e is not None and h is not None) else None
        age = (time.time() - ts) if ts else None
        return e, h, diff, age


# ───────────────────────────────────────────────────────────────────────────
# 宇宙級（§1.25）：一個輪詢器服務 150 個配對
# ───────────────────────────────────────────────────────────────────────────

class MuxFundingPoller:
    """每個場館一次請求，服務任意多個 (場館, ticker)。

    既有的 `FundingPoller` 是**一個配對一個**，而它每輪要打 2 個請求。
    150 個配對 = 300 個請求/輪，而端點本來就是全量的 —— 所以這裡是
    **3 個請求/輪**（HL、lighter、lighter-rh），跟配對數無關。

    `view(leg_a, leg_b, ticker)` 回傳一個只有 `snapshot()` 的輕量物件，
    介面與 `FundingPoller` **完全相同**，所以 `MinuteRecorder` 不用改。
    """

    def __init__(self, venues=("HL", "lighter", "lighter-rh"),
                 poll_sec: float = POLL_SEC) -> None:
        self.venues = tuple(venues)
        self.poll_sec = poll_sec
        self._lock = threading.Lock()
        self._rates = {v: {} for v in self.venues}   # venue -> {ticker: bps8h}
        self._ts = {v: 0.0 for v in self.venues}
        self._stop = threading.Event()
        self._thread = None
        self.polls = 0
        self.errors = {}

    def _poll_once(self) -> None:
        for v in self.venues:
            try:
                got = fetch_hl_all("") if v == "HL" else fetch_lighter_all(v)
            except Exception as exc:                              # noqa: BLE001
                self.errors[v] = repr(exc)
                log.debug("mux funding: %s failed: %r", v, exc)
                continue                 # 保留上一次的值，age 自己長大
            if not got:
                continue
            with self._lock:
                self._rates[v] = got
                self._ts[v] = time.time()
            self.errors.pop(v, None)
        self.polls += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self.poll_sec)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._poll_once()                # 先抓一次，不要讓第一分鐘空白
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mux-funding")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get(self, venue: str, ticker: str):
        with self._lock:
            return self._rates.get(venue, {}).get(ticker), self._ts.get(venue, 0.0)

    def coverage(self) -> dict:
        with self._lock:
            return {v: len(self._rates.get(v) or {}) for v in self.venues}

    def view(self, leg_a: str, leg_b: str, ticker: str):
        return _MuxView(self, leg_a, leg_b, ticker)


class _MuxView:
    """一個配對的唯讀視圖。`snapshot()` 的語意與 FundingPoller 逐字相同：
    `diff = hedge − entropy`（正值 = 空 hedge 腿、多 entropy 腿收得到）。"""

    def __init__(self, mux: "MuxFundingPoller", leg_a: str, leg_b: str,
                 ticker: str) -> None:
        self.mux, self.leg_a, self.leg_b, self.ticker = mux, leg_a, leg_b, ticker

    def snapshot(self) -> tuple:
        e, ts_e = self.mux.get(self.leg_a, self.ticker)
        h, ts_h = self.mux.get(self.leg_b, self.ticker)
        diff = (h - e) if (e is not None and h is not None) else None
        ts = min(t for t in (ts_e, ts_h) if t) if (ts_e and ts_h) else 0.0
        age = (time.time() - ts) if ts else None
        return e, h, diff, age
