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

    G1 淨值   **淨邊際 > 3.0 bps**（2026-09-14 改，見下）
                 淨 = 掛單腿半價差 - 對沖腿半價差 - 費用(0.40+4.50)
    G2 兩側   少數側的報價佔比 >= 20%
              來源 = live 的 maker.csv（2026-09-14 前是 shadow.csv）
    G3 流量   掛單腿有成交的分鐘佔比 >= 30%
    G4 切片   **中位成交切片 >= 對沖腿最小單（$10）**（2026-09-14 新增）
    G5 雙向   **吃單流少數側 >= 35%**（2026-09-14 新增）

五關都過才是候選。**不挑格、不事後換門檻。**

===========================================================================
2026-09-14：G1 改了，而且是三次實盤打出來的
===========================================================================
舊的 G1 是「半價差 >= 5.0 bps」，理由寫著「扣掉 4.9 bps 的來回費就沒東西」。
**那句話漏了對沖腿自己的半價差**：我們是吃單過去的，要穿過 HL 的價差。

    真正的損益平衡 = HL 半價差(~2.5) + 0.40 + 4.50 = **7.4 bps**

所以**舊的 G1 門檻本身就低於損益平衡線**，它放行了一整類必虧的市場。
FIL 半價差 7.86 -> 舊 G1 過，實盤跑一小時只報價 2 次、0 成交，
因為淨邊際只有 +0.46 bps，引擎自己的 `maker_min_edge_bps` 把它擋掉了。

G4 / G5 也是實盤打出來的，而且它們解釋了 MET 與 CHIP：

    MET  中位成交切片 $0.27（對沖腿最小單 $10）-> 97% 的成交對沖不掉
         吃單流 93.1% 是買方 -> 實盤 121 筆成交**全部是 SELL**，
         買單掛了 82 次一次都沒成交
    CHIP 切片 $0.03、吃單流 97.4% 單向 —— 比 MET 更極端

**G3 對這兩件事是瞎的**：它數「有成交的分鐘」，而灰塵流每分鐘都有成交。
一個每分鐘成交三十次、每次 $0.27 的市場，G3 滿分。

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
# 2026-09-14：G1 從「半價差 >= 5」改成「淨值 > 3」。舊門檻低於損益平衡線
# （7.4 bps），系統性放行必虧的市場 —— 見檔頭。
HL_HALF_SPREAD_BPS = 2.5      # 對沖腿的半價差,我們吃單要穿過它
FEES_BPS = 0.40 + 4.50        # Lighter maker + HL taker
G1_NET_BPS = 3.0              # 扣完之後還要剩這麼多才值得做
G2_MINORITY_PCT = 20.0
G3_FLOW_PCT = 30.0
G4_MIN_SLICE_USD = 10.0       # 對沖腿最小單；切片比它小就對沖不掉
G5_TAKER_MINORITY_PCT = 35.0  # 吃單流的少數側；單向流只會打到我們一側

# G4 / G5 的資料來源：flow_system 錄的 Lighter 逐筆成交帶。
# **讀不到就印「未量」,不是通過** —— 未知不可以長得像已知
# （mistake.md 2026-09-13）。
TAPE_DIR = os.path.join("D:", os.sep, "flowbot_data", "lighter", "trades")

# 錄製器的腿身分（從 config_*.yaml 讀出來的，寫死避免每次解析 YAML）
# entropy=hl 的那些：hedge 腿是 Lighter；GOLD_LL / NVDA_LL 兩腿都在 Lighter。
BOTH_LIGHTER = {"GOLD_LL", "NVDA_LL"}


def tape_stats(pair):
    """從 Lighter 逐筆成交帶量 G4（切片）與 G5（吃單流雙向）。

    `is_maker_ask=True` = 掛單方在賣側 = **吃單方在買**。我們掛的買單只有在
    對面有人主動賣時才會成交,所以吃單流單向 = 一側的掛單永遠不會成交
    （MET 實盤：吃單買 93.1%,買單掛 82 次成交 0 次）。

    讀不到就回 (None, None) —— 呼叫端印「未量」,不是通過。
    """
    import glob
    try:
        import pandas as pd
    except ImportError:
        return None, None
    fs = sorted(glob.glob(os.path.join(TAPE_DIR, "*", "*.parquet")))
    if not fs:
        return None, None
    try:
        df = pd.concat([pd.read_parquet(f, columns=["coin", "usd", "is_maker_ask"])
                        for f in fs[-24:]], ignore_index=True)
    except Exception:
        return None, None
    d = df[df["coin"] == pair]
    if len(d) < 100:            # 樣本太少就不要假裝量得出來
        return None, None
    slice_med = float(d["usd"].median())
    buy = float(d["is_maker_ask"].mean()) * 100.0
    return slice_med, min(buy, 100.0 - buy)


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
    # **2026-09-14：來源從 shadow.csv 換成 live 的 maker.csv。**
    # shadow 退場了,理由是雙重的而且都是量出來的：
    #   (a) 結構上量不到它該量的東西 —— shadow 的 quotes_rested 恆為 0
    #       （maker_rested 要交易所回一個 OPEN 狀態）,所以 M2 的分母永遠是零、
    #       M3 沒有成交可記、M4 沒有真實往返。跑十小時的乾淨 shadow 對
    #       「live 起不起得來」零資訊量（而那天 live 起不來：HL 的 SDK 沒裝）。
    #   (b) **它同時是 WAF 預算的主要消耗者** —— `_http_keepalive_loop` 只在
    #       `not record_only` 時起,所以 shadow 每 10 秒打兩腿而 record-only
    #       不打。七支 shadow = 84 req/分,結果是每十分鐘八支引擎一起被擋 60 秒,
    #       而被擋的是**重連**,那時會下單的那支手上有部位。
    #
    # 問題沒變（「這個市場讓不讓引擎兩側都報價」）,只是換成 live 的紀錄回答。
    # maker.csv 每一張真的送出去的報價都有 side,所以它跟 shadow.csv 一樣
    # 「不可能跟引擎不一致」,而且量的是真的發生過的事。
    # **代價**：G2 要先跑一輪 live。沒跑過就是 None —— 而 None 不是 0 也不是 PASS。
    #
    # 順帶記一個實盤打出來的校準：MET 的 shadow 決策側別是 18.8% 少數側,
    # 而 **live 的 121 筆成交全部是 SELL**。所以連 shadow 的決策側別都比
    # 真相樂觀 —— 真正對得上的是吃單流（G5,MET 6.9%）。
    # **輪替過的世代也要一起讀**（理由在 `arblib/maker_log.py`）:引擎在表頭
    # 變動時會把舊檔搬成 `.old` 再開新檔,而這裡的門檻是 `len(sd) >= 100` ——
    # 只讀現行檔的話,**任何一次加欄位都會讓 G2 在那之後安靜地退回「未量」**,
    # 而「未量」跟「這個市場還沒跑過」在版面上長得一模一樣。
    from .maker_log import maker_log_paths
    sd = []
    for sp in maker_log_paths(os.path.join(LOGS, pair)):
        sd += [x for x in csv.DictReader(io.open(sp, encoding="utf-8"))
               if x.get("side") in ("SELL", "BUY")]
    sell_ok = buy_ok = None
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

    # G1：**淨值**,不是半價差。我們吃單對沖要穿過對沖腿的價差,
    # 所以損益平衡是 HL 半價差 + 費用 = 7.4 bps,而舊門檻寫 5.0。
    half = st.median(hs) if hs else float("nan")
    net = (half - HL_HALF_SPREAD_BPS - FEES_BPS) if hs else float("nan")
    slice_med, taker_minority = tape_stats(pair)

    g1 = bool(hs) and net > G1_NET_BPS
    g2 = (minority is not None) and minority >= G2_MINORITY_PCT
    g3 = flow_pct >= G3_FLOW_PCT
    # **未量 != 通過**（mistake.md 2026-09-13：未知不可以長得像已知）
    g4 = (slice_med is not None) and slice_med >= G4_MIN_SLICE_USD
    g5 = (taker_minority is not None) and taker_minority >= G5_TAKER_MINORITY_PCT
    return dict(pair=pair, n=n,
                half_spread=half, net_bps=net,
                sell_pct=sell_ok, buy_pct=buy_ok,
                minority=minority, prem_med=st.median(prem) if prem else 0.0,
                balance=bal, cross_per_day=cross_per_day,
                flow_pct=flow_pct, flow_med=flow_med,
                slice_med=slice_med, taker_minority=taker_minority,
                g1=g1, g2=g2, g3=g3, g4=g4, g5=g5,
                passed=(g1 and g2 and g3 and g4 and g5))


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

    print("HMM 標的篩選｜掛單腿 = hedge｜判準凍結（2026-09-14 改版）：")
    print("  G1 淨值>%.1fbps（半價差 − HL %.1f − 費用 %.1f）  G2 少數側>=%.0f%%"
          "  G3 有成交分鐘>=%.0f%%  G4 切片>=$%.0f  G5 吃單流少數側>=%.0f%%"
          % (G1_NET_BPS, HL_HALF_SPREAD_BPS, FEES_BPS, G2_MINORITY_PCT,
             G3_FLOW_PCT, G4_MIN_SLICE_USD, G5_TAKER_MINORITY_PCT))
    print()
    print("%-9s %5s %8s %8s %7s %7s %9s %9s   %s"
          % ("配對", "分鐘", "半價差", "淨bps", "少數側%", "有成交%",
             "切片中位$", "吃單少數%", "判定"))
    print("-" * 92)
    for r in sorted(out, key=lambda x: (-x["passed"],
                                       -(x["minority"] if x["minority"]
                                         is not None else -1))):
        mark = "".join(["1" if r["g1"] else "-", "2" if r["g2"] else "-",
                        "3" if r["g3"] else "-", "4" if r["g4"] else "-",
                        "5" if r["g5"] else "-"])
        note = ""
        if r["pair"] == "BTC":
            note = "  <- 控制組"
        if r["pair"] == "GMX":
            note = "  <- 現行標的"
        if r["pair"] in BOTH_LIGHTER:
            note += "（兩腿都 Lighter）"
        fmt = lambda v, w=7: (("%%%d.1f" % w) % v) if v is not None             else ("%%%ds" % w) % "未量"
        fmt2 = lambda v: ("%9.2f" % v) if v is not None else "     未量"
        print("%-9s %5d %8.2f %8.2f %s %7.1f %s %s   %s%s"
              % (r["pair"], r["n"], r["half_spread"], r["net_bps"],
                 fmt(r["minority"]), r["flow_pct"], fmt2(r["slice_med"]),
                 fmt(r["taker_minority"], 9),
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
