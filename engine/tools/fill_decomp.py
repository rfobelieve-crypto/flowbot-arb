# -*- coding: utf-8 -*-
"""每筆成交拆成兩項：**賺到的半價差** 與 **逆選擇**。負期望值是哪一項造成的。

2026-09-15。使用者問：我們負期望、他正期望，關鍵差異是不是成交率？

**成交率不決定符號。** 期望值的符號由每一筆的分解決定，成交率只決定
你收幾次。所以要看的是這個分解 —— 而 Quant Arb `analysing-real-fills`
給了他那一邊的同一個分解（35,107 筆）:

    賺到的半價差  +1.25
    逆選擇        -1.07
                  ------
    淨            **+0.19 bps**

這支算我們的同一組數，**用同一個定義**:

    賺到的半價差 = |成交價 - 成交當下掛單腿的 mid| / mid * 1e4
    逆選擇       = 買:(未來 mid - 成交價)  賣:(成交價 - 未來 mid)，除 mid
    淨           = 兩者相加（= 他圖上的 "markout + half-spread"）

**未來 mid 來自錄製器的逐秒頂檔**（`D:/flowbot_data/lighter/tob`），
不是引擎自己的簿口 —— 兩條獨立管線,不然這就是拿自己驗自己。

**基準是 mid 不是成交價**（mistake.md 2026-09-14）:我們成交在自己的報價上,
而那個價格已經含了半個價差。用成交價當基準,價差越寬越會誤判通過。

用法:
    python tools/fill_decomp.py --pair MON [--horizon 60]
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
from arblib.maker_log import maker_log_paths            # noqa: E402

TOB = "D:/flowbot_data/lighter/tob"
FRESH_SEC = 30.0        # 對到的頂檔比這舊就不算（薄市場更新本來就慢）


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
        sys.exit("成交帶目錄裡沒有涵蓋這段時間的頂檔")
    d = pd.concat(fr, ignore_index=True)
    d["t"] = d["rx_ms"].astype("int64") / 1e3
    d = d[(d["ask"] > d["bid"]) & (d["bid"] > 0)].sort_values("t")
    d["mid"] = (d["bid"] + d["ask"]) / 2.0
    return d.reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--coin", default=None)
    ap.add_argument("--horizon", type=float, default=60.0)
    a = ap.parse_args()
    coin = a.coin or a.pair

    paths = maker_log_paths(os.path.join(ENGINE, "logs", a.pair))
    d = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    f = d[(d["filled"].astype(float) > 0)
          & d["mid_at_fill"].notna()].copy().sort_values("ts")
    if f.empty:
        sys.exit("沒有帶 mid_at_fill 的成交")
    f["ts"] = f["ts"].astype(float)
    # 成交時刻 = 解析時刻 - (rest - first_fill)。first_fill_ms 是送單到成交。
    f["t_fill"] = (f["ts"] - (f["rest_ms"].astype(float)
                              - pd.to_numeric(f["first_fill_ms"],
                                              errors="coerce")) / 1e3)
    f = f[f["t_fill"].notna()]

    tob = load_tob(coin, float(f["t_fill"].min()), float(f["t_fill"].max()))
    tgt = f["t_fill"].values + a.horizon
    i = tob["t"].searchsorted(tgt, side="right") - 1
    ok = i >= 0
    f = f[ok].copy()
    i = i[ok]
    f["fut_mid"] = tob["mid"].values[i]
    f["fut_age"] = (f["t_fill"].values + a.horizon) - tob["t"].values[i]

    m0 = f["mid_at_fill"].astype(float)
    px = f["px"].astype(float)
    is_buy = f["side"].values == "BUY"
    # 賺到的半價差：買在 mid 之下、賣在 mid 之上，兩種都是正的收入
    f["half"] = ((m0 - px) * is_buy + (px - m0) * (~is_buy)) / m0 * 1e4
    # 逆選擇：買 -> 之後 mid 往上是好事；賣 -> 往下是好事
    f["adv"] = (((f["fut_mid"] - px) * is_buy
                 + (px - f["fut_mid"]) * (~is_buy)) / m0 * 1e4) - f["half"]
    f["net"] = f["half"] + f["adv"]

    good = f[f["fut_age"] <= FRESH_SEC]
    print("=== %s：每筆成交的分解（n=%d，視窗 %.0f 秒）==="
          % (a.pair, len(good), a.horizon))
    if len(good) < 3:
        print("  **樣本太少（%d），不解讀**" % len(good))
        return 0
    print("  %-16s %9s %9s" % ("", "中位", "平均"))
    for lab, col in (("賺到的半價差", "half"), ("逆選擇", "adv"),
                     ("**淨**", "net")):
        print("  %-16s %+9.2f %+9.2f"
              % (lab, good[col].median(), good[col].mean()))
    print()
    print("  淨為正的 %d / %d = %.0f%%"
          % ((good["net"] > 0).sum(), len(good),
             (good["net"] > 0).mean() * 100))
    print()
    print("=== 對照 Quant Arb（35,107 筆，Binance 前 50，t=0）===")
    print("  %-16s %+9.2f" % ("賺到的半價差", 1.25))
    print("  %-16s %+9.2f" % ("逆選擇", -1.07))
    print("  %-16s %+9.2f" % ("**淨**", 0.19))
    print()
    h, adv = good["half"].median(), good["adv"].median()
    print("  我們的半價差是他的 **%.1f 倍**，逆選擇是他的 **%.1f 倍**"
          % (h / 1.25, abs(adv) / 1.07))
    print("  -> %s" % ("**收入端贏他，但被逆選擇吃掉更多**"
                       if h > 1.25 and abs(adv) > 1.07 else
                       "形狀跟他不同，先查這支"))

    print()
    print("=== 自曝檢查 ===")
    bad = []
    c = "PASS" if (good["half"] > 0).mean() > 0.8 else "**FAIL**"
    if c.startswith("*"):
        bad.append(c)
    print("  C1 賺到的半價差幾乎都該是正的（我們掛在 mid 外側）%s  %.0f%%"
          % (c, (good["half"] > 0).mean() * 100))
    age = good["fut_age"]
    c = "PASS" if age.median() <= FRESH_SEC else "**FAIL**"
    if c.startswith("*"):
        bad.append(c)
    print("  C2 未來頂檔要夠新                                 %s  中位 %.1fs"
          % (c, age.median()))
    drop = len(f) - len(good)
    print("  C3 因頂檔太舊丟掉 %d / %d 筆（丟太多就是這個市場"
          "報價太稀疏，數字要打折）" % (drop, len(f)))
    print()
    print("  %s" % ("全過" if not bad else "**沒過，數字不可引用**"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
