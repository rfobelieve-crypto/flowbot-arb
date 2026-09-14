# -*- coding: utf-8 -*-
"""成交發生的**那一刻**，我們的單在哪裡。

2026-09-15。這支存在的理由是兩台儀器對同一件事給出不同的圖像：

    tools/maker_uptime.py   撤單當下，56% 的逾時單**還在觸價上**
                            （edge decayed 那組甚至 100% 在觸價上）
    tools/missed_fills.py   我們那一側的成交，**88% 碰不到我們的價**
                            （它的結論行因此寫「掛太遠」）

兩個都不可能是錯的儀器 —— 它們量的是**不同的時刻**：一個是撤單那一秒，
一個是整段掛單期間。所以矛盾本身就是一個假說：**我們在觸價上與不在觸價上
之間來回，而成交不是均勻到達的**。

這支直接量那個假說：對每一筆打到我們那一側的市場成交，算我們當下
    (a) 有沒有被打到（我們的價 vs 成交價）
    (b) 離觸價多遠（我們的價 vs 該瞬間的頂檔）
如果 (b) 在「碰不到」那群上系統性地大於零，假說成立 ——
**流量在我們落後的時候到達**，而那是逆選擇最純粹的形狀，不是「掛太遠」。

反過來，如果碰不到的那群我們也在觸價上，那 missed_fills 的側別判定有問題，
要查的是它不是市場。

**兩個方向的下一步完全相反**，所以這個量值得單獨做一支：
    落後時到達 -> 要縮短「落後」的時間（reprice 更緊、逾時更長）
    在觸價也沒吃到 -> 佇列位置，那要的是完全不同的工程

資料：掛單來自引擎自己寫的 maker.csv（含輪替世代），成交與頂檔來自兩條
獨立的常駐 WS 錄製器。

用法：
    python tools/why_missed.py --pair MON
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(ENGINE))
from arblib.maker_log import maker_log_paths          # noqa: E402

TAPE = "D:/flowbot_data/lighter/trades"
TOB = "D:/flowbot_data/lighter/tob"
FRESH_SEC = 2.0


def _hours(t0: float, t1: float):
    out, t = set(), t0 - 3600.0
    while t <= t1 + 3600.0:
        dt = datetime.fromtimestamp(t, timezone.utc)
        out.add((dt.strftime("%Y%m%d"), dt.strftime("%H")))
        t += 1800.0
    return sorted(out)


def _load(root: str, coin: str, cols: list, t0: float, t1: float):
    fr = []
    for day, hh in _hours(t0, t1):
        for f in glob.glob("%s/%s/%s.parquet" % (root, day, hh)):
            x = pd.read_parquet(f, columns=cols)
            fr.append(x[x["coin"] == coin])
    if not fr:
        return pd.DataFrame(columns=cols)
    return pd.concat(fr, ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--coin", default=None)
    a = ap.parse_args()
    coin = a.coin or a.pair

    paths = maker_log_paths(os.path.join(ENGINE, "logs", a.pair))
    if not paths:
        sys.exit("沒有 logs/%s/maker.csv" % a.pair)
    q = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    q = q[q["rest_ms"].notna() & (q["rest_ms"] > 0)].copy()
    q["end"] = q["ts"].astype(float)
    q["beg"] = q["end"] - q["rest_ms"].astype(float) / 1e3
    q["is_buy"] = q["side"].values == "BUY"
    t0, t1 = float(q["beg"].min()), float(q["end"].max())

    tp = _load(TAPE, coin, ["ts", "coin", "px", "sz", "usd", "is_maker_ask"],
               t0, t1)
    if tp.empty:
        sys.exit("成交帶沒有涵蓋這段時間 —— 無法回答")
    tp["t"] = tp["ts"].astype("int64") / 1e3
    # 成交帶的時間範圍蓋不住掛單範圍就只分析交集（不要假裝蓋得住）
    lo, hi = float(tp["t"].min()), float(tp["t"].max())
    q = q[(q["end"] >= lo) & (q["beg"] <= hi)].copy()

    tob = _load(TOB, coin, ["rx_ms", "coin", "bid", "ask"], t0, t1)
    tob["t"] = tob["rx_ms"].astype("int64") / 1e3
    tob = tob.sort_values("t").reset_index(drop=True)

    rows = []
    for _, o in q.iterrows():
        # 我們掛買單 -> 會打到我們的是**賣方吃單**(maker 在 bid) -> is_maker_ask False
        want = (not o["is_buy"])
        sel = tp[(tp["t"] >= o["beg"]) & (tp["t"] <= o["end"])
                 & (tp["is_maker_ask"].astype(bool) == want)]
        for _, tr in sel.iterrows():
            i = tob["t"].searchsorted(tr["t"], side="right") - 1
            bid = ask = None
            age = None
            if i >= 0:
                bid, ask = float(tob["bid"].iloc[i]), float(tob["ask"].iloc[i])
                age = tr["t"] - float(tob["t"].iloc[i])
            reached = (tr["px"] <= o["px"]) if o["is_buy"] else (tr["px"] >= o["px"])
            behind = None
            if bid and ask and ask > bid:
                mid = (bid + ask) / 2.0
                raw = (bid - o["px"]) if o["is_buy"] else (o["px"] - ask)
                behind = raw / mid * 1e4
            rows.append({"t": tr["t"], "usd": float(tr["usd"]),
                         "reached": bool(reached), "behind": behind,
                         "tob_age": age, "our_px": float(o["px"]),
                         "tr_px": float(tr["px"]), "is_buy": bool(o["is_buy"])})

    r = pd.DataFrame(rows).drop_duplicates(subset=["t", "tr_px", "usd"])
    if r.empty:
        sys.exit("我們掛著的期間，我們那一側一筆成交都沒有 —— 樣本為零")

    print("=== %s：我們那一側的市場成交 %d 筆（掛單 %d 張）==="
          % (a.pair, len(r), len(q)))
    hit = r[r["reached"]]
    miss = r[~r["reached"]]
    print("  打到我們的      %3d 筆  $%.2f" % (len(hit), hit["usd"].sum()))
    print("  沒打到我們的    %3d 筆  $%.2f" % (len(miss), miss["usd"].sum()))
    print()
    print("=== 關鍵：成交那一刻，我們離觸價多遠 ===")
    print("  %-14s %4s %8s %8s %8s %10s"
          % ("", "n", "中位", "p90", "最大", "在觸價上"))
    for lab, s in (("打到我們", hit), ("**沒打到**", miss)):
        b = s["behind"].dropna()
        if b.empty:
            print("  %-14s %4d      未量（沒有頂檔對得到）" % (lab, len(s)))
            continue
        print("  %-14s %4d %8.2f %8.2f %8.2f %9.0f%%"
              % (lab, len(b), b.median(), b.quantile(0.9), b.max(),
                 (b <= 1.0).mean() * 100))

    mb = miss["behind"].dropna()
    if len(mb) >= 5:
        print()
        if mb.median() > 1.0:
            print("  -> **沒打到我們的那些成交，到達時我們中位落後 %.1f bps**。"
                  % mb.median())
            print("     不是「掛太遠」——送出時我們在觸價上，是**流量在我們"
                  "落後的那些時刻到達**。下一步是縮短落後的時間"
                  "（reprice 更緊 / 逾時更長），不是放寬帶。")
        else:
            print("  -> 沒打到的那些，我們當下**也在觸價上**（中位 %.1f bps）。"
                  % mb.median())
            print("     那就不是位置問題，是佇列位置或側別判定 —— 先查"
                  "missed_fills 的側別，再談佇列。")

    print()
    print("=== 成交大小（能不能吃得到跟值不值得吃是兩件事）===")
    print("  全部 %d 筆：中位 $%.2f  p90 $%.2f  最大 $%.2f  合計 $%.2f"
          % (len(r), r["usd"].median(), r["usd"].quantile(0.9),
             r["usd"].max(), r["usd"].sum()))
    dust = r[r["usd"] < 1.0]
    print("  小於 $1 的灰塵 %d 筆 = %.0f%%（吃到也不值錢）"
          % (len(dust), len(dust) / len(r) * 100))

    print()
    print("=== 自曝檢查 ===")
    bad = []
    c = "PASS" if len(hit) >= 1 else "**FAIL**"
    if c.startswith("**"):
        bad.append(c)
    print("  C1 我們真的成交過，所以「打到我們」不可以是 0   %s  n=%d"
          % (c, len(hit)))
    age = r["tob_age"].dropna()
    c = "PASS" if len(age) and age.median() <= FRESH_SEC else "**FAIL**"
    if c.startswith("**"):
        bad.append(c)
    print("  C2 對到的頂檔要夠新                             %s  中位 %.2fs"
          % (c, age.median() if len(age) else -1))
    ov = (r["behind"].notna()).mean() * 100
    c = "PASS" if ov >= 60 else "**FAIL**"
    if c.startswith("**"):
        bad.append(c)
    print("  C3 有頂檔可算的比例                             %s  %.0f%%"
          % (c, ov))
    print()
    print("  %s" % ("全過" if not bad else "**有 %d 關沒過 —— 數字不可引用**"
                    % len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
