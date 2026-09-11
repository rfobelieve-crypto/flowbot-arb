# -*- coding: utf-8 -*-
"""§1.25 宇宙凍結：我們要錄的是「每一個」標的，不是我們挑的那幾個（2026-09-11）

===========================================================================
為什麼這支存在
===========================================================================
2026-09-11 同一天，**三條線各自獨立撞到同一個綁束**：

    §1.20 套利容量   事件數不夠（只錄 8 個配對）
    §1.23 MFT        橫斷面只有 11 個名字（他用 50），SE 幾乎全來自它
    §1.24 耐心       事件數不夠,所以「挑慢的做」退化成「少做」

而 Quant Arb〈How to level up your arb game〉第 5 條逐字描述我們：

> **「把交易所上每一個標的都交易。** 這聽起來很平凡，但人們常常只手動挑
> 標的，**而沒有真正交易一切所需的自動化**。」

我們的形狀正是那個：**一個配對一個行程、一份 yaml、一支 .bat**，九份。

===========================================================================
**凍結的規則（寫在看任何 band 之前）**
===========================================================================
    錄製宇宙 = 每一個「在 >= 2 個我們能接 WS 的場館上都有」的 canonical ticker
               **沒有排名、沒有門檻、沒有排除。**

**這條規則刻意不含任何篩選。** 理由不是偷懶，是**偏誤**：
任何「先看哪些配對帶比較寬再決定錄哪些」的做法，就是 §0.92 判掉 C/D 變體的
同一種錯（事後挑統計量最好的子集），而且它會讓後面所有的樣本外都失效。
**規則先凍結，資料後產生** —— 順序反了就沒有樣本外可言。

**唯一的排除是機械性的、與績效無關的**：
  * 場館自己說它下市／不可交易（`is_active` 之類的欄位）
  * 我們連不上它的 WS（見下面的 `WS_VENUES`）

===========================================================================
標的對齊：**不寫第二份**
===========================================================================
`engine/tools/scanner.py` 已經有整套：`hl_universe()`、`lighter_universe()`、
canonical ticker 的正規化與別名政策（而那個政策是刻意保守的——
SPY/SP500、XAUT/PAXG **不互為別名**，因為「同一個標的」不等於「同一個合約」）。

本支**直接 import 它**。凡是未來要改標的對齊，改 scanner 一處。

===========================================================================
可行性（實測，不是推論）
===========================================================================
    HL        一條 WS 訂 **234 個 l2Book（整個主場）-> 234 個全部回資料，100%**
              （2026-09-11 實測，見 TODO §1.25）
    lighter / lighter-rh   待測

**這個數字是整個計畫的前提**：如果一條連線只能訂幾十個，加寬就得改架構
（多連線、分片），而不是改一個常數。HL 既然吃得下整個主場，**HL 那一側不是
瓶頸**。

    python arblib/universe.py            # 印出宇宙並寫 results/arb_universe.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "engine" / "tools"))
OUT = ROOT / "results" / "arb_universe.json"

# 我們能接 WS 的場館（= 能做分鐘級錄製的）。CEX 腿走 REST 掃描器，
# 不在這份宇宙裡 —— 它們的用途是當**第三方參照**（§1.21 的基差就是那樣量的）。
WS_VENUES = ("HL", "lighter", "lighter-rh")

# scanner 的場館 id -> 顯示名。scanner.NAME 是真相源，這裡只是反查用。
MIN_VENUES = 2


def _scanner():
    import scanner as SC
    return SC


def build() -> dict:
    SC = _scanner()
    # scanner 的每個 *_universe() 回傳 {canonical_ticker: leg_meta}
    per_venue = {}
    errs = {}
    for vid in WS_VENUES:
        try:
            if vid == "HL":
                per_venue[vid] = SC.hl_universe()
            else:
                per_venue[vid] = SC.lighter_universe(vid)
        except Exception as e:                                  # noqa: BLE001
            errs[vid] = repr(e)
            per_venue[vid] = {}
    # canonical ticker -> 它出現在哪些場館
    where = {}
    for vid, uni in per_venue.items():
        for canon in uni:
            where.setdefault(canon, []).append(vid)
    eligible = {c: sorted(v) for c, v in where.items() if len(v) >= MIN_VENUES}
    # 配對 = 同一個 canonical ticker 的任兩個場館
    pairs = []
    for c, vs in sorted(eligible.items()):
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                pairs.append({"ticker": c, "leg_a": vs[i], "leg_b": vs[j]})
    # 原生代號一起存：HL 要 `coin`、lighter 要 `market_id`，
    # 錄製器必須拿得到它們才訂得到（scanner 的 leg_meta 就帶著）。
    native = {}
    for vid in WS_VENUES:
        native[vid] = {c: per_venue[vid][c] for c in eligible
                       if vid in eligible[c] and c in per_venue[vid]}
    subs = {vid: sorted(native[vid]) for vid in WS_VENUES}
    return dict(
        asof=time.strftime("%Y-%m-%d %H:%M:%S"),
        rule=("每一個在 >= %d 個可接 WS 場館上都有的 canonical ticker；"
              "無排名、無門檻、無績效相關的排除" % MIN_VENUES),
        ws_venues=list(WS_VENUES),
        venue_counts={v: len(u) for v, u in per_venue.items()},
        errors=errs,
        n_tickers=len(eligible),
        n_pairs=len(pairs),
        subscriptions={v: len(s) for v, s in subs.items()},
        tickers=eligible, pairs=pairs, subs=subs, native=native)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    u = build()
    print("=== §1.25 錄製宇宙（凍結規則）===")
    print("  " + u["rule"] + "\n")
    print("各場館的標的數：")
    for v, n in u["venue_counts"].items():
        print("  %-12s %4d%s" % (v, n, ("   **抓不到：" + u["errors"][v] + "**")
                                 if v in u["errors"] else ""))
    if u["errors"]:
        print("\n**有場館抓不到清單 —— 宇宙不完整，不要拿它去凍結。**")
    print("\n>> 至少在 %d 個場館上都有的 ticker = **%d 個**"
          % (MIN_VENUES, u["n_tickers"]))
    print(">> 配對數 = **%d**（現在錄 8 個）" % u["n_pairs"])
    print(">> 需要的 WS 訂閱數：%s = **%d**"
          % (" + ".join("%s %d" % (k, v) for k, v in u["subscriptions"].items()),
             sum(u["subscriptions"].values())))
    by = {}
    for p in u["pairs"]:
        by["%s-%s" % (p["leg_a"], p["leg_b"])] = by.get(
            "%s-%s" % (p["leg_a"], p["leg_b"]), 0) + 1
    print("\n逐場館對：")
    for k, n in sorted(by.items(), key=lambda x: -x[1]):
        print("  %-24s %3d" % (k, n))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(u, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print("\nwritten -> " + str(OUT))
    return 0 if not u["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
