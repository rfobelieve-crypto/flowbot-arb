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


class FundingPoller:
    """Background poller exposing the latest funding of both legs.

    entropy leg = Hyperliquid (core dex "" or a HIP-3 dex such as "io")
    hedge leg   = a Lighter deployment
    """

    def __init__(self, hl_coin: str, hl_dex: str,
                 lighter_venue: str, lighter_symbol: str) -> None:
        self.hl_coin = hl_coin.split(":")[-1]     # "io:SNDK" -> "SNDK"
        self.hl_dex = hl_dex or ""
        self.lighter_venue = lighter_venue
        self.lighter_symbol = lighter_symbol
        self._lock = threading.Lock()
        self._e: Optional[float] = None           # bps / 8h
        self._h: Optional[float] = None           # bps / 8h
        self._ts: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── fetchers ─────────────────────────────────────────────────────────

    def _fetch_hl(self) -> Optional[float]:
        body = {"type": "metaAndAssetCtxs"}
        if self.hl_dex:
            body["dex"] = self.hl_dex
        r = requests.post(HL_INFO, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        meta, ctxs = r.json()[0], r.json()[1]
        for a, c in zip(meta.get("universe", []), ctxs):
            name = a.get("name", "").split(":")[-1]
            if name == self.hl_coin:
                # HL funding is per HOUR -> x8 for the 8h convention
                return float(c.get("funding") or 0.0) * 8.0 * 1e4
        return None

    def _fetch_lighter(self) -> Optional[float]:
        base = LIGHTER_BASE.get(self.lighter_venue)
        if not base:
            return None
        r = requests.get(base + "/api/v1/funding-rates", timeout=TIMEOUT)
        r.raise_for_status()
        for row in r.json().get("funding_rates", []):
            # only the venue's OWN rate; the endpoint also mirrors
            # binance/bybit/hyperliquid reference rates
            if (row.get("exchange") == "lighter"
                    and row.get("symbol") == self.lighter_symbol):
                return float(row.get("rate") or 0.0) * 1e4   # already /8h
        return None

    # ── loop ─────────────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        e = h = None
        try:
            e = self._fetch_hl()
        except Exception as exc:
            log.debug("funding: HL fetch failed: %r", exc)
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
