#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-venue premium SCANNER (flow_system TODO §0.75b).

REST-only, no websockets, no credentials, no orders. Every cycle it snapshots
best bid/ask + top-of-book depth for every symbol that TWO venues both list,
and appends one row per pair to logs/scan/scan_YYYYMMDD.csv. It also diffs each
venue's market list every cycle into logs/scan/listings.csv.

v3 (2026-09-03) — BREADTH: every live Hyperliquid dex, not just Entropy
--------------------------------------------------------------------
v1/v2 scanned HL-core x Lighter and Entropy("io") x Lighter = 113 pairs, all
crypto plus 5 stock perps.  But Hyperliquid has ELEVEN perp dexes (`perpDexs`),
and the biggest of them is not io: `xyz` turns over ~$2.2B/24h and lists GOLD,
SILVER, COPPER, PLATINUM, PALLADIUM, CL + BRENTOIL, NATGAS, SP500, XYZ100,
JP225, KR200, EUR/GBP/JPY and ~90 single stocks.  `para` lists 2Y/10Y/30Y
yields and more stocks; `mkts` lists index perps.  Several of those tickers
are ALSO on Lighter's Robinhood chain (XAU, XAG, SPY, QQQ, USO, SLV, 20+
stocks) and on each other.  Same underlying, different books = the same
question this line already asks, on instruments that are not crypto.

Three things this version adds, all COLLECTION-side (the promotion metric in
flow_system research/arb/scan_rank.py is untouched and stays frozen):
  1. venues = HL core + EVERY builder dex + both Lighter chains
  2. pairs = every canonical ticker carried by >= 2 venues, including
     HL-dex vs HL-dex (same chain, no bridge — a materially easier trade)
  3. two cheap instrument guards, because breadth multiplies the ways a
     number can be fake:
       * an asset with 0 USD of 24h volume on its venue is not a leg
         (flx/vntl/km/abcd/cash were all $0 with empty books on 2026-09-03)
       * SCALE GUARD: if the two legs' mids differ by more than 2x, the
         tickers are not the same unit (index vs ETF, e.g. SP500 vs SPY) —
         skip the row and log it, rather than record a 9000 bps "edge"

Pair names keep their v1 spelling for the venues that existed then
("BTC@HL-lighter", "ANTH@IO-lighter") so their history stays one series.

This file only COLLECTS.  Ranking lives in flow_system research/arb/
scan_rank.py with a metric frozen before this scanner produced its first row.

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

HL = "https://api.hyperliquid.xyz/info"
LIGHTER = {"lighter": "https://mainnet.zklighter.elliot.ai",
           "lighter-rh": "https://api.rh.lighter.xyz"}

# Venue id -> pair-name tag.  "" is HL core; anything else is a HIP-3 builder
# dex id.  The two tags below are pinned to their v1 spelling so pair names
# recorded since 2026-08-30 keep accumulating in the same series.
NAME = {"": "HL", "io": "IO"}

# Same underlying, different ticker.  Kept deliberately short: a wrong entry
# here manufactures a permanent fake spread.  ETF-vs-index look-alikes
# (SPY/SP500, QQQ/XYZ100, USO/CL, SLV/SILVER) are NOT aliased — different
# units and different carry; the scale guard would drop them anyway.
CANON = {"OPENAI": "OAI", "ANTHROPIC": "ANTH", "XAU": "GOLD", "XAG": "SILVER"}

MIN_VOL24_USD = 1.0        # a market with no 24h volume is not a leg
SCALE_MAX = 2.0            # legs whose mids differ by more than this are not
SCALE_MIN = 0.5            # the same instrument (index vs ETF, etc.)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "scan")
CYCLE_SEC = 120
UNIVERSE_REFRESH_SEC = 1800
REQ_SPACING = 0.06          # be polite to Lighter's public REST
HL_WORKERS = 4              # HL allows ~600 l2Book calls/min; this stays well under
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

def hl_dexes() -> list:
    """[""] + every builder dex id."""
    try:
        r = requests.post(HL, json={"type": "perpDexs"}, timeout=TIMEOUT).json()
    except Exception as e:
        log(f"perpDexs failed: {e!r}")
        return [""]
    out = [""]
    for d in r:
        if d and d.get("name"):
            out.append(d["name"])
    return out


def hl_universe(dex: str = "") -> dict:
    """short ticker -> {coin, vol24}.  Zero-volume assets are dropped."""
    body = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    r = requests.post(HL, json=body, timeout=TIMEOUT).json()
    meta, ctxs = r[0], r[1]
    out = {}
    for a, c in zip(meta.get("universe", []), ctxs):
        if a.get("isDelisted"):
            continue
        try:
            vol = float(c.get("dayNtlVlm") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol < MIN_VOL24_USD:
            continue
        name = a["name"]
        out[name.split(":")[-1]] = {"coin": name, "vol24": vol}
    return out


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
    """Return (pairs, snapshot).  A pair = one canonical ticker on two venues."""
    venues = {}                       # venue id -> {ticker -> leg meta}
    for dex in hl_dexes():
        try:
            u = hl_universe(dex)
        except Exception as e:
            log(f"HL meta {dex or 'core'} failed: {e!r}")
            continue
        if u:
            venues[dex] = {t: {"kind": "hl", "sym": m["coin"],
                               "vol24": m["vol24"], "created_at": ""}
                           for t, m in u.items()}
        time.sleep(REQ_SPACING)
    for v in LIGHTER:
        try:
            u = lighter_universe(v)
        except Exception as e:
            log(f"lighter {v} failed: {e!r}")
            continue
        venues[v] = {t: {"kind": "lighter", "venue": v, "sym": t,
                         "market_id": m["market_id"], "vol24": m["vol24"],
                         "created_at": m["created_at"]}
                     for t, m in u.items()}

    # canonical ticker -> [(venue id, leg meta)]
    book: dict = {}
    for vid, syms in venues.items():
        for t, meta in syms.items():
            book.setdefault(CANON.get(t, t), []).append((vid, meta))

    order = list(venues)
    pairs = []
    for canon, legs in book.items():
        if len(legs) < 2:
            continue
        legs.sort(key=lambda x: order.index(x[0]))
        for i in range(len(legs)):
            for j in range(i + 1, len(legs)):
                (va, ma), (vb, mb) = legs[i], legs[j]
                pairs.append({
                    "pair": f"{canon}@{NAME.get(va, va)}-{NAME.get(vb, vb)}",
                    "leg_a": NAME.get(va, va), "a": ma,
                    "leg_b": NAME.get(vb, vb), "b": mb,
                })
    snap = {(vid or "hl_core"): sorted(syms) for vid, syms in venues.items()}
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


def quote_all(pairs: list) -> dict:
    """One quote per distinct leg per cycle, keyed (kind, venue, sym)."""
    jobs = {}
    for p in pairs:
        for side in ("a", "b"):
            m = p[side]
            k = (m["kind"], m.get("venue", ""), m["sym"])
            jobs[k] = m
    hl_jobs = [k for k in jobs if k[0] == "hl"]
    lt_jobs = [k for k in jobs if k[0] == "lighter"]
    out = {}
    with ThreadPoolExecutor(max_workers=HL_WORKERS) as ex:
        for k, q in zip(hl_jobs, ex.map(lambda k: _safe(hl_top, k[2]), hl_jobs)):
            out[k] = q
    for k in lt_jobs:
        out[k] = _safe(lighter_top, k[1], jobs[k]["market_id"])
        time.sleep(REQ_SPACING)
    return out


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


# ───────────────────────────────────────────────────────────────── output ──

def _writer(path: str, header: list):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    fh = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(fh)
    if new:
        w.writerow(header)
        fh.flush()
    return fh, w


def scan_once(pairs: list) -> tuple:
    ts = int(time.time())
    tiso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    q = quote_all(pairs)
    fh, w = _writer(os.path.join(OUT_DIR, f"scan_{day}.csv"), HEADER)
    n_ok = n_scale = 0
    try:
        for p in pairs:
            try:
                ma, mb = p["a"], p["b"]
                a = q.get((ma["kind"], ma.get("venue", ""), ma["sym"]))
                b = q.get((mb["kind"], mb.get("venue", ""), mb["sym"]))
                if a is None or b is None:
                    continue
                a_bid, a_ask, a_bid_usd, a_ask_usd = a
                b_bid, b_ask, b_bid_usd, b_ask_usd = b
                if min(a_bid, a_ask, b_bid, b_ask) <= 0:
                    continue
                ratio = ((a_bid + a_ask) / 2) / ((b_bid + b_ask) / 2)
                if not (SCALE_MIN <= ratio <= SCALE_MAX):
                    n_scale += 1                 # not the same unit — see docstring
                    continue
                sell_edge = (a_bid / b_ask - 1) * 1e4     # sell A, buy B
                buy_edge = (b_bid / a_ask - 1) * 1e4      # buy A, sell B
                w.writerow([ts, tiso, p["pair"], p["leg_a"], ma["sym"],
                            p["leg_b"], mb["sym"],
                            a_bid, a_ask, b_bid, b_ask,
                            round(a_bid_usd, 2), round(a_ask_usd, 2),
                            round(b_bid_usd, 2), round(b_ask_usd, 2),
                            round(sell_edge, 3), round(buy_edge, 3),
                            round((a_ask / a_bid - 1) * 1e4, 3),
                            round((b_ask / b_bid - 1) * 1e4, 3),
                            round(mb.get("vol24") or 0.0, 0),
                            mb.get("created_at") or ""])
                n_ok += 1
            except Exception as e:              # one pair must never kill the cycle
                log(f"pair {p['pair']} failed: {e!r}")
        fh.flush()
    finally:
        fh.close()
    return n_ok, n_scale


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
                log(f"universe: {len(pairs)} pairs over {len(snap)} venues "
                    + ", ".join(f"{v}:{len(s)}" for v, s in snap.items()))
            n, n_scale = scan_once(pairs)
            log(f"cycle: {n}/{len(pairs)} pairs quoted in {time.time()-t0:.1f}s"
                + (f" ({n_scale} skipped: scale mismatch)" if n_scale else ""))
        except Exception as e:
            log(f"cycle failed: {e!r}")
        if args.once:
            return 0
        time.sleep(max(5.0, CYCLE_SEC - (time.time() - t0)))


if __name__ == "__main__":
    raise SystemExit(main())
