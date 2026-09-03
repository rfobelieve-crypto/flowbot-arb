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

v4 (2026-09-03) - CEX LEGS. The operator holds a 50% fee rebate on OKX and
Bitget, and both list the same non-crypto products (OKX: XAG, SPY, QQQ, NVDA,
TSLA, AAPL, COIN, MSTR; Bitget: those plus XAUT, PAXG, COPPER, NDX100). A
rebated CEX leg costs ~2.5-3 bps against Lighter's structural 0, which needs a
band of ~10 bps instead of the 18 bps an unrebated Hyperliquid leg needs. So
the CEXes join as venues; the pairing logic is unchanged. Fee rates go into
universe.json (Bitget publishes them per contract), NOT into the CSV -- the row
schema must stay stable for scan_rank.py.

v5 (2026-09-03) - CUMULATIVE DEPTH. Top-of-book was the only size recorded,
so every capacity estimate assumed you can trade the first level and nothing
else. A live Bitget XAUT book: $752 at best, $5,974 within 0.5 bps. Now each
row also carries the depth reachable within 1 bps and within 3 bps on both
legs, so "how much can actually be traded" stops being a guess. Written to
scan_v5_YYYYMMDD.csv; the frozen promotion metric still reads top-of-book.

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
OKX = "https://www.okx.com/api/v5"
BITGET = "https://api.bitget.com/api/v2/mix/market"
BITGET_PT = "USDT-FUTURES"

# Venue id -> pair-name tag.  "" is HL core; anything else is a HIP-3 builder
# dex id.  The two tags below are pinned to their v1 spelling so pair names
# recorded since 2026-08-30 keep accumulating in the same series.
NAME = {"": "HL", "io": "IO"}
CEX_FEES: dict = {}   # venue -> ticker -> {taker,maker} bps (Bitget publishes them)
LAST_GOOD: dict = {}  # venue -> universe; a failed fetch reuses it (see below)

# Same underlying, different ticker.  Kept deliberately short: a wrong entry
# here manufactures a permanent fake spread.  ETF-vs-index look-alikes
# (SPY/SP500, QQQ/XYZ100, USO/CL, SLV/SILVER) are NOT aliased — different
# units and different carry; the scale guard would drop them anyway.
# XAUT/PAXG are tokenised gold: they track spot gold but carry their own token
# basis, so pairing them with GOLD is a HYPOTHESIS for the convergence gate to
# test, not an identity. Same reasoning that keeps SPY/SP500 unaliased.
CANON = {"OPENAI": "OAI", "ANTHROPIC": "ANTH", "XAU": "GOLD", "XAG": "SILVER",
         "XAUT": "GOLD", "PAXG": "GOLD"}

MIN_VOL24_USD = 1.0        # a market with no 24h volume is not a leg
SCALE_MAX = 2.0            # legs whose mids differ by more than this are not
SCALE_MIN = 0.5            # the same instrument (index vs ETF, etc.)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "scan")
CYCLE_SEC = 180
UNIVERSE_REFRESH_SEC = 1800
REQ_SPACING = 0.06          # be polite to Lighter's public REST
HL_WORKERS = 4              # HL allows ~600 l2Book calls/min; this stays well under
CEX_WORKERS = 4             # OKX/Bitget public books; well inside their IP limits
TIMEOUT = 12

HEADER = ["ts", "time_utc", "pair", "leg_a", "sym_a", "leg_b", "sym_b",
          "a_bid", "a_ask", "b_bid", "b_ask",
          "a_bid_usd", "a_ask_usd", "b_bid_usd", "b_ask_usd",
          "sell_edge_bps", "buy_edge_bps", "a_spread_bps", "b_spread_bps",
          "b_vol24_usd", "b_created_at",
          # v5: cumulative size reachable within 1 / 3 bps of the best price.
          # Top-of-book is what you get at zero slippage; these say what you
          # get if you are willing to pay a little, which is how the trade is
          # actually sized.
          "a_bid_usd_1bps", "a_ask_usd_1bps", "b_bid_usd_1bps", "b_ask_usd_1bps",
          "a_bid_usd_3bps", "a_ask_usd_3bps", "b_bid_usd_3bps", "b_ask_usd_3bps"]
DEPTH_BPS = (1.0, 3.0)
BOOK_LEVELS = 25

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


def okx_universe() -> dict:
    """ticker -> leg meta. OKX quotes size in CONTRACTS, so ctVal rides along
    for the notional maths."""
    inst = requests.get(f"{OKX}/public/instruments",
                        params={"instType": "SWAP"}, timeout=TIMEOUT).json()
    tick = requests.get(f"{OKX}/market/tickers",
                        params={"instType": "SWAP"}, timeout=TIMEOUT).json()
    vol = {t["instId"]: float(t.get("volCcy24h") or 0) * float(t.get("last") or 0)
           for t in tick.get("data", [])}
    out = {}
    for d in inst.get("data", []):
        iid = d["instId"]
        if d.get("state") != "live" or not iid.endswith("-USDT-SWAP"):
            continue
        v = vol.get(iid, 0.0)
        if v < MIN_VOL24_USD:
            continue
        out[iid.split("-")[0]] = {"kind": "okx", "venue": "okx", "sym": iid,
                                  "mult": float(d.get("ctVal") or 1.0),
                                  "vol24": v, "created_at": ""}
    return out


def bitget_universe() -> dict:
    con = requests.get(f"{BITGET}/contracts",
                       params={"productType": BITGET_PT}, timeout=TIMEOUT).json()
    tick = requests.get(f"{BITGET}/tickers",
                        params={"productType": BITGET_PT}, timeout=TIMEOUT).json()
    vol = {t["symbol"]: float(t.get("usdtVolume") or 0)
           for t in (tick.get("data") or [])}
    out, fees = {}, {}
    for d in (con.get("data") or []):
        sym = d["symbol"]
        if d.get("symbolStatus") not in (None, "normal") or d.get("quoteCoin") != "USDT":
            continue
        v = vol.get(sym, 0.0)
        if v < MIN_VOL24_USD:
            continue
        base = d.get("baseCoin") or sym[:-4]
        # mult = 1.0, NOT sizeMultiplier. Bitget's USDT-FUTURES book quotes
        # size in BASE COIN already (BTCUSDT top bid 1.8754 = 1.8754 BTC =
        # ~$146k), while `sizeMultiplier` is the minimum size STEP (0.0001 for
        # BTC, 0.01 for XAUT). Using it as a contract value divided every
        # Bitget depth by 10,000 and made the CEX legs look like empty books —
        # the exact opposite of why they were added. OKX is different and does
        # need its multiplier: its size is in CONTRACTS and ctVal is the coin
        # per contract (verified 2026-09-03 against both live books).
        out[base] = {"kind": "bitget", "venue": "bitget", "sym": sym,
                     "mult": 1.0,
                     "vol24": v, "created_at": ""}
        fees[base] = {"taker": round(float(d.get("takerFeeRate") or 0) * 1e4, 2),
                      "maker": round(float(d.get("makerFeeRate") or 0) * 1e4, 2)}
    out["_fees"] = fees            # popped by build_pairs, kept for universe.json
    return out


def okx_top(inst_id: str, mult: float):
    r = requests.get(f"{OKX}/market/books",
                     params={"instId": inst_id, "sz": BOOK_LEVELS},
                     timeout=TIMEOUT).json()
    d = (r.get("data") or [{}])[0]
    b, a = d.get("bids") or [], d.get("asks") or []
    if not b or not a:
        return None
    bids = [(float(x[0]), float(x[1])) for x in b]
    asks = [(float(x[0]), float(x[1])) for x in a]
    return (bids[0][0], asks[0][0],
            _cum(bids, bids[0][0], True, mult), _cum(asks, asks[0][0], False, mult))


def bitget_top(symbol: str, mult: float):
    r = requests.get(f"{BITGET}/orderbook",
                     params={"symbol": symbol, "productType": BITGET_PT,
                             "limit": BOOK_LEVELS}, timeout=TIMEOUT).json()
    d = r.get("data") or {}
    b, a = d.get("bids") or [], d.get("asks") or []
    if not b or not a:
        return None
    bids = [(float(x[0]), float(x[1])) for x in b]
    asks = [(float(x[0]), float(x[1])) for x in a]
    return (bids[0][0], asks[0][0],
            _cum(bids, bids[0][0], True, mult), _cum(asks, asks[0][0], False, mult))


LAST_GOOD_PATH = os.path.join(OUT_DIR, "last_good_universe.json")


def _load_last_good() -> None:
    """The in-memory fallback below dies with the process, and the process is
    restarted by a batch loop. 2026-09-03: a restart landed on one of Lighter's
    JSONDecodeErrors with an empty cache and ran a whole refresh interval
    without the venue that half the pairs need. Persist it."""
    try:
        LAST_GOOD.update(json.load(open(LAST_GOOD_PATH, encoding="utf-8")))
        log(f"last-good universe cache loaded ({len(LAST_GOOD)} venues)")
    except Exception:
        pass


def _fetch(fn, *a, tries: int = 3, delay: float = 2.0):
    """Retry before falling back — most of these failures are one bad reply."""
    last = None
    for i in range(tries):
        try:
            return fn(*a), None
        except Exception as e:
            last = e
            if i + 1 < tries:
                time.sleep(delay)
    return None, last


def _keep(venues: dict, vid: str, universe: dict | None, err=None) -> str:
    """Record a venue, or fall back to its last good universe.

    A transient REST failure must never quietly shrink the scan: on
    2026-09-03 one JSONDecodeError from Lighter cut 1629 pairs to 1068 for a
    full refresh interval, with one log line as the only evidence.

    Returns one of four OUTCOMES, because they are four different events and
    the log used to print three of them as the same sentence ("universe
    failed"). Six of the eleven HL dexes carry no traded market at all — every
    refresh printed six failure lines for a venue that is simply empty, which
    is how a real failure gets lost among the noise:

      ok     - fetched, has markets
      empty  - fetched fine, the venue genuinely has nothing with volume
      stale  - fetch failed OR came back suspiciously empty; reusing cache
      lost   - fetch failed and there is no cache
    """
    if universe:
        venues[vid] = universe
        LAST_GOOD[vid] = universe
        try:
            json.dump(LAST_GOOD, open(LAST_GOOD_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False)
        except Exception:
            pass
        return "ok"
    stale = LAST_GOOD.get(vid)
    if err is None and not stale:
        # Fetch succeeded and returned nothing. For flx/vntl/km/abcd/cash/hyna
        # that IS the answer, and it is not news. Counted, not logged.
        return "empty"
    if stale:
        venues[vid] = stale
        # An empty reply from a venue that had markets a moment ago is far more
        # likely to be an upstream glitch than a mass delisting, so it takes the
        # same path as an exception — but it says which one happened.
        why = repr(err) if err is not None else "returned an EMPTY universe"
        log(f"{vid or 'hl_core'} universe {why} - reusing last good "
            f"({len(stale)} symbols)")
        return "stale"
    log(f"{vid or 'hl_core'} universe FETCH FAILED ({err!r}) - no cached copy, "
        f"this venue is missing from this cycle")
    return "lost"


def build_pairs():
    """Return (pairs, snapshot).  A pair = one canonical ticker on two venues."""
    venues = {}                       # venue id -> {ticker -> leg meta}
    empty = []                        # venues with nothing traded — one line, not six
    for dex in hl_dexes():
        u, err = _fetch(hl_universe, dex)
        if _keep(venues, dex,
                 {t: {"kind": "hl", "sym": m["coin"], "vol24": m["vol24"],
                      "created_at": ""} for t, m in u.items()} if u else None,
                 err) == "empty":
            empty.append(dex or "hl_core")
        time.sleep(REQ_SPACING)
    for v in LIGHTER:
        u, err = _fetch(lighter_universe, v)
        if _keep(venues, v,
                 {t: {"kind": "lighter", "venue": v, "sym": t,
                      "market_id": m["market_id"], "vol24": m["vol24"],
                      "created_at": m["created_at"]} for t, m in u.items()}
                 if u else None, err) == "empty":
            empty.append(v)
    for name, fn in (("okx", okx_universe), ("bitget", bitget_universe)):
        u, err = _fetch(fn)
        if u:
            CEX_FEES[name] = u.pop("_fees", {})
        if _keep(venues, name, u, err) == "empty":
            empty.append(name)
    if empty:
        log(f"no traded market (skipped): {', '.join(empty)}")

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


# A venue whose universe fetch failed comes back with its whole market list,
# and a naive set-difference calls every one of them a new listing: 2,006 of
# the first 2,023 events were that (flow_system TODO 1.05, 2026-09-04). The
# few REAL ones -- PONS appearing on three venues over three days, GPRO on io
# then Bitget two days later -- were buried under them.
#
# Two rules, and neither one deletes anything (an event that is reclassified
# stays in the file under its own label, so a later reader can disagree):
#   * a venue absent from the previous snapshot is `seen_at_start`, never
#     `listed` -- we did not watch it appear, we just started looking.
#   * more than MASS_EVENT symbols moving the same way in one venue in one
#     cycle is a fetch artifact, not a market event: labelled
#     `mass_reappear` / `mass_vanish`.
# The threshold is deliberately low. A venue really adding 5 markets in one
# 3-minute cycle is rarer than a fetch failing, and the mislabelled real
# event still sits in the file with its timestamp.
MASS_EVENT = 5


def diff_listings(prev: dict, cur: dict, ts: int) -> list:
    rows = []
    for venue, syms in cur.items():
        if venue not in prev:
            for s in sorted(set(syms)):
                rows.append([ts, venue, s, "seen_at_start"])
            continue
        before, now = set(prev.get(venue, [])), set(syms)
        added, gone = sorted(now - before), sorted(before - now)
        add_ev = "mass_reappear" if len(added) >= MASS_EVENT else "listed"
        gone_ev = ("mass_vanish" if len(gone) >= MASS_EVENT
                   else "delisted_or_inactive")
        for s in added:
            rows.append([ts, venue, s, add_ev])
        for s in gone:
            rows.append([ts, venue, s, gone_ev])
    for venue in prev:
        if venue not in cur:
            rows.append([ts, venue, "*", "venue_fetch_missing"])
    return rows


# ───────────────────────────────────────────────────────────────── quotes ──

def _cum(levels, best: float, is_bid: bool, mult: float = 1.0) -> tuple:
    """(top-of-book USD, USD within 1 bps, USD within 3 bps).

    levels: [(price, size), ...] already sorted best-first.
    A bid is reachable down to best*(1-x); an ask up to best*(1+x).
    """
    out, top = [], 0.0
    for i, bps in enumerate(DEPTH_BPS):
        lim = best * (1 - bps / 1e4) if is_bid else best * (1 + bps / 1e4)
        cum = 0.0
        for px, sz in levels:
            if (px < lim) if is_bid else (px > lim):
                break
            cum += px * sz * mult
        out.append(round(cum, 2))
    if levels:
        top = round(levels[0][0] * levels[0][1] * mult, 2)
    return (top, *out)


def hl_top(coin: str):
    r = requests.post(HL, json={"type": "l2Book", "coin": coin},
                      timeout=TIMEOUT).json()
    lv = r.get("levels") or [[], []]
    if not lv[0] or not lv[1]:
        return None
    bids = [(float(x["px"]), float(x["sz"])) for x in lv[0][:BOOK_LEVELS]]
    asks = [(float(x["px"]), float(x["sz"])) for x in lv[1][:BOOK_LEVELS]]
    return bids[0][0], asks[0][0], _cum(bids, bids[0][0], True), _cum(asks, asks[0][0], False)


def lighter_top(venue: str, market_id: int):
    r = requests.get(LIGHTER[venue] + "/api/v1/orderBookOrders",
                     params={"market_id": market_id, "limit": BOOK_LEVELS},
                     timeout=TIMEOUT).json()
    b, a = r.get("bids") or [], r.get("asks") or []
    if not b or not a:
        return None
    bids = [(float(x["price"]), float(x["remaining_base_amount"])) for x in b]
    asks = [(float(x["price"]), float(x["remaining_base_amount"])) for x in a]
    return bids[0][0], asks[0][0], _cum(bids, bids[0][0], True), _cum(asks, asks[0][0], False)


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
    cex_jobs = [k for k in jobs if k[0] in ("okx", "bitget")]
    out = {}
    with ThreadPoolExecutor(max_workers=HL_WORKERS) as ex:
        for k, q in zip(hl_jobs, ex.map(lambda k: _safe(hl_top, k[2]), hl_jobs)):
            out[k] = q

    def _cex(k):
        fn = okx_top if k[0] == "okx" else bitget_top
        return _safe(fn, k[2], jobs[k].get("mult", 1.0))

    with ThreadPoolExecutor(max_workers=CEX_WORKERS) as ex:
        for k, q in zip(cex_jobs, ex.map(_cex, cex_jobs)):
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
    fh, w = _writer(os.path.join(OUT_DIR, f"scan_v5_{day}.csv"), HEADER)
    n_ok = n_scale = 0
    try:
        for p in pairs:
            try:
                ma, mb = p["a"], p["b"]
                a = q.get((ma["kind"], ma.get("venue", ""), ma["sym"]))
                b = q.get((mb["kind"], mb.get("venue", ""), mb["sym"]))
                if a is None or b is None:
                    continue
                a_bid, a_ask, a_bidd, a_askd = a
                b_bid, b_ask, b_bidd, b_askd = b
                a_bid_usd, b_bid_usd = a_bidd[0], b_bidd[0]
                a_ask_usd, b_ask_usd = a_askd[0], b_askd[0]
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
                            mb.get("created_at") or "",
                            a_bidd[1], a_askd[1], b_bidd[1], b_askd[1],
                            a_bidd[2], a_askd[2], b_bidd[2], b_askd[2]])
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
    _load_last_good()
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
                    n_real = sum(1 for r in ev if r[3] == "listed")
                    n_mass = sum(1 for r in ev if r[3].startswith("mass_"))
                    log(f"listing events: {len(ev)} "
                        f"({n_real} listed, "
                        f"{sum(1 for r in ev if r[3].startswith('delisted'))} delisted"
                        + (f", {n_mass} mass/artifact" if n_mass else "") + ")")
                    if n_real:
                        for r in ev:
                            if r[3] == "listed":
                                log(f"  NEW LISTING: {r[1]} {r[2]}")
                prev_snap = snap
                json.dump({"asof": int(t0), "pairs": len(pairs), "venues": snap,
                           "cex_fees_bps": CEX_FEES},
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
