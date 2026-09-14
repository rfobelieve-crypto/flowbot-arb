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

    G1 淨值   **兩側較小的那一邊**                    <- 兩邊的簿口錄製
              賣側 = LT半價差 + 基差 − HL半價差 − 費用
              買側 = LT半價差 − 基差 − HL半價差 − 費用
    G4 切片   Lighter 逐筆成交的中位金額             <- 成交帶
    G5 雙向   吃單流的少數側                          <- 成交帶的 is_maker_ask

===========================================================================
G1 從單側改成兩側（2026-09-14），而這也是實盤打出來的
===========================================================================
舊版是 `net = lt_hs - hl_hs - fees`，**沒有基差項** —— 它隱含假設兩所的
中價對齊。實際上 Lighter 與 HL 之間有持續的基差 b，於是真實的兩側相差 2b，
而舊 G1 算的正好是兩者的**平均**：那兩側都不是。

引擎只會做有利的那一側，庫存就單向堆到上限然後停住：

    XPL   舊G1 +3.99（過關）  賣側 +14.40  買側 **-6.42**  基差 +10.41
          實盤：報價側別 **332 : 0**、部位 $58.7/$60、十分鐘 95 次
          `blocked by position caps`
    AERO  舊G1 +3.14（過關）  賣側 +11.54  買側 **-5.26**  基差  +8.40
          實盤：122 次報價 2 筆成交

**而 G5 對這個病完全是瞎的**：XPL 的吃單流少數側是 46.0%，流量非常兩側。
兩者不矛盾 —— G5 量「誰在吃單」，而我們報哪一側是由**基差的正負號**決定的。

連帶的負結果：C6（AERO 在線時間 < 50%）從註冊起就一直紅，因為當初用
「在線時間」去解釋 AERO 的失敗。現在知道那個解釋是錯的，真正的原因是基差。
C6 **刻意留著而且留成紅的**，它是「G7 對 AERO 沒有分辨力」的證據。

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
# G7（2026-09-14,AERO 教的）：**中位數會騙人。**
# AERO 的 24 小時中位半價差是 10.57 bps -> 過 G1,於是上線。
# 一小時後它塌到 5.31 -> 淨 -0.90,引擎正確地停止報價,一小時 0 成交。
# 中位數只說「一半的時間在線上」,而做市是**持續**的生意 —— 我們要的是
# 「這個市場有多少時間是可做的」,那才決定樣本累積得多快。
G7_UPTIME_PCT = 50.0
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
    # 多取的欄位（2026-09-14）：兩邊的**中價**與時戳。基差要靠它們算，
    # 而基差是 G1 一直缺的那一項 —— 見下面 df["basis"] 的說明。
    lt, n1 = _read(["lighter", "tob"],
                   ["rx_ms", "coin", "bid", "ask"], n)
    hl, n2 = _read(["hl", "mid"], ["ts", "coin", "spread_bps", "mid"], n * 2)
    tp, n3 = _read(["lighter", "trades"], ["coin", "usd", "is_maker_ask"], n)

    lt = lt[(lt.bid > 0) & (lt.ask > lt.bid)].copy()
    lt["hs"] = (lt.ask - lt.bid) / (lt.ask + lt.bid) * 1e4
    lt["lt_mid"] = (lt.bid + lt.ask) / 2.0
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
    # G7：**逐筆**算淨邊際,再看有多少比例在門檻之上。
    # 對沖腿的半價差用該幣的中位（HL 的簿口穩定得多,而且我們沒有逐筆對齊
    # 的資料 —— 用中位是保守方向：HL 價差被低估會讓我們的淨值被高估）。
    hlmed = df["hl_hs"].to_dict()
    up = {}
    for c, x in lt.groupby("coin"):
        h = hlmed.get(c)
        if h is None or len(x) < 200:
            continue
        net_series = x["hs"].values - h - FEES_BPS
        up[c] = float((net_series > G1_NET_BPS).mean() * 100.0)
    df["uptime"] = [up.get(c, float("nan")) for c in df.index]
    df = df[df.trades >= MIN_TRADES].dropna(subset=["lt_hs", "slice"])
    df["taker_min"] = np.minimum(df.taker_buy, 100.0 - df.taker_buy)
    # 對沖腿讀不到半價差時**不要猜** —— 那一列標成未量，不進判定。
    df["net"] = df.lt_hs - df.hl_hs - FEES_BPS

    # ---------------------------------------------------------------- 基差
    # **舊 G1 是單側的,而那是 2026-09-14 XPL 死掉的原因。**
    #
    # `net = lt_hs - hl_hs - fees` 隱含假設兩所的中價對齊。實際上 Lighter
    # 與 HL 之間有持續的基差 b,於是真實的兩側是
    #
    #     賣側 = lt_hs + b - hl_hs - fees
    #     買側 = lt_hs - b - hl_hs - fees
    #
    # 舊 G1 算的正好是兩者的**平均**,而那兩側都不是。引擎只會做有利的那一
    # 側,庫存就單向堆到上限然後停住。XPL 實測:
    #
    #     舊 G1 +1.48 bps（過關）   賣側 +12.34   買側 **-9.38**
    #     實盤報價側別 332 : 0,部位 $58.7/$60,十分鐘 95 次 blocked
    #
    # 而 G5（吃單流少數側）那時是 **46.0%** —— 流量非常兩側,所以 G5 對這個
    # 病完全是瞎的。兩者不矛盾:G5 量「誰在吃單」,而我們報哪一側是由**基差
    # 的正負號**決定的。
    #
    # 對齊用 60 秒桶（HL 是 60 秒取樣,Lighter tob 更密）。不用 merge_asof
    # 是因為 190 幣各跑一次太慢,而基差取中位對 60 秒內的錯位不敏感。
    BUCKET_MS = 60_000
    lt_b = lt[["coin", "rx_ms", "lt_mid"]].copy()
    lt_b["b"] = (lt_b.rx_ms // BUCKET_MS).astype("int64")
    lt_b = lt_b.groupby(["coin", "b"], as_index=False)["lt_mid"].median()
    hl_b = hl[["coin", "ts", "mid"]].copy()
    hl_b["b"] = (hl_b.ts // BUCKET_MS).astype("int64")
    hl_b = hl_b.groupby(["coin", "b"], as_index=False)["mid"].median()
    j = lt_b.merge(hl_b, on=["coin", "b"], how="inner")
    j = j[(j.lt_mid > 0) & (j["mid"] > 0)]
    j["bps"] = (j["mid"] - j.lt_mid) / j.lt_mid * 1e4
    bas = j.groupby("coin")["bps"].median()
    nb = j.groupby("coin").size()
    # 樣本太少的不要猜 —— 未量比猜錯好（mistake.md 2026-09-14）。
    bas = bas[nb >= 30]
    df["basis"] = [float(bas[c]) if c in bas.index else float("nan")
                   for c in df.index]
    df["net_sell"] = df.lt_hs + df.basis - df.hl_hs - FEES_BPS
    df["net_buy"] = df.lt_hs - df.basis - df.hl_hs - FEES_BPS
    df["net_min"] = np.minimum(df.net_sell, df.net_buy)

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
    """這支管得到的四關 ＋ 對沖腿存在性。未量一律不算過。

    **G1 自 2026-09-14 起改判兩側較小的那一邊**（`net_min`）。舊的單側
    `net` 仍然算出來並印在表上,因為它被引用過,不可以悄悄消失 ——
    兩欄擺在一起本身就是這個改動的證據。
    """
    import math
    g1 = (not math.isnan(r.net_min)) and r.net_min > G1_NET_BPS
    g4 = r["slice"] >= G4_MIN_SLICE_USD
    g5 = r.taker_min >= G5_TAKER_MINORITY_PCT
    g7 = (not math.isnan(r.uptime)) and r.uptime >= G7_UPTIME_PCT
    hedge = (not math.isnan(r.hl_vol)) and r.hl_vol >= MIN_HL_VOL_USD
    return g1, g4, g5, g7, hedge


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args(argv)

    df, files = build(a.hours)
    print("Lighter tob %d 檔 / HL mid %d 檔 / 成交帶 %d 檔"
          " -> %d 個市場（成交 >= %d 筆）\n"
          % (files[0], files[1], files[2], len(df), MIN_TRADES))
    print("判準：**G1 兩側較小的那一邊** 淨 > %.1f bps  G4 切片 >= $%.0f"
          "  G5 吃單流少數側 >= %.0f%%  **G7 在線時間 >= %.0f%%**"
          "  ＋ 對沖腿 HL 量 >= $%.0fk"
          % (G1_NET_BPS, G4_MIN_SLICE_USD, G5_TAKER_MINORITY_PCT,
             G7_UPTIME_PCT, MIN_HL_VOL_USD / 1000))
    print("  （2026-09-14 起 G1 判 net_min = lt_hs − |基差| − hl_hs − 費用。"
          "舊的單側值仍印在「舊G1」欄）")
    print()

    rows = []
    for c, r in df.iterrows():
        g1, g4, g5, g7, hedge = verdict(r)
        rows.append((c, r, g1, g4, g5, g7, hedge,
                     g1 and g4 and g5 and g7 and hedge))
    ok = [x for x in rows if x[7]]
    ok.sort(key=lambda x: -x[1].vol)

    print("=== 通過的市場：**%d 個**（共 %d 個）===\n" % (len(ok), len(rows)))
    hdr = ("%-9s %8s %7s %8s %8s %7s %8s %9s %11s"
           % ("市場", "LT半價差", "基差", "賣側", "買側", "舊G1",
              "在線%", "切片$", "吃單少數%"))
    print(hdr)
    print("-" * len(hdr))
    for c, r, *_ in ok[:a.top]:
        print("%-9s %8.2f %7.2f %8.2f %8.2f %7.2f %8.1f %9.2f %11.1f"
              % (c, r.lt_hs, r.basis, r.net_sell, r.net_buy, r.net,
                 r.uptime, r["slice"], r.taker_min))
    if not ok:
        print("  （沒有市場通過。這不是錯誤 —— 見下面的自曝檢查）")

    print("\n=== 舊 G1（單側）會放行、而兩側檢驗擋下來的 ===")
    shown = 0
    for c, r, *_ in sorted(rows, key=lambda x: -x[1].vol):
        import math
        if math.isnan(r.net_min):
            continue
        if r.net > G1_NET_BPS and r.net_min <= G1_NET_BPS:
            print("  %-9s 舊G1 %+6.2f -> 賣 %+7.2f / 買 %+7.2f"
                  "（基差 %+7.2f）" % (c, r.net, r.net_sell, r.net_buy,
                                      r.basis))
            shown += 1
            if shown >= 12:
                break
    if not shown:
        print("  （沒有 —— 那代表基差這一項在這批資料上沒有分辨力,先查它）")

    print("\n=== 自曝檢查（答案已知；紅了先查這支，不是查市場）===")
    d = {c: (r,) for c, r, *_ in rows}
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
    if "AERO" in d:
        # **C6 從 2026-09-14 起是一個已知會紅的檢查,而它紅得有意義。**
        #
        # 它原本的用意:AERO 的中位半價差 10.57 過了舊 G1,於是我們上線 ——
        # 然後一小時幾乎 0 成交。當時我們用「在線時間」去解釋,所以 C6 要求
        # AERO 的在線時間必須 < 50%。它一直是 52% 左右,也就是
        # **G7 從來沒有解釋過 AERO**。
        #
        # 當天我刻意不去調那個門檻讓它變綠（把儀器擬合到答案正是它要擋的
        # 事）。現在有了兩側檢驗,才知道真正的原因是**基差**:AERO 的基差
        # +8.40,賣側 +11.54 而買側 **-5.26** —— 單側市場。見 C9。
        #
        # 所以 C6 留著,而且刻意留成紅的:它是「G7 對 AERO 沒有分辨力」這個
        # 負結果的證據。要拿掉 G7 是另一個決定,不在這個改動裡。
        r = d["AERO"][0]
        checks.append(("C6 AERO 的在線時間必須 < %.0f%%"
                       "（**已知紅**：G7 從來沒解釋過 AERO，見 C9）"
                       % G7_UPTIME_PCT,
                       (r.uptime == r.uptime) and r.uptime < G7_UPTIME_PCT,
                       "%.1f%%" % r.uptime))
        checks.append(("C9 AERO 的買側必須 < 0（兩側檢驗才是 AERO 的解釋）",
                       (r.net_buy == r.net_buy) and r.net_buy < 0,
                       "%.2f bps" % r.net_buy))
    if "XPL" in d:
        # **C7/C8 是兩側 G1 自己的反向證明。** 2026-09-14 XPL 過了舊 G1
        # （單側 +1.48）而且過了 G5（吃單流少數側 46.0%,流量非常兩側）,
        # 於是我們上線 —— 然後報價側別 332:0、部位 $58.7/$60、十分鐘 95 次
        # blocked by position caps。
        # 如果兩側檢驗有分辨力,XPL 的買側必須是負的,而且舊 G1 必須是正的
        # （後者證明**是這個改動擋下它的**,不是它本來就會被別的關擋掉）。
        r = d["XPL"][0]
        checks.append(("C7 XPL 的買側必須 < 0（實盤報價側別 332:0）",
                       (r.net_buy == r.net_buy) and r.net_buy < 0,
                       "%.2f bps" % r.net_buy))
        checks.append(("C8 XPL 的**舊** G1 必須 > %.1f（證明是這個改動擋下它）"
                       % G1_NET_BPS,
                       r.net > G1_NET_BPS, "%.2f bps" % r.net))
    # 「已知會紅」與「意外的紅」要分開印。混在一起的話,一個永遠紅的檢查會
    # 訓練人忽略這整個頻道 —— 那正是 transition-only 告警要避免的事
    # （mistake.md 2026-09-03：一條永遠紅的守衛跟壞掉的燈一樣沒用）。
    # **但已知紅不可以被靜音**:它留在表上,只是不觸發「不要解讀上面的表」。
    KNOWN_RED = {"C6"}
    surprises = 0
    for name, passed, val in checks:
        tag = name.split()[0]
        if passed:
            mark = "PASS"
        elif tag in KNOWN_RED:
            mark = "**FAIL（已知）**"
        else:
            mark = "**FAIL**"
            surprises += 1
        print("  %-58s %-10s %s" % (name, val, mark))
    if surprises:
        print("  -> **先查這支,不要解讀上面的表**（%d 個意外的紅）" % surprises)
    else:
        print("  -> 儀器可信（%d 個已知的紅,理由寫在原始碼註解裡）"
              % sum(1 for n, p, _ in checks
                    if not p and n.split()[0] in KNOWN_RED))
    return 0 if surprises == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
