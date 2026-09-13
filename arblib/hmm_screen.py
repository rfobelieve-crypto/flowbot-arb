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

import yaml

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


def _band(pair):
    """該配對自己的 thresholds（引擎的閘門）。讀設定，不寫死 ——
    寫死就是第二份實作，而它會安靜地跟真正做決定的那個不一致。"""
    for nm in ("config_%s.yaml" % pair, "config_HMM_%s.yaml" % pair):
        p = os.path.join(os.path.dirname(LOGS), nm)
        if os.path.exists(p):
            t = (yaml.safe_load(io.open(p, encoding="utf-8")) or {}
                 ).get("thresholds") or {}
            return (float(t.get("upper_bps", 0.0)),
                    float(t.get("lower_bps", 0.0)))
    return 0.0, 0.0          # SNDK 用預設設定檔，沒有自己的一份


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

    # 兩側可做性。
    #
    # **第一版用了 recorder 的 sell_edge_mean_bps / buy_edge_mean_bps，
    # 而那是錯的構造**（2026-09-14 改正）。recorder.py:13 的定義是
    #     sell_edge = (entropy_bid / hedge_ask - 1) * 1e4
    # —— **兩條腿都穿價差**，那是 §0.75 的吃單–吃單構造。
    # HMM 是掛單–吃單：在掛單腿的 ask 掛賣、到吃單腿的 ask 吃單對沖。
    # 兩者在一個 91 bps 價差的市場上差了整整一個價差，而錯的那個會把
    # 「幾乎一直可做」報成「0.4% 可做」。
    #
    # 用 recorder 既有欄位本來是對的直覺（第二份實作會安靜地不同意,
    # mistake.md 2026-08-26）—— 但前提是那個欄位量的是同一件事。
    # **「不要重算」不等於「拿現成的那個」**：先問它算的是哪個構造。
    #
    # 手算對過引擎的 log：(7.9012 − 7.8215)/7.8215 = 101.9 − 4.9 = 97.0
    # ≈ 引擎印的 96.95 bps。
    #
    # 門檻（20%）一個字沒動 —— 這是儀器修正，不是閘門鬆綁
    # （mistake.md 2026-09-03 Bitget 單位修正的同一種）。
    # ---- G2：**只用引擎自己的決策，不自己算** ----
    #
    # 我試過兩次自己算，兩次都跟引擎對不上：
    #   v1 用 recorder 的 sell_edge/buy_edge  -> 那是吃單–吃單的構造（錯）
    #   v2 用掛單–吃單 ＋ 設定的 thresholds   -> GMX 報 34% 買側，
    #                                            而引擎實跑是 4,782 賣 : 6 買
    # v2 還差在哪不知道（報價定價、premium_persist、depth、post-only 都可能），
    # **而繼續調到控制組變綠，就是把儀器擬合到我期待的答案**
    # —— 那正是 C3 存在要擋的事（mistake.md 2026-08-26 / 2026-09-09）。
    #
    # 所以 G2 的唯一來源是 `logs/<pair>/shadow.csv`：引擎跑 --shadow 時
    # 每一次報價決策的側別。它不可能跟引擎不一致,因為它就是引擎寫的。
    # 代價是 G2 需要**先跑一輪 shadow**（GMX 70 分鐘給了 4,788 次決策,
    # 一小時綽綽有餘）。沒跑過就是 None,而 None 不是 0 也不是 PASS。
    sp = os.path.join(LOGS, pair, "shadow.csv")
    sell_ok = buy_ok = None
    if os.path.exists(sp):
        sd = [x for x in csv.DictReader(io.open(sp, encoding="utf-8"))
              if x.get("action") == "quote"]
        if len(sd) >= 100:
            sell_ok = sum(1 for x in sd if x.get("side") == "SELL")
            buy_ok = sum(1 for x in sd if x.get("side") == "BUY")
    if sell_ok is None:
        minority = None
    else:
        tot = max(sell_ok + buy_ok, 1)
        sell_ok = sell_ok / tot * 100.0
        buy_ok = buy_ok / tot * 100.0
        minority = min(sell_ok, buy_ok)

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
    g2 = (minority is not None) and minority >= G2_MINORITY_PCT
    g3 = flow_pct >= G3_FLOW_PCT
    return dict(pair=pair, n=n,
                half_spread=st.median(hs) if hs else float("nan"),
                sell_pct=sell_ok, buy_pct=buy_ok,
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
    for r in sorted(out, key=lambda x: (-x["passed"],
                                       -(x["minority"] if x["minority"]
                                         is not None else -1))):
        mark = "".join(["1" if r["g1"] else "-", "2" if r["g2"] else "-",
                        "3" if r["g3"] else "-"])
        note = ""
        if r["pair"] == "BTC":
            note = "  <- 控制組"
        if r["pair"] == "GMX":
            note = "  <- 現行標的"
        if r["pair"] in BOTH_LIGHTER:
            note += "（兩腿都 Lighter）"
        fmt = lambda v: ("%6.1f" % v) if v is not None else "   未量"
        print("%-9s %5d  %8.2f %s %s %s  %8.1f %7.1f  %7.1f %9.0f   %s%s"
              % (r["pair"], r["n"], r["half_spread"], fmt(r["sell_pct"]),
                 fmt(r["buy_pct"]), fmt(r["minority"]), r["prem_med"],
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
