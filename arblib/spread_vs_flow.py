# -*- coding: utf-8 -*-
"""整個 Lighter 宇宙：價差 vs 流量，有沒有市場兩樣都有。

2026-09-15。這支回答 HMM 這條線的生死問題，而它離線就算得出來。

===========================================================================
為什麼要問
===========================================================================
`hmm_screen` 的 G1 要**寬價差**（淨值 > 3 bps），G6 要**厚流量**
（少數側 >= 20 筆/小時）。已知的兩端：

    MON  淨值 +3.36 bps  流量 **3.4 筆/小時**   -> 實盤 4.4 小時 3 筆成交
    BTC  淨值 −7.01 bps  流量 **7368 筆/小時**  -> 有人但沒錢賺

如果這兩件事在 Lighter 上**結構性地互斥**，那 HMM 就不是參數問題，
而 M2 那條「成交率 <10% 這條路關掉」就有了機制解釋，不是運氣不好。
11 個標的看起來是互斥的，但 11 個不是判決 —— 這支算全部。

===========================================================================
怎麼算
===========================================================================
價差來自 `lighter/tob`（逐秒頂檔，193 幣），流量來自 `lighter/trades`
（逐筆，帶 `is_maker_ask` 與 `usd`）。兩個都是常駐 WS 錄製器，
**同一瞬間讀同一個公開簿口** —— 不是掃描器那種序列抓取
（mistake.md 2026-09-13：那個會造出 94% 是儀器的假價差）。

判準沿用 `hmm_screen` 的常數，**不在這裡另立一套**（第二份實作會安靜地
跟做決定的那個不一致）。

**這支不預測損益**，只回答「結構上有沒有」。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hmm_screen import (G1_NET_BPS, G4_MIN_SLICE_USD,      # noqa: E402
                        G6_MINORITY_FLOW_PER_H,
                        HL_HALF_SPREAD_BPS, FEES_BPS)

TOB = "D:/flowbot_data/lighter/tob"
TAPE = "D:/flowbot_data/lighter/trades"


def _read(root: str, cols: list, hours: int) -> pd.DataFrame:
    fs = sorted(glob.glob(os.path.join(root, "*", "*.parquet")))
    if not fs:
        sys.exit("讀不到 %s —— 空結果不是合法狀態（D 槽掛載了嗎）" % root)
    return pd.concat([pd.read_parquet(f, columns=cols) for f in fs[-hours:]],
                     ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=12)
    a = ap.parse_args()

    tb = _read(TOB, ["coin", "bid", "ask"], a.hours)
    tb = tb[(tb["ask"] > tb["bid"]) & (tb["bid"] > 0)]
    tb["half"] = (tb["ask"] - tb["bid"]) / ((tb["ask"] + tb["bid"]) / 2) * 1e4 / 2
    half = tb.groupby("coin")["half"].median()
    nobs = tb.groupby("coin").size()

    tp = _read(TAPE, ["ts", "coin", "usd", "is_maker_ask"], a.hours)
    big = tp[tp["usd"] >= G4_MIN_SLICE_USD]
    span_h = (big["ts"].max() - big["ts"].min()) / 3.6e6
    gb = big.groupby("coin")["is_maker_ask"]
    n_ask = gb.apply(lambda s: int(s.astype(bool).sum()))
    n_all = big.groupby("coin").size()
    flow = (pd.concat([n_ask, n_all - n_ask], axis=1).min(axis=1) / span_h)

    d = pd.DataFrame({"half": half, "nobs": nobs}).join(
        flow.rename("flow"), how="left")
    d["flow"] = d["flow"].fillna(0.0)
    d = d[d["nobs"] >= 200]                 # 頂檔樣本太少的不談
    d["net"] = d["half"] - HL_HALF_SPREAD_BPS - FEES_BPS
    d["g1"] = d["net"] > G1_NET_BPS
    d["g6"] = d["flow"] >= G6_MINORITY_FLOW_PER_H

    print("=== Lighter 宇宙：價差 vs 流量（%.1f 小時，%d 個市場）==="
          % (span_h, len(d)))
    print("  G1 淨值 > %.1f bps      G6 少數側 >= %.0f 筆/小時（切片>=$%.0f）"
          % (G1_NET_BPS, G6_MINORITY_FLOW_PER_H, G4_MIN_SLICE_USD))
    print()
    both = d[d["g1"] & d["g6"]]
    print("  %-22s %4d 個" % ("G1 過（價差夠寬）", int(d["g1"].sum())))
    print("  %-22s %4d 個" % ("G6 過（流量夠厚）", int(d["g6"].sum())))
    print("  %-22s **%d 個**" % ("兩個都過", len(both)))
    if len(both):
        b = both.sort_values("flow", ascending=False).head(12)
        print()
        print("  %-12s %9s %9s %11s" % ("市場", "半價差", "淨bps", "少數側/時"))
        for c, r in b.iterrows():
            print("  %-12s %9.2f %9.2f %11.1f" % (c, r["half"], r["net"],
                                                  r["flow"]))

    # 互斥有多強：把流量分五層，看每層的淨值
    print()
    print("=== 流量分層 vs 淨值（互斥有多強）===")
    d2 = d[d["flow"] > 0].copy()
    if len(d2) >= 20:
        d2["q"] = pd.qcut(d2["flow"], 5, labels=["最少", "少", "中", "多",
                                                 "最多"], duplicates="drop")
        g = d2.groupby("q", observed=True).agg(
            n=("net", "size"), 流量中位=("flow", "median"),
            淨值中位=("net", "median"), G1過=("g1", "sum"))
        print(g.round(2).to_string())
        lo = d2[d2["q"] == "最少"]["net"].median()
        hi = d2[d2["q"] == "最多"]["net"].median()
        print()
        print("  流量最少那層淨值中位 %+.2f bps，最多那層 %+.2f bps"
              % (lo, hi))
        print("  -> %s" % ("**單調互斥**：越有人交易的市場，價差越薄到做不了"
                           if lo > hi else
                           "不是單調的 —— 那上面「互斥」的說法要收回"))

    print()
    print("=== 自曝檢查 ===")
    bad = []
    btc = d.loc["BTC"] if "BTC" in d.index else None
    if btc is not None:
        c = btc["half"] < 2.0 and btc["flow"] > 100
        bad += [] if c else ["C1"]
        print("  C1 BTC 必須「價差極小 ∧ 流量極大」  %s  %.2f bps / %.0f 筆/時"
              % ("PASS" if c else "**FAIL**", btc["half"], btc["flow"]))
    mon = d.loc["MON"] if "MON" in d.index else None
    if mon is not None:
        c = mon["flow"] < G6_MINORITY_FLOW_PER_H
        bad += [] if c else ["C2"]
        print("  C2 MON 必須過不了 G6（實盤 4.4h 3 筆）%s  %.1f 筆/時"
              % ("PASS" if c else "**FAIL**", mon["flow"]))
    c = span_h > 1.0
    bad += [] if c else ["C3"]
    print("  C3 時窗要夠長                          %s  %.1f 小時"
          % ("PASS" if c else "**FAIL**", span_h))
    print()
    print("  %s" % ("全過" if not bad else "**%s 沒過，數字不可引用**"
                    % ",".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
