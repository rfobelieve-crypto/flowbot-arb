# -*- coding: utf-8 -*-
"""HMM 標的篩選：寬價差 ∧ **premium 會震盪** ∧ 有人來吃。

    python arblib/hmm_screen.py

===========================================================================
為什麼要有這一支（2026-09-14）
===========================================================================
§1.40 選 GMX 的判準是「毛邊際夠寬」。實跑 shadow 之後發現那個判準少了一條，
而缺的那條決定了這條線賺不賺得到錢：

    價差夠寬              GMX ✓   105 bps
    有人來吃              GMX ？  日成交額 $9,534
    **premium 會兩邊跑**   GMX ✗   173 分鐘裡 96% 為負，中位 −38.9 bps

HMM 是做市不是套利 —— 它靠**兩側輪流成交**把庫存做掉。premium 持續偏在
一邊的市場，我們只能一直賣、賣到 `max_position_usd` 滿（$60/腿 ÷ $15 =
4 張），然後卡住等 premium 翻過來（實測 4% 的分鐘）。
**於是「每張 $0.143」變成「一次性 $0.57」而不是每日收益。**

所以這支問的是一個 §1.40 沒問的問題：**這個市場讓不讓我們循環。**

===========================================================================
判準（跑之前寫死，事後不放寬）
===========================================================================
用**掛單腿**（HMM 的 `maker_venue: hedge`，也就是 Lighter 那側）：

    G1 價差   掛單腿半價差中位 >= 5.0 bps
              低於這個，扣掉 4.9 bps 的來回費就沒東西了
    G2 兩側   少數側可做的分鐘佔比 >= 20%
              「可做」= 該側的 edge_mean_bps > 0。20% = 一天約 5 小時
              可以做另一邊，庫存才轉得動
    G3 流量   掛單腿有成交的分鐘佔比 >= 30%
              簿口深但沒人交易 = 對做市是餓死（CLAUDE.md §HFT 的原話）

三關都過才是候選。**不挑格、不事後換門檻。**

===========================================================================
對照組（已知答案；它們不對就是這支儀器壞了，不是市場有事）
===========================================================================
    BTC   兩個深簿、帶應該 ~0  ->  半價差要很小、premium 要**平衡**
    GMX   已經量過 96% 為負    ->  G2 必須**不過**

`factor-research.md` 最後一條：自己剛寫的儀器要先在答案已知的資料上跑一次。
而 BTC 這個控制配對在這條線上已經抓到過兩次儀器錯誤（mistake.md 2026-09-03、
2026-09-13），所以它一直留在表上。

===========================================================================
這支**不**回答什麼
===========================================================================
成交率（M2）與逆選擇（M3）離線量不到 —— 那是第 9 次 override 要買的東西。
這支只回答「哪個市場的**結構**讓 HMM 循環得起來」，不預測損益。
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(os.path.dirname(HERE), "engine", "logs")

# 判準（凍結）
G1_HALF_SPREAD_BPS = 5.0
G2_MINORITY_PCT = 20.0
G3_FLOW_PCT = 30.0

# 錄製器的腿身分（從 config_*.yaml 讀出來的，寫死避免每次解析 YAML）
# entropy=hl 的那些：hedge 腿是 Lighter；GOLD_LL / NVDA_LL 兩腿都在 Lighter。
BOTH_LIGHTER = {"GOLD_LL", "NVDA_LL"}


def f(row, key):
    v = row.get(key)
    if v in (None, "", "nan", "NaN"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load(pair):
    p = (os.path.join(LOGS, "minutes.csv") if pair == "SNDK"
         else os.path.join(LOGS, pair, "minutes.csv"))
    if not os.path.exists(p):
        return []
    return list(csv.DictReader(io.open(p, encoding="utf-8")))


def screen(pair):
    rows = load(pair)
    if len(rows) < 60:
        return None
    n = len(rows)

    # 掛單腿 = hedge（HMM 的 maker_venue）。半價差從它的頂檔算。
    hs = []
    for r in rows:
        b, a = f(r, "hedge_bid"), f(r, "hedge_ask")
        if b and a and a > b > 0:
            hs.append((a - b) / ((a + b) / 2.0) * 1e4 / 2.0)

    # 兩側可做性：引擎自己逐分鐘算的邊際，直接用，不重算
    # （第二份實作會安靜地不同意 —— mistake.md 2026-08-26）
    sell_ok = sum(1 for r in rows if (f(r, "sell_edge_mean_bps") or -1) > 0)
    buy_ok = sum(1 for r in rows if (f(r, "buy_edge_mean_bps") or -1) > 0)
    minority = min(sell_ok, buy_ok) / n * 100.0

    # premium 的符號平衡與翻轉次數（獨立於上面那個，當交叉對照）
    prem = [f(r, "premium_mean_bps") for r in rows]
    prem = [x for x in prem if x is not None]
    pos = sum(1 for x in prem if x > 0)
    bal = min(pos, len(prem) - pos) / len(prem) * 100.0 if prem else 0.0
    cross = sum(1 for a, b in zip(prem, prem[1:]) if (a > 0) != (b > 0))
    cross_per_day = cross / (n / 1440.0)

    # 掛單腿的吃單流量：有成交的分鐘，以及中位成交額
    flow = [(f(r, "h_buy_usd") or 0.0) + (f(r, "h_sell_usd") or 0.0)
            for r in rows]
    flow_pct = sum(1 for x in flow if x > 0) / n * 100.0
    flow_med = st.median([x for x in flow if x > 0]) if any(flow) else 0.0

    g1 = st.median(hs) >= G1_HALF_SPREAD_BPS if hs else False
    g2 = minority >= G2_MINORITY_PCT
    g3 = flow_pct >= G3_FLOW_PCT
    return dict(pair=pair, n=n,
                half_spread=st.median(hs) if hs else float("nan"),
                sell_pct=sell_ok / n * 100.0, buy_pct=buy_ok / n * 100.0,
                minority=minority, prem_med=st.median(prem) if prem else 0.0,
                balance=bal, cross_per_day=cross_per_day,
                flow_pct=flow_pct, flow_med=flow_med,
                g1=g1, g2=g2, g3=g3, passed=(g1 and g2 and g3))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="")
    a = ap.parse_args(argv)

    pairs = ([p.strip() for p in a.pairs.split(",") if p.strip()] or
             ["SNDK", "NBIS", "ANTH", "BTC", "ZEC", "NEAR", "HYPE",
              "GOLD_LL", "NVDA_LL", "GMX"])
    out = [r for r in (screen(p) for p in pairs) if r]
    if not out:
        raise RuntimeError("一個配對都讀不到 —— 空輸出在這裡不是合法狀態")

    print("HMM 標的篩選｜掛單腿 = hedge｜判準凍結："
          "G1 半價差>=%.1fbps  G2 少數側>=%.0f%%  G3 有成交分鐘>=%.0f%%"
          % (G1_HALF_SPREAD_BPS, G2_MINORITY_PCT, G3_FLOW_PCT))
    print()
    print("%-9s %5s  %8s %6s %6s %7s  %8s %7s  %7s %9s   %s"
          % ("配對", "分鐘", "半價差", "賣側%", "買側%", "少數側%",
             "prem中位", "翻轉/日", "有成交%", "成交$/分", "判定"))
    print("-" * 108)
    for r in sorted(out, key=lambda x: (-x["passed"], -x["minority"])):
        mark = "".join(["1" if r["g1"] else "-", "2" if r["g2"] else "-",
                        "3" if r["g3"] else "-"])
        note = ""
        if r["pair"] == "BTC":
            note = "  <- 控制組"
        if r["pair"] == "GMX":
            note = "  <- 現行標的"
        if r["pair"] in BOTH_LIGHTER:
            note += "（兩腿都 Lighter）"
        print("%-9s %5d  %8.2f %6.1f %6.1f %7.1f  %8.1f %7.1f  %7.1f %9.0f   %s%s"
              % (r["pair"], r["n"], r["half_spread"], r["sell_pct"],
                 r["buy_pct"], r["minority"], r["prem_med"],
                 r["cross_per_day"], r["flow_pct"], r["flow_med"],
                 "PASS " + mark if r["passed"] else "過 " + mark, note))

    # ---- 對照組：已知答案。不對就是這支壞了 ----
    print()
    print("=== 自曝檢查（答案已知，紅了先查這支不是查市場）===")
    btc = next((r for r in out if r["pair"] == "BTC"), None)
    gmx = next((r for r in out if r["pair"] == "GMX"), None)
    ok = True
    if btc:
        c = btc["half_spread"] < 2.0
        ok &= c
        print("  C1 BTC 半價差 %.2f bps < 2.0（兩個深簿）        %s"
              % (btc["half_spread"], "PASS" if c else "**FAIL**"))
        c = btc["balance"] > 15.0
        ok &= c
        print("  C2 BTC premium 平衡度 %.1f%% > 15%%（帶應該 ~0） %s"
              % (btc["balance"], "PASS" if c else "**FAIL**"))
    if gmx:
        c = not gmx["g2"]
        ok &= c
        print("  C3 GMX 必須**過不了** G2（已量過 96%% 單邊）     %s"
              % ("PASS" if c else "**FAIL** —— 跟先前的量測矛盾"))
    print("  -> %s" % ("儀器可信" if ok else "**先修這支，不要解讀上面的表**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
