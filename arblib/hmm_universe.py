# -*- coding: utf-8 -*-
"""全宇宙 HMM 篩選 —— **不需要先為每個市場跑一個引擎**。

    python arblib/hmm_universe.py
    python arblib/hmm_universe.py --top 30 --hours 24

===========================================================================
為什麼要有這一支（2026-09-14）
===========================================================================
`hmm_screen.py` 只看得到錄製家族那十個配對，因為它讀的是引擎的
`minutes.csv`。於是「換一個標的」變成「先開一個錄製器、等幾小時、再看」——
而那正是 2026-09-14 那天連換三個標的的結構性原因：**驗證一個候選的成本
等於跑一個引擎**，所以一次只驗得了一個。

但五關裡有三關**只需要場館自己的公開資料**：

    G1 淨值   Lighter 半價差 − HL 半價差 − 費用      <- 兩邊的簿口錄製
    G4 切片   Lighter 逐筆成交的中位金額             <- 成交帶
    G5 雙向   吃單流的少數側                          <- 成交帶的 is_maker_ask

而我們已經在錄這三樣東西（flow_system 的 lighter/tob、lighter/trades、
hl/mid），127 個市場、每分鐘。所以這三關可以**一次算完全宇宙**，
把「跑引擎」留給真的值得跑的那幾個。

===========================================================================
G5 取代了「溢價兩側」，而這件事是實盤打出來的
===========================================================================
2026-09-14 我先用**溢價的兩側**估「庫存轉不轉得動」：

    MET 溢價兩側    賣側 86.0% / 買側 83.8% / 少數側 **49.4%**  -> 看起來很平衡
    MET 實盤        121 筆成交 **全部是 SELL**，買單掛 82 次成交 0 次
    MET 吃單流      吃單方在買 **93.1%**，少數側 **6.9%**      -> 與實盤一致

**溢價說「兩邊都有得掛」，而那是對的 —— 錯的是「掛了就會成交」。**
要打到我們的買單，得有人主動**賣**給我們；溢價不管這件事，吃單流管。
所以離線篩選用 G5，不用溢價版本。（溢價版本留在 hmm_screen 的 G2，
那裡讀的是引擎自己的決策，語意不同。）

===========================================================================
這支**不**回答什麼
===========================================================================
M2（成交率）與 M3（逆選擇）仍然只有實盤量得到。這支回答的是
「**哪些市場值得花那筆錢**」，把候選從 127 個縮到可以一個一個跑的數量。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

DATA = os.path.join("D:", os.sep, "flowbot_data")
HL_INFO = "https://api.hyperliquid.xyz/info"

# 判準（與 hmm_screen.py 同源；改一邊就要改另一邊）
FEES_BPS = 0.40 + 4.50          # Lighter maker + HL taker
G1_NET_BPS = 3.0
G4_MIN_SLICE_USD = 10.0
G5_TAKER_MINORITY_PCT = 35.0
MIN_TRADES = 100                # 樣本下限；低於這個一律「未量」
MIN_HL_VOL_USD = 50_000.0       # 對沖腿要有量，否則它是死的（AI 的教訓）


def hl_post(body, tries: int = 6):
    """HL 的 /info，帶退避重試。

    這台機器上十幾支引擎在共用同一個 IP 的預算，撞到 429 是常態不是例外。
    沒有退避 = 每次都要人重跑一次，而人第三次就會改成「先不管那個欄位」。
    """
    req = urllib.request.Request(HL_INFO, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            if i == tries - 1:
                raise RuntimeError(
                    "HL /info 連續 %d 次 %s —— 等幾分鐘再跑，不要把讀不到"
                    "當成『那個市場不存在』" % (tries, e.code)) from e
            w = 4.0 * (2 ** i)
            print("  HL %s，%.0f 秒後重試" % (e.code, w), file=sys.stderr)
            time.sleep(w)


def _read(sub, cols, nfiles):
    import pandas as pd
    fs = sorted(glob.glob(os.path.join(DATA, *sub, "*", "*.parquet")))
    if not fs:
        raise RuntimeError(
            "讀不到 %s —— 空結果不是合法狀態。D 槽掛載了嗎？"
            "（CLAUDE.md §大資料在 D 槽：連結斷掉時那個路徑會變成空目錄，"
            "而每一支讀它的程式都會安靜地讀到零列）" % os.path.join(*sub))
    return pd.concat([pd.read_parquet(f, columns=cols) for f in fs[-nfiles:]],
                     ignore_index=True), len(fs[-nfiles:])


def build(hours: int):
    import numpy as np
    import pandas as pd

    n = max(hours, 1)
    lt, n1 = _read(["lighter", "tob"], ["coin", "bid", "ask"], n)
    hl, n2 = _read(["hl", "mid"], ["coin", "spread_bps"], n * 2)
    tp, n3 = _read(["lighter", "trades"], ["coin", "usd", "is_maker_ask"], n)

    lt = lt[(lt.bid > 0) & (lt.ask > lt.bid)].copy()
    lt["hs"] = (lt.ask - lt.bid) / (lt.ask + lt.bid) * 1e4
    hl = hl[hl.spread_bps > 0].copy()

    g = tp.groupby("coin")
    df = pd.DataFrame({
        "lt_hs": lt.groupby("coin")["hs"].median(),
        "hl_hs": hl.groupby("coin")["spread_bps"].median() / 2.0,  # 半價差
        "slice": g["usd"].median(),
        "taker_buy": g["is_maker_ask"].mean() * 100.0,
        "trades": g.size(),
        "vol": g["usd"].sum(),
    })
    df = df[df.trades >= MIN_TRADES].dropna(subset=["lt_hs", "slice"])
    df["taker_min"] = np.minimum(df.taker_buy, 100.0 - df.taker_buy)
    # 對沖腿讀不到半價差時**不要猜** —— 那一列標成未量，不進判定。
    df["net"] = df.lt_hs - df.hl_hs - FEES_BPS

    meta = hl_post({"type": "metaAndAssetCtxs"})
    core = {u["name"]: (u, c) for u, c in zip(meta[0]["universe"], meta[1])}
    if len(core) < 100:
        raise RuntimeError("HL core 只回 %d 個市場 —— 讀不到不是『沒有』"
                           % len(core))
    df["hl_vol"] = [float(core[c][1].get("dayNtlVlm") or 0) if c in core
                    else np.nan for c in df.index]
    df["hl_lev"] = [core[c][0].get("maxLeverage") if c in core else None
                    for c in df.index]
    return df, (n1, n2, n3)


def verdict(r):
    """五關裡這支管得到的三關 ＋ 對沖腿存在性。未量一律不算過。"""
    import math
    g1 = (not math.isnan(r.net)) and r.net > G1_NET_BPS
    g4 = r["slice"] >= G4_MIN_SLICE_USD
    g5 = r.taker_min >= G5_TAKER_MINORITY_PCT
    hedge = (not math.isnan(r.hl_vol)) and r.hl_vol >= MIN_HL_VOL_USD
    return g1, g4, g5, hedge


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args(argv)

    df, files = build(a.hours)
    print("Lighter tob %d 檔 / HL mid %d 檔 / 成交帶 %d 檔"
          " -> %d 個市場（成交 >= %d 筆）\n"
          % (files[0], files[1], files[2], len(df), MIN_TRADES))
    print("判準：G1 淨 > %.1f bps（Lighter 半價差 − HL 半價差 − 費用 %.2f）"
          "  G4 切片 >= $%.0f  G5 吃單流少數側 >= %.0f%%  ＋ 對沖腿 HL 量 >= $%.0fk"
          % (G1_NET_BPS, FEES_BPS, G4_MIN_SLICE_USD,
             G5_TAKER_MINORITY_PCT, MIN_HL_VOL_USD / 1000))
    print()

    rows = []
    for c, r in df.iterrows():
        g1, g4, g5, hedge = verdict(r)
        rows.append((c, r, g1, g4, g5, hedge, g1 and g4 and g5 and hedge))
    ok = [x for x in rows if x[6]]
    ok.sort(key=lambda x: -x[1].vol)

    print("=== 通過的市場：**%d 個**（共 %d 個）===\n" % (len(ok), len(rows)))
    hdr = ("%-9s %8s %8s %8s %9s %9s %12s %13s"
           % ("市場", "LT半價差", "HL半價差", "淨bps", "切片$", "吃單少數%",
              "LT成交$", "HL日成交$"))
    print(hdr)
    print("-" * len(hdr))
    for c, r, *_ in ok[:a.top]:
        print("%-9s %8.2f %8.2f %8.2f %9.2f %9.1f %12.0f %13.0f"
              % (c, r.lt_hs, r.hl_hs, r.net, r["slice"], r.taker_min,
                 r.vol, r.hl_vol))

    print("\n=== 自曝檢查（答案已知；紅了先查這支，不是查市場）===")
    d = {c: (r, g1, g4, g5, h) for c, r, g1, g4, g5, h, _ in rows}
    checks = []
    if "BTC" in d:
        r = d["BTC"][0]
        checks.append(("C1 BTC 淨值必須 < 0（兩個深簿，扣完費用沒得做）",
                       r.net < 0, "%.2f bps" % r.net))
        checks.append(("C2 BTC 吃單流少數側必須 > 35%（深簿雙向流）",
                       r.taker_min > 35, "%.1f%%" % r.taker_min))
    if "MET" in d:
        r = d["MET"][0]
        checks.append(("C3 MET 必須**過不了** G4（實盤切片 $0.51）",
                       r["slice"] < G4_MIN_SLICE_USD, "$%.2f" % r["slice"]))
        checks.append(("C4 MET 必須**過不了** G5（實盤 121 筆成交全是 SELL）",
                       r.taker_min < G5_TAKER_MINORITY_PCT,
                       "%.1f%%" % r.taker_min))
    if "FIL" in d:
        r = d["FIL"][0]
        checks.append(("C5 FIL 必須**過不了** G1（實盤一小時 0 成交）",
                       r.net <= G1_NET_BPS, "%.2f bps" % r.net))
    allok = True
    for name, passed, val in checks:
        print("  %-52s %-10s %s" % (name, val, "PASS" if passed else "**FAIL**"))
        allok &= passed
    print("  -> %s" % ("儀器可信" if allok
                       else "**先查這支,不要解讀上面的表**"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
