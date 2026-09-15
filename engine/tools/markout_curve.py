# -*- coding: utf-8 -*-
"""逆選擇隨時間長什麼樣 —— 以及**它是不是只是這個市場自己的波動**。

2026-09-15。使用者問：我們的半價差與逆選擇都是 Quant Arb 的十倍,
**那會不會是我們當初設計就選錯了東西**。

這支回答那個問題，方法是把「我們的成交」跟**同一個市場的隨機時刻**比。

    逆選擇(h)      我們成交之後 h 秒,中價往我們不利的方向跑了幾 bps
    隨機對照(h)    同一段時間隨機挑時刻,往「同樣的方向定義」跑了幾 bps
    無條件波動(h)  同一段時間 |中價變動| 的中位（這是尺）

**判斷法**:
  逆選擇 ÷ 無條件波動 ≈ 1     -> 我們的成交幾乎**完全**被一次典型的波動挑走
                               = 選擇問題,不是資訊問題,而它隨價差放大
  逆選擇 ÷ 無條件波動 ≈ 0.2   -> 跟 Binance 前 50 同一個量級,是真的資訊

因為如果寬價差的市場只是波動大,那個寬價差**就是這個波動的報酬**,
不是免費的錢 —— 而我們的篩選器 G1 只看價差、沒有波動項。

**兩端的中價都取自同一個來源**（`D:/flowbot_data/lighter/tob`,掛單腿自己
的逐檔錄製器）。引擎 CSV 裡的 `mid_at_fill` 記的是**對沖腿（HL）**的中價,
拿它配 Lighter 的未來中價,量到的是「逆選擇 ＋ 兩所基差」（mistake.md
2026-09-14 記過這個形狀,而 `fill_decomp.py` 第一版就是這樣寫的）。
舊讀數仍然印出來當對照 —— 它被引用過,不可以悄悄消失。

用法:
    python tools/markout_curve.py --pair MON
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(ENGINE))
from arblib.maker_log import maker_log_paths            # noqa: E402

TOB = "D:/flowbot_data/lighter/tob"
HORIZONS = [1, 2, 3, 5, 10, 20, 30, 60, 120, 300]
FRESH_SEC = 30.0        # 對到的頂檔比這舊就丟掉（薄市場更新本來就慢）
N_RANDOM = 4000         # 隨機對照的抽樣數


def load_tob(coin, t0, t1):
    hours, t = set(), t0 - 3600.0
    while t <= t1 + 7200.0:
        dt = datetime.fromtimestamp(t, timezone.utc)
        hours.add((dt.strftime("%Y%m%d"), dt.strftime("%H")))
        t += 1800.0
    fr = []
    for day, hh in sorted(hours):
        for f in glob.glob("%s/%s/%s.parquet" % (TOB, day, hh)):
            x = pd.read_parquet(f, columns=["rx_ms", "coin", "bid", "ask"])
            fr.append(x[x["coin"] == coin])
    if not fr:
        sys.exit("錄製器目錄裡沒有涵蓋這段時間的頂檔")
    d = pd.concat(fr, ignore_index=True)
    d["t"] = d["rx_ms"].astype("int64") / 1e3
    d = d[(d["ask"] > d["bid"]) & (d["bid"] > 0)].sort_values("t")
    d["mid"] = (d["bid"] + d["ask"]) / 2.0
    return d.reset_index(drop=True)


def asof(tob, times, col="mid"):
    """回傳 (值, 這一筆頂檔有多舊)。太舊的由呼叫端丟掉。"""
    i = tob["t"].searchsorted(times, side="right") - 1
    ok = i >= 0
    v = np.full(len(times), np.nan)
    age = np.full(len(times), np.inf)
    v[ok] = tob[col].values[i[ok]]
    age[ok] = times[ok] - tob["t"].values[i[ok]]
    return v, age


def far_side(tob, times, s):
    """**我們沒碰到的那一側。**

    賣在 ask 就看 bid、買在 bid 就看 ask。中價量到的逆選擇裡有一部分是
    **我們自己被吃掉造成的位移** —— MON 一個 tick 是 4.28 bps、價差有六個
    tick 寬,我們多半是那個價位上唯一的單,所以我們一消失,觸價就跳到下一張
    單那裡,中價跟著動,而那不是任何人的資訊。
    另一側沒有我們的單,所以它的移動不含這個footprint。
    """
    bid, age = asof(tob, times, "bid")
    ask, _ = asof(tob, times, "ask")
    return np.where(s > 0, bid, ask), age


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--coin", default=None)
    a = ap.parse_args()
    coin = a.coin or a.pair

    d = pd.concat([pd.read_csv(p) for p in
                   maker_log_paths(os.path.join(ENGINE, "logs", a.pair))],
                  ignore_index=True)
    f = d[d["filled"].astype(float) > 0].copy().sort_values("ts")
    if f.empty:
        sys.exit("沒有成交")
    f["ts"] = f["ts"].astype(float)
    # 成交時刻 = 解析時刻 − （掛著多久 − 多久成交）
    f["t_fill"] = (f["ts"] - (f["rest_ms"].astype(float)
                              - pd.to_numeric(f["first_fill_ms"],
                                              errors="coerce")) / 1e3)
    f = f[f["t_fill"].notna()]
    tob = load_tob(coin, float(f["t_fill"].min()), float(f["t_fill"].max()))

    tf = f["t_fill"].values
    m0, age0 = asof(tob, tf)
    keep = age0 <= FRESH_SEC
    f, tf, m0 = f[keep].copy(), tf[keep], m0[keep]
    px = f["px"].astype(float).values
    # 賣出 -> 之後中價往上是不利；買進 -> 往下是不利
    s = np.where(f["side"].values == "BUY", -1.0, 1.0)
    half = s * (px - m0) / m0 * 1e4

    # ---- 隨機對照：同一段時間、同樣的方向混合,但時刻是隨機的 -----------
    rng = np.random.default_rng(42)
    lo, hi = tob["t"].iloc[0], tob["t"].iloc[-1] - max(HORIZONS)
    rt = rng.uniform(lo, hi, N_RANDOM)
    rs = rng.choice(s, N_RANDOM) if len(s) else np.ones(N_RANDOM)
    rm0, rage = asof(tob, rt)
    rkeep = rage <= FRESH_SEC
    rt, rs, rm0 = rt[rkeep], rs[rkeep], rm0[rkeep]

    print("=== %s：逆選擇 vs 時間（n=%d 筆成交，%.1f 小時）===" %
          (a.pair, len(f), (tf.max() - tf.min()) / 3600))
    print("  賺到的半價差（用掛單腿自己的中價）中位 %+.2f bps" %
          np.median(half))
    print()
    # **中位數在這裡幾乎沒有解析度。** MON 一個 tick = 4.28 bps,中價只會
    # 跳半個 tick 的整數倍,所以 31 筆的中位必然落在 0 / ±2.14 / ±4.28 上。
    # 平均才看得到分布,所以兩個都印,而判斷用平均。
    print("  %5s %5s %9s %9s %11s %11s %8s" %
          ("h 秒", "n", "中價逆選", "**遠側**", "隨機對照", "無條件波動",
           "遠側比值"))
    far0, _ = far_side(tob, tf, s)
    rfar0, _ = far_side(tob, rt, rs)
    rows = []
    for h in HORIZONS:
        mh, ageh = asof(tob, tf + h)
        ok = ageh <= FRESH_SEC
        if ok.sum() < 5:
            print("  %5d   樣本不足（%d）" % (h, int(ok.sum())))
            continue
        adv = -s[ok] * (mh[ok] - m0[ok]) / m0[ok] * 1e4
        farh, _ = far_side(tob, tf + h, s)
        advf = -s[ok] * (farh[ok] - far0[ok]) / m0[ok] * 1e4

        rmh, rageh = asof(tob, rt + h)
        rok = rageh <= FRESH_SEC
        rfarh, _ = far_side(tob, rt + h, rs)
        radvf = -rs[rok] * (rfarh[rok] - rfar0[rok]) / rm0[rok] * 1e4
        vol = np.mean(np.abs((rfarh[rok] - rfar0[rok]) / rm0[rok] * 1e4))

        ratio = abs(np.mean(advf)) / vol if vol > 0 else float("nan")
        rows.append((h, int(ok.sum()), np.mean(adv), np.mean(advf),
                     np.mean(radvf), vol, ratio))
        print("  %5d %5d %+9.2f %+9.2f %+11.2f %11.2f %8.2f" % rows[-1])

    print()
    print("=== 自曝檢查 ===")
    bad = []
    # C1 隨機對照必須貼近零。不貼近 = 這支在量趨勢不是在量逆選擇。
    worst = max(abs(r[4]) for r in rows) if rows else 0.0
    c = "PASS" if worst < 2.0 else "**FAIL**"
    if c.startswith("*"):
        bad.append("C1")
    print("  C1 隨機時刻的「逆選擇」必須 ~0            %s  最大 %+.2f bps"
          % (c, worst))
    # C2 h 越短逆選擇越小（t=0 依定義是 0）。反過來就是儀器有偏移。
    c = "PASS" if rows and abs(rows[0][2]) < abs(rows[-1][2]) else "**FAIL**"
    if c.startswith("*"):
        bad.append("C2")
    print("  C2 最短視窗的逆選擇要小於最長視窗的        %s" % c)
    # C3 我們賺到的半價差不可以**超過**市場自己的半價差 —— post-only 掛在
    # 觸價或更裡面,超過就代表兩端的中價不是同一本簿口（基差混進來了）。
    hs = ((tob["ask"] - tob["bid"]) / (tob["ask"] + tob["bid"]) * 1e4).median()
    c = "PASS" if np.median(half) <= hs + 1.0 else "**FAIL**"
    if c.startswith("*"):
        bad.append("C3")
    print("  C3 賺到的半價差不可超過市場自己的半價差    %s  %+.2f vs %.2f"
          % (c, np.median(half), hs))

    print()
    print("=== 對照：舊讀數（`mid_at_fill` = **對沖腿 HL** 的中價）===")
    om = pd.to_numeric(f["mid_at_fill"], errors="coerce").values
    oh = s * (px - om) / om * 1e4
    print("  賺到的半價差 中位 %+.2f（含兩所基差）vs 修正後 %+.2f  差 %+.2f bps"
          % (np.median(oh), np.median(half), np.median(oh) - np.median(half)))

    print()
    print("  %s" % ("全過" if not bad else "**沒過（%s），數字不可引用**"
                    % ",".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
