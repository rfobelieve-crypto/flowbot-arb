#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-venue premium SCANNER (flow_system TODO §0.75b, 2026-08-30).

REST-only, no websockets, no credentials, no orders. Every cycle it snapshots
best bid/ask + top-of-book depth for EVERY symbol listed on both legs of

    Hyperliquid core  x  Lighter mainnet
    Hyperliquid core  x  Lighter Robinhood chain
    Entropy (HIP-3 dex "io")  x  Lighter mainnet / RH   (ticker aliases)

and appends one row per pair to logs/scan/scan_YYYYMMDD.csv. It also diffs
each venue's market list every cycle and appends listing/delisting events to
logs/scan/listings.csv (new listings are where the user expects the fat
mispricings to live; delistings are where two registered pairs died on
2026-08-30 before recording a single row).

This file only COLLECTS. Ranking lives in flow_system research/arb/scan_rank.py
with a metric frozen before this scanner produced its first row.

Run:  python tools/scanner.py            (loops forever; Ctrl+C to stop)
      python tools/scanner.py --once     (one cycle, for smoke tests)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

HL = "https://api.hyperliquid.xyz/info"
LIGHTER = {"lighter": "https://mainnet.zklighter.elliot.ai",
           "lighter-rh": "https://api.rh.lighter.xyz"}
ENTROPY_DEX = "io"
# Entropy ticker -> Lighter ticker where they differ
ALIAS = {"OAI": "OPENAI", "ANTH": "ANTHROPIC"}

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "scan")
CYCLE_SEC = 120
UNIVERSE_REFRESH_SEC = 1800
REQ_SPACING = 0.06          # be polite to Lighter's public REST
TIMEOUT = 12

HEADER = ["ts", "time_utc", "pair", "leg_a", "sym_a", "leg_b", "sym_b",
          "a_bid", "a_ask", "b_bid", "b_ask",
          "a_bid_usd", "a_ask_usd", "b_bid_usd", "b_ask_usd",
          "sell_edge_bps", "buy_edge_bps", "a_spread_bps", "b_spread_bps",
          "b_vol24_usd", "b_created_at"]

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S}Z {msg}", flush=True)


# ─────────────────────────────────────────────────────────────── universe ──

def hl_universe(dex: str = "") -> dict:
    body = {"type": "meta"}
    if dex:
        body["dex"] = dex
    r = requests.post(HL, json=body, timeout=TIMEOUT).json()
    out = {}
    for a in r.get("universe", []):
        if a.get("isDelisted"):
            continue
        name = a["name"]
        short = name.split(":")[-1]
        out[short] = name
    return out                      # short ticker -> HL coin name


def lighter_universe(venue: str) -> dict:
    r = requests.get(LIGHTER[venue] + "/api/v1/orderBookDetails",
                     timeout=TIMEOUT).json()
    out = {}
    for ob in r.get("order_book_details") or r.get("order_books") or []:
        if ob.get("status") != "active" or "/" in ob.get("symbol", ""):
            continue
        out[ob["symbol"]] = {
            "market_id": int(ob["market_id"]),
            "vol24": float(ob.get("daily_quote_token_volume") or 0.0),
            "created_at": ob.get("created_at"),
        }
    return out


def build_pairs():
    """Return (pairs, snapshot) where pairs = list of dicts with legs."""
    hl_core = hl_universe("")
    hl_io = hl_universe(ENTROPY_DEX)
    lt = {v: lighter_universe(v) for v in LIGHTER}
    pairs = []
    for v, lu in lt.items():
        for short, coin in hl_core.items():
            if short in lu:
                pairs.append({"pair": f"{short}@HL-{v}", "leg_a": "HL",
                              "sym_a": coin, "leg_b": v, "sym_b": short,
                              "b_meta": lu[short]})
        for short, coin in hl_io.items():
            bsym = ALIAS.get(short, short)
            if bsym in lu:
                pairs.append({"pair": f"{short}@IO-{v}", "leg_a": "ENTROPY",
                              "sym_a": coin, "leg_b": v, "sym_b": bsym,
                              "b_meta": lu[bsym]})
    snap = {"hl_core": sorted(hl_core), "hl_io": sorted(hl_io),
            **{f"{v}": sorted(lu) for v, lu in lt.items()}}
    return pairs, snap


def diff_listings(prev: dict, cur: dict, ts: int) -> list:
    rows = []
    for venue, syms in cur.items():
        before = set(prev.get(venue, []))
        now = set(syms)
        for s in sorted(now - before):
            rows.append([ts, venue, s, "listed" if prev else "seen_at_start"])
        for s in sorted(before - now):
            rows.append([ts, venue, s, "delisted_or_inactive"])
    return rows


# ───────────────────────────────────────────────────────────────── quotes ──

def hl_top(coin: str):
    r = requests.post(HL, json={"type": "l2Book", "coin": coin},
                      timeout=TIMEOUT).json()
    lv = r.get("levels") or [[], []]
    if not lv[0] or not lv[1]:
        return None
    b, a = lv[0][0], lv[1][0]
    bp, bs, ap, asz = float(b["px"]), float(b["sz"]), float(a["px"]), float(a["sz"])
    return bp, ap, bp * bs, ap * asz


def lighter_top(venue: str, market_id: int):
    r = requests.get(LIGHTER[venue] + "/api/v1/orderBookOrders",
                     params={"market_id": market_id, "limit": 1},
                     timeout=TIMEOUT).json()
    bids, asks = r.get("bids") or [], r.get("asks") or []
    if not bids or not asks:
        return None
    bp = float(bids[0]["price"]); bs = float(bids[0]["remaining_base_amount"])
    ap = float(asks[0]["price"]); asz = float(asks[0]["remaining_base_amount"])
    return bp, ap, bp * bs, ap * asz


# ───────────────────────────────────────────────────────────────── output ──

def _writer(path: str, header: list):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    fh = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(fh)
    if new:
        w.writerow(header)
        fh.flush()
    return fh, w


def scan_once(pairs: list) -> int:
    ts = int(time.time())
    tiso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    fh, w = _writer(os.path.join(OUT_DIR, f"scan_{day}.csv"), HEADER)
    n_ok = 0
    hl_cache: dict = {}
    try:
        for p in pairs:
            try:
                if p["sym_a"] not in hl_cache:
                    hl_cache[p["sym_a"]] = hl_top(p["sym_a"])
                    time.sleep(REQ_SPACING)
                a = hl_cache[p["sym_a"]]
                b = lighter_top(p["leg_b"], p["b_meta"]["market_id"])
                time.sleep(REQ_SPACING)
                if a is None or b is None:
                    continue
                a_bid, a_ask, a_bid_usd, a_ask_usd = a
                b_bid, b_ask, b_bid_usd, b_ask_usd = b
                if min(a_bid, a_ask, b_bid, b_ask) <= 0:
                    continue
                sell_edge = (a_bid / b_ask - 1) * 1e4     # sell A, buy B
                buy_edge = (b_bid / a_ask - 1) * 1e4      # buy A, sell B
                w.writerow([ts, tiso, p["pair"], p["leg_a"], p["sym_a"],
                            p["leg_b"], p["sym_b"],
                            a_bid, a_ask, b_bid, b_ask,
                            round(a_bid_usd, 2), round(a_ask_usd, 2),
                            round(b_bid_usd, 2), round(b_ask_usd, 2),
                            round(sell_edge, 3), round(buy_edge, 3),
                            round((a_ask / a_bid - 1) * 1e4, 3),
                            round((b_ask / b_bid - 1) * 1e4, 3),
                            round(p["b_meta"]["vol24"], 0),
                            p["b_meta"]["created_at"]])
                n_ok += 1
            except Exception as e:                      # one pair must never kill the cycle
                log(f"pair {p['pair']} failed: {e!r}")
        fh.flush()
    finally:
        fh.close()
    return n_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    uni_path = os.path.join(OUT_DIR, "universe.json")
    prev_snap = {}
    if os.path.exists(uni_path):
        try:
            prev_snap = json.load(open(uni_path, encoding="utf-8")).get("venues", {})
        except Exception:
            prev_snap = {}
    pairs, snap, last_uni = [], {}, 0.0
    while True:
        t0 = time.time()
        try:
            if t0 - last_uni >= UNIVERSE_REFRESH_SEC or not pairs:
                pairs, snap = build_pairs()
                last_uni = t0
                ev = diff_listings(prev_snap, snap, int(t0))
                if ev:
                    fh, w = _writer(os.path.join(OUT_DIR, "listings.csv"),
                                    ["ts", "venue", "symbol", "event"])
                    for r in ev:
                        w.writerow(r)
                    fh.close()
                    log(f"listing events: {len(ev)} "
                        f"({sum(1 for r in ev if r[3]=='listed')} listed, "
                        f"{sum(1 for r in ev if r[3].startswith('delisted'))} delisted)")
                prev_snap = snap
                json.dump({"asof": int(t0), "pairs": len(pairs), "venues": snap},
                          open(uni_path, "w", encoding="utf-8"), ensure_ascii=False)
                log(f"universe: {len(pairs)} pairs "
                    f"(HL core {len(snap['hl_core'])}, io {len(snap['hl_io'])}, "
                    f"lighter {len(snap['lighter'])}, rh {len(snap['lighter-rh'])})")
            n = scan_once(pairs)
            log(f"cycle: {n}/{len(pairs)} pairs quoted in {time.time()-t0:.1f}s")
        except Exception as e:
            log(f"cycle failed: {e!r}")
        if args.once:
            return 0
        time.sleep(max(5.0, CYCLE_SEC - (time.time() - t0)))


if __name__ == "__main__":
    raise SystemExit(main())
