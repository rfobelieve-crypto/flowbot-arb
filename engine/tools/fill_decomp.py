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

**兩端的 mid 都來自錄製器的逐檔頂檔**（`D:/flowbot_data/lighter/tob`），
不是引擎自己的簿口 —— 兩條獨立管線,不然這就是拿自己驗自己。

> **2026-09-15 修正，第一版是錯的。** 第一版的成交當下 mid 取 CSV 的
> `mid_at_fill`,而那個欄位記的是**對沖腿（HL）**的中價（engine.py:1480
> `taker_v.book.mid()`）。拿它配 Lighter 的未來 mid,量到的是
> 「逆選擇 ＋ **兩所基差**」。實測基差把半價差灌水 **+4.69 bps**、
> 把逆選擇同額往下壓。
> 這正是 mistake.md 2026-09-14 記過的形狀（引擎為此另外記了
> `maker_mid_at_fill`）—— 那條教訓修了引擎,沒有修到隔天才寫的這支工具。
> 舊讀數仍然印在下面當對照,它被引用過,不可以悄悄消失。

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
sys.path.insert(0, HERE)
from arblib.maker_log import maker_log_paths            # noqa: E402
# **同一份頂檔載入與對齊,不要第二份實作。** 兩支工具對同一件事給出不同
# 答案的時候,先查它們跑在什麼之上（mistake.md 2026-09-11）—— 而最省事的
# 做法是讓它們不可能不同。
from markout_curve import FRESH_SEC, asof, load_tob     # noqa: E402


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
    # **兩端都取自這一本簿口。** 成交當下的 mid 不可以用 CSV 的
    # `mid_at_fill` —— 那是對沖腿的（見檔頭的更正）。
    base, base_age = asof(tob, f["t_fill"].values)
    fut, fut_age = asof(tob, f["t_fill"].values + a.horizon)
    f["maker_mid"], f["fut_mid"] = base, fut
    f["fut_age"], f["base_age"] = fut_age, base_age
    f = f[f["maker_mid"].notna() & (f["base_age"] <= FRESH_SEC)].copy()

    m0 = f["maker_mid"].astype(float)
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
    h, adv = good["half"].mean(), good["adv"].mean()
    print("  我們的半價差是他的 **%.1f 倍**，逆選擇是他的 **%.1f 倍**"
          % (h / 1.25, abs(adv) / 1.07))
    # **留下來的佔比才是結構**,倍數本身只說這個市場比較寬。
    print("  留下來的佔比：我們 %.0f%%（%.2f / %.2f）  他 %.0f%%（0.19 / 1.25）"
          % ((1 - abs(adv) / h) * 100 if h else 0, h - abs(adv), h, 15.2))

    print()
    print("=== 對照：舊讀數（`mid_at_fill` = **對沖腿 HL** 的中價）===")
    om = pd.to_numeric(good["mid_at_fill"], errors="coerce")
    opx, obuy = good["px"].astype(float), good["side"].values == "BUY"
    ohalf = ((om - opx) * obuy + (opx - om) * (~obuy)) / om * 1e4
    print("  賺到的半價差 平均 %+.2f（含兩所基差）vs 修正後 %+.2f  差 %+.2f bps"
          % (ohalf.mean(), h, ohalf.mean() - h))

    print()
    print("=== 自曝檢查 ===")
    bad = []
    c = "PASS" if (good["half"] > 0).mean() > 0.8 else "**FAIL**"
    if c.startswith("*"):
        bad.append("C1")
    print("  C1 賺到的半價差幾乎都該是正的（我們掛在 mid 外側）  %s  %.0f%%"
          % (c, (good["half"] > 0).mean() * 100))
    age = good["fut_age"]
    c = "PASS" if age.median() <= FRESH_SEC else "**FAIL**"
    if c.startswith("*"):
        bad.append("C2")
    print("  C2 未來頂檔要夠新                                  %s  中位 %.1fs"
          % (c, age.median()))
    # C3 **新增,而且它就是第一版死掉的那一關**:post-only 掛在觸價或更裡面,
    # 所以賺到的半價差不可能超過這個市場自己的半價差。第一版（拿 HL 的
    # 中價當基準）在這一關會紅 —— 13.21 > 12.81。
    hs = ((tob["ask"] - tob["bid"]) / (tob["ask"] + tob["bid"]) * 1e4).median()
    c = "PASS" if h <= hs + 1.0 else "**FAIL**"
    if c.startswith("*"):
        bad.append("C3")
    print("  C3 賺到的半價差不可超過市場自己的半價差            %s  %+.2f vs %.2f"
          % (c, h, hs))
    drop = len(f) - len(good)
    print("  C4 因頂檔太舊丟掉 %d / %d 筆（丟太多就是這個市場"
          "報價太稀疏，數字要打折）" % (drop, len(f)))
    print()
    print("  %s" % ("全過" if not bad
                    else "**沒過（%s），數字不可引用**" % ",".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
