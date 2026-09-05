# -*- coding: utf-8 -*-
"""§0.75b scanner ranking — the FROZEN promotion metric (2026-08-30).

Written before the scanner (engine/tools/scanner.py) had produced
more than one cycle. It ranks every scanned pair by

    capturable_usd_per_day = fires_per_day x band_bps/1e4 x depth_usd

per side (sell A/buy B, buy A/sell B), keeps the larger side, where
  band    = p90 of the POSITIVE executable edge (analyze.py's methodology,
            identical to the recording family's gate — no sweep)
  fires   = samples with edge >= band, per day of scan span
  depth   = median over those fat samples of min(top-of-book USD on the two
            legs that the trade would hit)

Why money and not bps: SNDK's interim showed a pair can have a clean 5 bps
band on $200 books — cents per day. Ranking by bps would promote exactly
those.

Discipline (TODO §0.75b):
  * a pair is LISTED only after >= MIN_SPAN_DAYS of scanning and
    >= MIN_SAMPLES quotes — no one-cycle winners
  * listed < 48h on the Lighter leg -> excluded (book not built yet)
  * BTC pairs are always printed first as the CONTROL: their band is the
    instrument's noise floor. Anything whose band is within CONTROL_MULT x
    the BTC band is "not distinguishable from spread" and cannot be promoted
  * promotion = top PROMOTE_N by capturable_usd_per_day; the promoted pair's
    recording clock starts from ITS OWN first recorded minute, and the scan
    data that selected it never enters its verdict (selection window !=
    verification window)

Read-only. Output: results/arb_scan_rank.json
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent  # the arb/ repo root
SCAN = ROOT / "engine" / "logs" / "scan"
OUT = ROOT / "results" / "arb_scan_rank.json"

MIN_SPAN_DAYS = 3.0
MIN_SAMPLES = 500
MIN_LISTED_H = 48.0
CONTROL_MULT = 2.0
PROMOTE_N = 3

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# Every Bitget row recorded before this instant carries a depth that is wrong
# by a factor of 1/sizeMultiplier (10,000x for BTC, 100x for XAUT): the scanner
# treated Bitget's minimum size STEP as a contract value, though its book is
# quoted in base coin. Dropping those rows is an INSTRUMENT correction, not a
# criterion change — the metric below is byte-for-byte the one frozen on
# 2026-08-30. Keeping them would blend two units into one median.
BITGET_DEPTH_FIX_TS = 1788432000   # 2026-09-03 19:20 UTC, scanner restart


def load() -> pd.DataFrame:
    files = sorted(glob.glob(str(SCAN / "scan_*.csv")))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    # B8 (2026-09-05): the scanner no longer EMITS same-venue pairs, but the
    # historical CSVs are full of them -- Bitget lists XAUT and PAXG, both of
    # which used to canonicalise to GOLD, so GOLD@bitget-bitget exists in the
    # data and was sitting in the promotion top three. One venue is not an
    # arbitrage: there is no second book to hedge on, and config.py refuses
    # to load such a pair. Dropping them is an INSTRUMENT correction of the
    # same class as the Bitget depth fix below, not a criterion change.
    same = df.leg_a == df.leg_b
    if same.any():
        print(f"  （丟棄 {int(same.sum()):,} 筆同場館配對——一個交易所不是套利："
              f"{'，'.join(sorted(df.loc[same, 'pair'].unique()[:3]))} 等）")
        df = df[~same]
    bad = (((df.leg_a == "bitget") | (df.leg_b == "bitget"))
           & (df.ts < BITGET_DEPTH_FIX_TS))
    if bad.any():
        print(f"  （丟棄 {int(bad.sum()):,} 筆修正前的 Bitget 列——深度單位錯 "
              f"{'，'.join(sorted(df.loc[bad, 'pair'].unique()[:3]))} 等）")
        df = df[~bad]
    return df.sort_values("ts").reset_index(drop=True)


def two_sided(g: pd.DataFrame) -> dict:
    """Can this pair's premium even REACH both sides? (2026-09-05)

    An identity, not a heuristic. At any single instant

        sell_edge + buy_edge = -(spread_a + spread_b)

    so the two directions are never executable at the same moment; their sum
    is minus the total spread. A round trip therefore needs the premium to
    travel across BOTH spreads at different times. If it does not oscillate
    that far, the pair can be entered and never unwound -- one-sided by
    arithmetic, whatever the band and depth say.

    Measured on the recording family 2026-09-05: seven of nine pairs fail
    this, two of them (GOLD_LL, NVDA_LL) with a premium standard deviation
    under 1 bps against spreads of 1.5-2.9 bps. Those are not "wrong
    midline" pairs; they have no oscillation to arbitrage at all.

    `margin_bps` = 2 sigma of the premium MINUS the two spreads. Fees are
    deliberately NOT included: this is an instrument question (can it reach?)
    and a negative margin cannot be rescued by any fee schedule. Fees come
    out of whatever margin is left.

    This is REPORTED, not applied. The promotion metric is the one frozen on
    2026-08-30 and nothing here reorders it.
    """
    a_mid = (g.a_bid + g.a_ask) / 2.0
    b_mid = (g.b_bid + g.b_ask) / 2.0
    ok = (a_mid > 0) & (b_mid > 0)
    if ok.sum() < 30:
        return {"premium_std_bps": None, "spread_sum_bps": None,
                "margin_bps": None, "reaches_both": None}
    prem = (a_mid[ok] / b_mid[ok] - 1.0) * 1e4
    sd = float(prem.std())
    spread = float((g.a_spread_bps[ok] + g.b_spread_bps[ok]).median())
    margin = 2.0 * sd - spread
    return {"premium_std_bps": round(sd, 2),
            "spread_sum_bps": round(spread, 2),
            "margin_bps": round(margin, 2),
            "reaches_both": bool(margin > 0)}


def side_metric(g: pd.DataFrame, edge_col: str, depth_cols) -> dict:
    pos = g[g[edge_col] > 0]
    span_days = max((g.ts.max() - g.ts.min()) / 86400, 1e-9)
    if len(pos) < 10:
        return {"band_bps": None, "fires_per_day": 0.0, "depth_usd": None,
                "capturable_usd_per_day": 0.0}
    band = float(pos[edge_col].quantile(0.9))
    fat = g[g[edge_col] >= band]
    depth = float(fat[list(depth_cols)].min(axis=1).median()) if len(fat) else 0.0
    fires_per_day = len(fat) / span_days
    return {"band_bps": round(band, 3),
            "fires_per_day": round(fires_per_day, 1),
            "depth_usd": round(depth, 0),
            "capturable_usd_per_day": round(fires_per_day * band / 1e4 * depth, 2)}


def main() -> int:
    df = load()
    now = datetime.now(timezone.utc)
    print("§0.75b 掃描器排名（指標 2026-08-30 凍結：可捕獲美元／天 = 次數／天 × 帶寬 × 深度）")
    if df.empty:
        print("  尚無掃描資料")
        return 1
    span = (df.ts.max() - df.ts.min()) / 86400
    print(f"  掃描 {len(df):,} 筆報價｜{df.pair.nunique()} 個配對｜跨度 {span:.2f} 天"
          f"（名單門檻 ≥{MIN_SPAN_DAYS:.0f} 天、每配對 ≥{MIN_SAMPLES} 筆）")
    rows = []
    for pair, g in df.groupby("pair"):
        listed_h = None
        ca = g.b_created_at.dropna()
        if len(ca):
            try:
                listed_h = (now.timestamp() * 1000 - float(ca.iloc[-1])) / 3.6e6
            except Exception:
                listed_h = None
        sell = side_metric(g, "sell_edge_bps", ("a_bid_usd", "b_ask_usd"))
        buy = side_metric(g, "buy_edge_bps", ("b_bid_usd", "a_ask_usd"))
        best = max(sell, buy, key=lambda s: s["capturable_usd_per_day"])
        ts = two_sided(g)
        rows.append({"pair": pair, "n": int(len(g)),
                     "leg_b": g.leg_b.iloc[0], "listed_h": listed_h,
                     "sell": sell, "buy": buy, **ts,
                     "capturable_usd_per_day": best["capturable_usd_per_day"],
                     "band_bps": best["band_bps"], "depth_usd": best["depth_usd"],
                     "fires_per_day": best["fires_per_day"]})
    tab = pd.DataFrame(rows).sort_values("capturable_usd_per_day", ascending=False)

    ctrl = tab[tab.pair.str.startswith("BTC@")]
    ctrl_band = float(ctrl.band_bps.dropna().max()) if len(ctrl) else None
    print("\n  對照組（BTC，帶＝儀器的雜訊底）：")
    for _, r in ctrl.iterrows():
        print(f"    {r.pair:22s} band {r.band_bps} bps  depth ${r.depth_usd}  "
              f"≈ ${r.capturable_usd_per_day}/天")

    eligible = tab[(tab.n >= MIN_SAMPLES)
                   & ((tab.listed_h.isna()) | (tab.listed_h >= MIN_LISTED_H))
                   & (~tab.pair.str.startswith("BTC@"))]
    if ctrl_band is not None:
        eligible = eligible[eligible.band_bps > CONTROL_MULT * ctrl_band]
    gate_ok = span >= MIN_SPAN_DAYS

    print(f"\n  前 15（{'正式名單' if gate_ok else '期中觀察，跨度未達 — 不出名單'}）：")
    show = (eligible if gate_ok else tab[~tab.pair.str.startswith("BTC@")]).head(15)
    for _, r in show.iterrows():
        reach = ("  " if r.reaches_both is None
                 else ("✓ " if r.reaches_both else "✗ "))
        print(f"    {reach}{r.pair:22s} n={r.n:4d}  band {str(r.band_bps):>7s} bps  "
              f"{r.fires_per_day:6.1f}/天  depth ${str(r.depth_usd):>8s}  "
              f"≈ ${r.capturable_usd_per_day:>8}/天  "
              f"｜擺盪 2σ−價差 {str(r.margin_bps):>7s} bps")
    n_one = int((show.reaches_both == False).sum())          # noqa: E712
    if n_one:
        print()
        print(f"  ✗ = 溢價擺盪碰不到另一邊（2σ < 兩腿價差和）——{n_one} 個。")
        print("    那是恆等式不是門檻：任一瞬間 sell_edge + buy_edge = −價差和，")
        print("    所以進得去出不來。**這一欄只報告，不參與排序與升格**")
        print("    （指標 2026-08-30 凍結）。要讓它擋住升格請明講。")
    promote = eligible.head(PROMOTE_N).pair.tolist() if gate_ok else []
    if gate_ok:
        print(f"\n  升格候選（前 {PROMOTE_N}）：{promote or '無'}")
        print("  升格後從該配對自己的首列起算 7 天；掃描期資料不進判決。")
    out = {"asof_utc": now.strftime("%Y-%m-%d %H:%M"), "span_days": round(span, 2),
           "quotes": int(len(df)), "pairs": int(df.pair.nunique()),
           # Published so the board can say "this row has not met the sample
           # threshold" per pair. `top` is the whole ranking, unfiltered — the
           # sample gate applies to `eligible`, and a reader looking at one row
           # cannot tell the difference without this number.
           "min_samples": MIN_SAMPLES,
           "gate_ok": gate_ok, "control_band_bps": ctrl_band,
           # 2026-09-05: reported alongside the frozen metric, never inside
           # it. `reaches_both` false = the premium's oscillation cannot
           # cross both spreads, so the pair is one-sided by arithmetic and a
           # 7-day recording clock spent on it can only ever measure one leg.
           "two_sided_note": "reaches_both/margin_bps are DIAGNOSTIC; the "
                             "ranking and promotion are the 2026-08-30 metric",
           "one_sided_pairs": sorted(
               tab.loc[tab.reaches_both == False, "pair"].tolist()),  # noqa: E712
           "promote": promote,
           # `top` = the ranking exactly as the frozen metric orders it, kept
           # whole so the ordering stays auditable. `top_sampled` = the same
           # ranking restricted to pairs that have met the sample threshold,
           # which is what a board should show: after the CEX legs joined, a
           # 14-quote pair with a deep book outranked 2,700-quote pairs and
           # filled 19 of the 20 published rows with noise. Filtering the
           # DISPLAY is not filtering the metric — promotion still reads
           # `eligible`, and `pending_pairs` says how many are still counting.
           "pending_pairs": int((tab.n < MIN_SAMPLES).sum()),
           "top": tab.head(30).drop(columns=["sell", "buy"]).to_dict("records"),
           "top_sampled": (tab[tab.n >= MIN_SAMPLES].head(30)
                           .drop(columns=["sell", "buy"]).to_dict("records"))}
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str),
                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
