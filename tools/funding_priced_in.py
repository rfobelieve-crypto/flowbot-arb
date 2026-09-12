# -*- coding: utf-8 -*-
"""跨場館資金費差：**它被定價到基差裡了嗎**（A6 的生死題）

來源：Small Trader Alpha #4 - Funding Arbitrage（2024-01-30，付費）的
「When Is Cross-Exchange Funding Priced-In?」一節，作者原話：

    「當一條腿的資金費結算時，基差會劇烈地往你不利的方向擺動，因為大家
      在出場，造成的損失抵掉你賺到的任何利潤。……不是每個機會都像它看
      起來的那樣。**尤其是當它容量很大而市場相對平靜的時候。**」

也就是說：`fund_diff_bps8h` 看起來是白拿的錢，但你可能在基差上吐回去。
這支只回答這一題，**不判決任何東西**。

── 為什麼不用「結算時點」對齊 ────────────────────────────────────────
各所的資金費間隔不同（HL 每小時、多數 CEX 每 8 小時），而
`fund_diff_bps8h` 已經正規化成 8h，原始間隔被藏起來了。所以改問一個
不需要時點的等價問題：**持有 8 小時，收到的資金費有沒有被 premium 的
漂移吃掉。**

── 構造（事前寫死）────────────────────────────────────────────────
方向：d = sign(fund_diff_bps8h)。diff > 0 代表 entropy 腿的資金費較高
      -> **做空 entropy／做多 hedge**，而 premium 定義是 entropy vs hedge，
      所以這個部位在 **premium 下跌**時賺錢。
資金費收入（8h）  = |fund_diff_bps8h|
基差損益（8h）    = −d × (premium(t+480) − premium(t))
淨                = 兩者相加

**不重疊窗**：每 480 分鐘取一格（mistake.md 2026-09-11：掃 horizon 用
不重疊區間，累積／重疊會讓相鄰觀測互相污染）。

── 自曝關 ─────────────────────────────────────────────────────────
D1 **對照組**：GOLD_LL 與 NVDA_LL 兩腿都在 Lighter，資金費必然相同
   -> fund_diff 必須 ≈ 0。**但「它們的淨值是 0」不算檢查** —— fund_diff=0
   時 d=0，basis 機械地等於 0。所以 D1 改成：對同場館配對**強制 d=+1**，
   看 8h 的 premium 漂移有沒有系統性偏離零。偏離了就代表 basis 那一項
   量到的不是「對碰carry不利的漂移」而是某種趨勢。
D2 資金費收入必須恆 ≥ 0（它是絕對值），若出現負值就是程式錯。
D3 全格報告，不挑配對。

跑法：python tools/funding_priced_in.py
"""
from __future__ import annotations

import csv
import glob
import os
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "..", "engine", "logs")
H = 480                      # 持有 8 小時
BOTH_LIGHTER = {"GOLD_LL", "NVDA_LL"}


def load(pair):
    """把該配對所有 schema 版本的分鐘列接起來，只留有 premium+funding 的。"""
    rows = []
    for f in sorted(glob.glob(os.path.join(LOGS, pair, "minutes.csv*"))):
        with open(f, newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            if not rd.fieldnames:
                continue
            if "premium_close_bps" not in rd.fieldnames:
                continue
            if "fund_diff_bps8h" not in rd.fieldnames:
                continue
            for r in rd:
                try:
                    ts = int(r["minute_ts"])
                    pc = float(r["premium_close_bps"])
                    fd = float(r["fund_diff_bps8h"])
                except (TypeError, ValueError, KeyError):
                    continue
                rows.append((ts, pc, fd))
    rows.sort()
    # 去重（同一分鐘只留一筆）
    out, seen = [], set()
    for ts, pc, fd in rows:
        if ts in seen:
            continue
        seen.add(ts)
        out.append((ts, pc, fd))
    return out


def windows(rows):
    """不重疊的 8 小時窗：每 H 列取一格，且要求窗的兩端在時間上真的差 ~8h。

    **時間戳單位自動偵測**（mistake.md 2026-04-12 / 2026-09-07）：
     在這個錄製器裡是**秒**，而同一個專案別處是毫秒。
    硬編碼任何一邊都會讓整支腳本安靜地回 0 個窗 —— 第一版就是這樣。
    """
    if not rows:
        return []
    per_min = 60000.0 if rows[0][0] > 1e12 else 60.0
    out = []
    i = 0
    while i + H < len(rows):
        t0, p0, f0 = rows[i]
        t1, p1, _ = rows[i + H]
        gap_min = (t1 - t0) / per_min
        # 容許排程斷線造成的缺列，但窗不能被拉長太多
        if 400 <= gap_min <= 700 and f0 == f0:
            d = 1.0 if f0 > 0 else (-1.0 if f0 < 0 else 0.0)
            fund = abs(f0)
            basis = -d * (p1 - p0)
            out.append((fund, basis, fund + basis, d))
        i += H
    return out


print("構造：持有 8h，資金費收入 = |fund_diff|，基差損益 = −d×Δpremium，淨 = 兩者和")
print("不重疊窗（每 480 分鐘一格）。單位全部是 bps。")
print()
print("%-9s %5s │ %9s │ %9s │ %9s │ %7s" %
      ("配對", "窗數", "資金費", "基差", "淨", "淨>0"))
print("-" * 66)

pool = []
for pair in sorted(os.listdir(LOGS)):
    if not os.path.isdir(os.path.join(LOGS, pair)):
        continue
    rows = load(pair)
    w = windows(rows)
    if not w:
        print("%-9s %5d │ %s" % (pair, 0, "（窗不足）"))
        continue
    fu = [x[0] for x in w]
    ba = [x[1] for x in w]
    ne = [x[2] for x in w]
    tag = pair + (" *" if pair in BOTH_LIGHTER else "")
    print("%-9s %5d │ %+9.3f │ %+9.3f │ %+9.3f │ %6.0f%%"
          % (tag, len(w), st.median(fu), st.median(ba), st.median(ne),
             100.0 * sum(1 for x in ne if x > 0) / len(ne)))
    if pair not in BOTH_LIGHTER:
        pool += w
    assert min(fu) >= 0, "D2 破：資金費收入出現負值"

print("-" * 66)
if pool:
    fu = [x[0] for x in pool]
    ba = [x[1] for x in pool]
    ne = [x[2] for x in pool]
    print("合池（排除兩腿同場館的對照組） n=%d" % len(pool))
    print("  資金費中位 %+.3f bps / 8h" % st.median(fu))
    print("  基差中位   %+.3f bps / 8h   <- 負的就是「被定價進去了」" % st.median(ba))
    print("  淨中位     %+.3f bps / 8h   淨>0 的窗 %.0f%%"
          % (st.median(ne), 100.0 * sum(1 for x in ne if x > 0) / len(ne)))
    print("  基差吃掉了資金費的 %.0f%%"
          % (-100.0 * st.median(ba) / st.median(fu) if st.median(fu) else float("nan")))
print()
print("── D1（真正的對照）：同場館配對強制 d=+1，premium 的 8h 漂移 ──")
for pair in sorted(BOTH_LIGHTER):
    rows = load(pair)
    if not rows:
        continue
    per_min = 60000.0 if rows[0][0] > 1e12 else 60.0
    drift = []
    i = 0
    while i + H < len(rows):
        t0, p0, _ = rows[i]
        t1, p1, _ = rows[i + H]
        if 400 <= (t1 - t0) / per_min <= 700:
            drift.append(-(p1 - p0))          # d 強制 = +1
        i += H
    if drift:
        drift.sort()
        print("   %-9s n=%d  中位 %+.3f bps  p25 %+.3f  p75 %+.3f"
              % (pair, len(drift), st.median(drift),
                 drift[len(drift) // 4], drift[3 * len(drift) // 4]))
print("   -> 中位遠離零就代表 basis 那一項混到了趨勢，不是純粹的 carry 不利漂移。")
print()
print("注意：三欄的中位數**不可相加**（NEAR 就是例子：0.040 + (−0.195) != +1.260）。")
print("      分布偏斜時中位數不是線性的 —— 要看配對層級的淨值中位，不要自己加。")
print()
print("* = 兩腿都在 Lighter 的對照組（D1）：資金費差必然 ~0。")
print("**這不是判決** —— 每個配對只有約 31 個不重疊窗，而且全部來自同一個月。")
