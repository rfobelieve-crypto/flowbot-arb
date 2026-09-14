"""我們有多少時間真的掛在簿口上,以及 20 秒逾時撤掉的是什麼樣的單。

2026-09-14。這支補的是「頻率要怎麼提升」拆解裡缺的那一項:

    頻率 = 流量 x 可及率 x 佇列勝率        <- missed_fills.py 量的
    而以上三個都乘在「我們在場的那段時間」上 <- **這支量的**

MON 的撤單理由分布裡,**逾時佔了快一半**,所以它值得單獨問:那些單被撤掉的
時候還是好的嗎？如果是,那 20 秒逾時就是在丟佇列位置換不到東西 ——
而那會是唯一一個「提高頻率但不用拿邊際去換」的旋鈕。

**這支的數字來自引擎自己記的欄位,不是事後重建的。** 理由值得寫下來,
因為我第一版就是重建的,而它錯了:

 1. `engine._maker_cancel_reason` 把**逾時檢查排在最前面**,所以理由是
    「unfilled after 20s」**不蘊含**「邊際還好、位置還好」—— 那兩個檢查在
    那一刻根本沒跑到。
 2. 從錄製器的逐秒頂檔重建,會撞上一個**跟結論同方向的取樣偏誤**:
    Lighter 的公開頂檔只在簿口變動時才更新,而逾時撤單正好發生在簿口不動的
    時候。實測撤單當下的頂檔年齡 ——

        behind the touch  n=39   中位 0.13 秒   77% 在 1 秒內
        edge decayed      n=202  中位 0.21 秒   84%
        **unfilled after  n=238  中位 2.64 秒   只有 32%**

    於是「只取新鮮的那些重建」會**系統性篩掉要量的那一群**,而且是往
    「市場有在動」偏 —— 剛好會讓逾時的單看起來比實際更該撤。

所以改成引擎在撤單那一刻自己記（`behind_at_cancel_bps` / `edge_at_cancel_bps`,
共用 `_maker_behind_bps` 一份實作）。這支只負責讀與彙總。

頂檔重建沒有丟掉,它降級成**控制組**:在頂檔夠新的那些列上,引擎記的值必須
跟獨立管線重算的值對得上。對不上 = 兩台儀器不同意 = 上面的數字不可引用
（mistake.md 2026-09-11）。

用法:
    python tools/maker_uptime.py --pair MON
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(ENGINE))
from arblib.maker_log import maker_log_paths          # noqa: E402

TOB = "D:/flowbot_data/lighter/tob"
FRESH_SEC = 1.0        # 控制組只用這麼新的頂檔（誤差中位 0.013 bps 的那一段）
NEAR_BPS = 1.0         # 「還在觸價上」的定義

RE_BEHIND = re.compile(r"^([0-9.-]+) bps behind the touch")
REASONS = ("unfilled after", "edge decayed", "behind the touch",
           "hedge depth gone", "venue outage", "volatility breaker",
           "own book stale", "hedge book stale", "hedge venue rate limited")


def short_reason(r) -> str:
    r = str(r)
    for k in REASONS:
        if k in r:
            return k
    return "(成交/未撤)" if r in ("", "nan") else r[:26]


def load_quotes(pair: str) -> pd.DataFrame:
    paths = maker_log_paths(os.path.join(ENGINE, "logs", pair))
    if not paths:
        sys.exit("沒有 logs/%s/maker.csv —— 這個標的沒跑過掛單路徑" % pair)
    d = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    if d.empty:
        sys.exit("logs/%s/maker.csv 是空的" % pair)
    d = d[d["rest_ms"].notna() & (d["rest_ms"] > 0)].copy()
    # ts 是**解析時刻**（engine._log_maker_csv 的 now）,rest_ms = now - sent_ts。
    d["end"] = d["ts"].astype(float)
    d["start"] = d["end"] - d["rest_ms"].astype(float) / 1e3
    d["why"] = d["cancel_reason"].map(short_reason)
    for c in ("behind_at_cancel_bps", "edge_at_cancel_bps"):
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    d["printed"] = [float(m.group(1)) if (m := RE_BEHIND.match(str(r)))
                    else float("nan") for r in d["cancel_reason"]]
    return d.sort_values("start").reset_index(drop=True)


def load_tob(coin: str, t0: float, t1: float) -> pd.DataFrame:
    hours, t = set(), t0 - 3600.0
    while t <= t1 + 3600.0:
        dt = datetime.fromtimestamp(t, timezone.utc)
        hours.add((dt.strftime("%Y%m%d"), dt.strftime("%H")))
        t += 1800.0
    frames = []
    for day, hh in sorted(hours):
        for f in glob.glob("%s/%s/%s.parquet" % (TOB, day, hh)):
            x = pd.read_parquet(f, columns=["rx_ms", "coin", "bid", "ask"])
            frames.append(x[x["coin"] == coin])
    if not frames:
        return pd.DataFrame(columns=["t", "bid", "ask"])
    d = pd.concat(frames, ignore_index=True)
    d["t"] = d["rx_ms"].astype("int64") / 1e3
    return d.sort_values("t").reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--coin", default=None, help="Lighter 幣名,預設同 --pair")
    a = ap.parse_args()
    coin = a.coin or a.pair
    q = load_quotes(a.pair)

    # ---- 1. 在簿口的時間 -------------------------------------------------
    span0, span1 = float(q["start"].min()), float(q["end"].max())
    span = span1 - span0
    merged, overlaps = [], 0
    for s, e in zip(q["start"], q["end"]):
        if merged and s < merged[-1][1] - 1e-3:
            overlaps += 1                       # 真重疊:嚴格小於,不是相接
            merged[-1][1] = max(merged[-1][1], e)
        elif merged and s <= merged[-1][1] + 1e-3:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    union = sum(e - s for s, e in merged)
    gaps = pd.Series([merged[i + 1][0] - merged[i][1]
                      for i in range(len(merged) - 1)], dtype=float)

    print("=== %s 在簿口的時間 ===" % a.pair)
    print("  牆鐘 %.1f 分,%d 張單" % (span / 60.0, len(q)))
    print("  在簿口 **%.1f%%**（%.1f 分）   不在簿口 %.1f 分"
          % (union / span * 100.0, union / 60.0, (span - union) / 60.0))
    if len(gaps):
        print("  空檔 n=%d  中位 %.2f 秒  p90 %.1f  最大 %.0f  合計 %.1f 分"
              % (len(gaps), gaps.median(), gaps.quantile(0.9), gaps.max(),
                 gaps.sum() / 60.0))
        big = gaps[gaps > 5]
        print("  其中 >5 秒的 %d 個就佔了 %.1f 分 —— 那是「兩側都不合格」的"
              "時刻,不是換單的間隙" % (len(big), big.sum() / 60.0))

    # ---- 2. 撤單那一刻,這張單在哪 ---------------------------------------
    have = q["behind_at_cancel_bps"].notna()
    print()
    print("=== 撤單當下離觸價幾 bps（引擎自己記的,負 = 在觸價之內）===")
    if not have.any():
        print("  **未量** —— 這批 log 是加上 behind_at_cancel_bps 之前寫的。")
        print("  重啟引擎之後才會開始有,不要拿頂檔重建代替（檔頭第 2 點）。")
    else:
        print("  %-22s %4s %8s %8s %8s %9s"
              % ("撤單理由", "n", "中位", "p90", "最大", "還在觸價"))
        for why, g in q[have].groupby("why"):
            b = g["behind_at_cancel_bps"]
            print("  %-22s %4d %8.2f %8.2f %8.2f %8.0f%%"
                  % (why, len(g), b.median(), b.quantile(0.9), b.max(),
                     (b <= NEAR_BPS).mean() * 100.0))
        to = q[have & (q["why"] == "unfilled after")]
        if len(to):
            near = int((to["behind_at_cancel_bps"] <= NEAR_BPS).sum())
            ed = to["edge_at_cancel_bps"].dropna()
            print()
            print("  逾時撤掉的 %d 張裡,撤單當下**還在觸價上**的有 **%d 張 "
                  "= %.0f%%**" % (len(to), near, near / len(to) * 100.0))
            if len(ed):
                print("  同一批的邊際中位 %.2f bps（撤單門檻 maker_min_edge）"
                      % ed.median())
            print("  這些是「位置還好、邊際還好、只是沒人來」—— 撤掉重掛丟的"
                  "是佇列位置,換不到東西。")

    # ---- 3. 自曝檢查 -----------------------------------------------------
    print()
    print("=== 自曝檢查 ===")
    checks = []

    ctl = q[q["printed"].notna() & q["behind_at_cancel_bps"].notna()]
    if ctl.empty:
        c = "SKIP"
        note = ("這批沒有任何一張是 reprice 規則撤的,或欄位還沒開始寫 —— "
                "**沒有控制組,上面的數字先不要引用**")
    else:
        err = (ctl["behind_at_cancel_bps"] - ctl["printed"]).abs()
        c = "PASS" if err.max() <= 0.2 else "**FAIL**"
        note = "n=%d 最大差 %.3f bps（同一個 poll、同一本簿口,應該幾乎相等）" \
               % (len(ctl), err.max())
    checks.append(c)
    print("  C1 引擎印在理由裡的 behind vs 記進 CSV 的  %s" % c)
    print("     %s" % note)

    tob = load_tob(coin, span0, span1)
    if tob.empty or not have.any():
        c = "SKIP"
        note = "沒有涵蓋這段時間的頂檔,或引擎還沒開始記 —— 少一道跨儀器對照"
    else:
        i = tob["t"].searchsorted(q["end"].values, side="right") - 1
        age = q["end"].values - tob["t"].values[i.clip(0)]
        bid = tob["bid"].values[i.clip(0)]
        ask = tob["ask"].values[i.clip(0)]
        mid = (bid + ask) / 2.0
        raw = ((bid - q["px"].values) * (q["side"].values == "BUY")
               + (q["px"].values - ask) * (q["side"].values != "BUY"))
        rebuilt = pd.Series(raw / mid * 1e4, index=q.index)
        fresh = pd.Series((i >= 0) & (age <= FRESH_SEC) & (ask > bid),
                          index=q.index) & have
        if fresh.sum() < 5:
            c, note = "SKIP", ("頂檔夠新的樣本只有 %d 列,對照不成立"
                               % int(fresh.sum()))
        else:
            e2 = (q.loc[fresh, "behind_at_cancel_bps"] - rebuilt[fresh]).abs()
            c = "PASS" if e2.median() <= 0.5 else "**FAIL**"
            note = ("n=%d（頂檔 <=%.0f 秒）誤差中位 %.3f 最大 %.2f bps"
                    % (int(fresh.sum()), FRESH_SEC, e2.median(), e2.max()))
    checks.append(c)
    print("  C2 引擎的簿口 vs 錄製器的獨立頂檔          %s" % c)
    print("     %s" % note)

    c = "PASS" if overlaps == 0 else "**FAIL**"
    checks.append(c)
    print("  C3 掛單視窗不得重疊（一次只掛一張）        %s  重疊 %d 張"
          % (c, overlaps))
    if overlaps:
        print("     重疊 = 視窗重建錯了,不是引擎掛了兩張")

    rest = float((q["end"] - q["start"]).sum())
    c = "PASS" if rest <= span * 1.02 else "**FAIL**"
    checks.append(c)
    print("  C4 掛單時間合計不得超過牆鐘                %s  %.1f vs %.1f 分"
          % (c, rest / 60.0, span / 60.0))

    bad = [x for x in checks if x.startswith("**")]
    skip = [x for x in checks if x == "SKIP"]
    print()
    if bad:
        print("  **有 %d 關沒過 —— 上面的數字不可引用**" % len(bad))
    elif skip:
        print("  %d 關通過,%d 關 SKIP（缺資料,不是通過）"
              % (len(checks) - len(skip), len(skip)))
    else:
        print("  全過")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
