# -*- coding: utf-8 -*-
"""§1.25 缺口 3：讓判決吃得到 149 個配對（2026-09-11）

===========================================================================
為什麼是新檔而不是改 premium_verdict
===========================================================================
`premium_verdict.py` 是 §0.75 的**唯一判決者**，而它的 `PAIRS` 寫死九個。
把 149 個塞進同一張清單會動到兩件不該動的東西：

  1. `arb_premium_verdict.json` 的形狀 —— 下游有 `arb_publish`（寫網站）
     與 `prereg_publish`（數分鐘），改形狀可能安靜地弄壞它們
     （mistake.md 2026-08-29：一份資料兩個讀者只改了一個）
  2. 凍結那九個的數字 —— 它們是判決依據

所以這一支**一行都不改** `premium_verdict.py`：它 import 那支的
`score_pair()`（同一顆計分器、同一套 band/收斂/深度定義），
只是餵不同的配對清單，並寫到**另一個檔**
`results/arb_premium_universe.json`。

===========================================================================
**免費的跨儀器對照**（這是這個設計附帶的好處）
===========================================================================
宇宙裡有幾個配對與凍結那九個**是同一個東西**（同 ticker、同兩個場館），
只是**由兩個互不相干的錄製器分別錄的**：

    凍結  BTC      = HL vs lighter-rh   （engine/logs/BTC/minutes.csv）
    宇宙  BTC@HL-lighter-rh             （logs/universe/BTC@HL-lighter-rh/…）

**兩者的 band 必須收斂到同一個數。** 不收斂就是至少一邊的錄製有問題，
而這種對照我們原本沒有（mistake.md 2026-09-11：`conj_redef` 與
`conj_backtest` 共存兩天，沒有任何東西在比它們）。

本支把重疊配對的兩組數字**並排印出來**。宇宙錄製器才剛上線，
所以現在只能看量級；資料長出來之後這一欄會變成一道真的守衛。

===========================================================================
注意：新配對一律是「期中」不是判決
===========================================================================
`GATE_DAYS = 7`，而宇宙錄製從 2026-09-11 才開始。所以 149 個配對在
九月中以前全部是 `status="interim"`，**它們的數字不得被當成判決引用**。
（而 `cost_model.family_specs()` 今天才修好「兩種 schema 都讀」，
所以期中的配對它也吃得下 —— 那個修正正是為了這一刻。）

    python arblib/premium_universe.py
    python arblib/premium_universe.py --top 20     # 只印帶最寬的前 20 個
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

OUT = ROOT / "results" / "arb_premium_universe.json"
UNI = ROOT / "results" / "arb_universe.json"
FROZEN = ROOT / "results" / "arb_premium_verdict.json"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    import premium_verdict as PV          # 同一顆計分器

    u = json.loads(UNI.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    res = dict(asof_utc=now.strftime("%Y-%m-%d %H:%M"),
               gate_days=PV.GATE_DAYS,
               note=("由 arblib/premium_universe.py 產生。計分器與 §0.75 "
                     "完全相同（import premium_verdict.score_pair），"
                     "只是配對清單不同、輸出到不同的檔。"
                     "GATE_DAYS=%d，所以新配對一律是期中不是判決。"
                     % PV.GATE_DAYS),
               pairs={})

    scored = 0
    for p in u["pairs"]:
        pid = "%s@%s-%s" % (p["ticker"], p["leg_a"], p["leg_b"])
        sub = "universe/%s/minutes.csv" % pid
        # VENUE_KEYS 是費用表用的；宇宙配對要現場補上，否則費用關會用預設值
        PV.VENUE_KEYS[pid] = (p["leg_a"], p["leg_b"])
        r = PV.score_pair(pid, sub, p["leg_a"], p["leg_b"], "§1.25 宇宙", now)
        res["pairs"][pid] = r
        if r.get("status") not in ("missing", "empty"):
            scored += 1
    res["n_pairs"] = len(res["pairs"])
    res["n_scored"] = scored

    # ---------- 排行：帶最寬的（期中，不是判決）----------
    rows = []
    for pid, r in res["pairs"].items():
        i = r.get("interim") or {}
        sd = r.get("sides") or {}
        def band(side):
            if i:
                return ((i.get(side) or {}) or {}).get("band_bps")
            return (((sd.get(side) or {}).get("full") or {}) or {}).get("band_bps")
        bs, bb = band("sell"), band("buy")
        if bs is None and bb is None:
            continue
        best = max([x for x in (bs, bb) if x is not None])
        rows.append((pid, r.get("minutes") or 0, bs, bb, best))
    rows.sort(key=lambda x: -(x[4] or 0))
    print("=== §1.25 宇宙配對（**期中，不是判決** —— 錄製從今天才開始）===")
    print("已評分 %d / %d 個配對\n" % (scored, len(res["pairs"])))
    print("%-28s %8s %10s %10s" % ("配對", "分鐘", "賣側band", "買側band"))
    for pid, mins, bs, bb, _ in rows[:a.top]:
        print("%-28s %8d %10s %10s"
              % (pid, mins,
                 ("%.2f" % bs) if bs is not None else "—",
                 ("%.2f" % bb) if bb is not None else "—"))

    # ---------- 跨儀器對照：與凍結那九個重疊的配對 ----------
    print("\n=== 跨儀器對照（同 ticker、同兩個場館，兩個互不相干的錄製器）===")
    fz = json.loads(FROZEN.read_text(encoding="utf-8")) if FROZEN.exists() else {"pairs": {}}
    vk = PV.VENUE_KEYS
    def fz_band(pid, side):
        p_ = (fz.get("pairs") or {}).get(pid) or {}
        i_ = p_.get("interim") or {}
        if i_:
            return ((i_.get(side) or {}) or {}).get("band_bps")
        return (((p_.get("sides") or {}).get(side) or {}).get("full") or {}).get("band_bps")
    overlaps = []
    for fpid in ("SNDK", "NBIS", "ANTH", "BTC", "HYPE", "ZEC", "NEAR",
                 "GOLD_LL", "NVDA_LL"):
        legs = vk.get(fpid)
        if not legs:
            continue
        # 凍結那九個的 ticker 名與宇宙的 canonical 不一定同字
        cand = [pid for pid in res["pairs"]
                if pid.endswith("@%s-%s" % legs)
                and pid.split("@")[0] in (fpid, fpid.replace("_LL", ""),
                                          {"GOLD_LL": "XAU"}.get(fpid, fpid))]
        for upid in cand:
            overlaps.append((fpid, upid))
    if not overlaps:
        print("  （沒有重疊 —— 凍結那九個有四個在 IO 上，而 IO 不在宇宙裡）")
    else:
        print("%-10s %-28s %11s %11s %11s %11s"
              % ("凍結", "宇宙", "凍結賣", "宇宙賣", "凍結買", "宇宙買"))
        for fpid, upid in overlaps:
            r = res["pairs"][upid]
            i = r.get("interim") or {}
            ub_s = ((i.get("sell") or {}) or {}).get("band_bps")
            ub_b = ((i.get("buy") or {}) or {}).get("band_bps")
            f = lambda x: ("%.2f" % x) if x is not None else "—"
            print("%-10s %-28s %11s %11s %11s %11s"
                  % (fpid, upid, f(fz_band(fpid, "sell")), f(ub_s),
                     f(fz_band(fpid, "buy")), f(ub_b)))
        print("  **兩邊必須收斂到同一個數。** 宇宙那側才錄幾分鐘，現在只能看量級；")
        print("  資料長出來之後這張表就是一道真的守衛。")
    res["overlaps"] = [{"frozen": a_, "universe": b_} for a_, b_ in overlaps]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\nwritten -> " + str(OUT))
    print("**凍結的 arb_premium_verdict.json 一個位元都沒有被動到。**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
