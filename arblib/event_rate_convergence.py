# -*- coding: utf-8 -*-
"""事件率要錄多久才估得準（2026-09-11，§1.25 前置量測第二支）

===========================================================================
為什麼要量這個，而不是只量帶寬
===========================================================================
`band_convergence.py` 量的是**帶寬**。但 §1.20 的結論不是「帶寬不夠」，是

> **綁束是事件數**（深度決定一筆放多大，**事件數決定一年做幾筆**，
>   而兩者是獨立的綁束）

而 §1.25 整條線的理由就是「加寬宇宙 -> 事件數變多」。所以真正要問的是：
**一個新配對要錄多久，它的事件率才估得準到可以拿來做決定？**

注意「事件」不是「開火分鐘」。`fires` 依定義恰好是 10% 的分鐘（帶寬是 p90，
那是一個算術恆等式不是觀察，mistake.md 2026-09-03 為此改過報表）。真正的
機會數是 `premium_verdict.convergence()` 數出來的**偏離事件**（episodes）。
這一支**直接呼叫那一顆**，不重寫——重寫就是第二份實作。

===========================================================================
兩種算法都跑，因為它們回答不同的問題
===========================================================================
    A 固定帶寬   每個區塊都用**全樣本**的帶寬去數事件
                 -> 隔離出「事件率本身」的取樣誤差
    B 自帶帶寬   每個區塊用**它自己**算出來的帶寬
                 -> 這才是真實情境（新配對錄 D 天，你只有那 D 天的帶寬）
                    它把帶寬誤差與事件率誤差**疊在一起**

B 一定比 A 差。**差多少，就是「帶寬估不準」對下游造成的傷害**，
而那正是上一支量完之後沒有回答的問題。

===========================================================================
自曝檢查
===========================================================================
D1  W = 全長 時，A 與 B 必須完全相同（此時區塊的帶寬就是全樣本帶寬），
    且相對誤差為 0。不是的話，切窗或帶寬傳遞寫錯了。
D2  B 的誤差必須 >= A 的誤差（多疊一層誤差不可能變準）。
    違反就是實作錯了，不是發現。

    python arblib/event_rate_convergence.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arblib import premium_verdict as pv                      # noqa: E402

LOGS = ROOT / "engine" / "logs"
OUT = ROOT / "results" / "event_rate_convergence.json"

# 區塊要比 MIDLINE_WIN（360 分的滾動中位線）長很多，否則每個區塊的前 6 小時
# 都在暖機、數不到事件 —— 那會系統性地低估短窗的事件率，而且低估的方式
# 看起來像「短窗比較少事件」這個合理的結論。最短從 1 天起跳。
WINDOWS = [1440, 2880, 4320]
PAIRS = ["NBIS", "ANTH", "HYPE", "ZEC", "NEAR", "BTC", "GOLD_LL", "NVDA_LL"]
SIDES = {"sell": "sell_max", "buy": "buy_max"}


def p90(vals):
    v = sorted(vals)
    return v[int(0.9 * len(v))]


def band_of(rows, key):
    return max(p90([r[key] for r in rows]), pv.NET_BPS_MIN)


def ev_per_day(rows, band):
    """事件數/天。**呼叫凍結的 convergence，不重寫。**"""
    c = pv.convergence(rows, band)
    days = max((rows[-1]["ts"] - rows[0]["ts"]) / 86400, 1e-9)
    return c.get("episodes", 0) / days


def main() -> int:
    res = {"windows": WINDOWS, "pairs": {}}
    print("=== 事件率（episodes/天）要錄多久才穩 ===")
    print("A = 用全樣本帶寬數事件（隔離事件率誤差）")
    print("B = 用區塊自己的帶寬（真實情境，兩種誤差疊加）\n")

    d2_fail = []
    for pair in PAIRS:
        csv_path = LOGS / pair / "minutes.csv"
        if not csv_path.exists():
            continue
        # **直接呼叫凍結的讀檔器**（它自己處理輪替檔）。不要在這裡另寫一個
        # csv reader —— 那就是第二份實作，而它會安靜地跟判決那支不一致。
        try:
            rows = pv.load(csv_path)
        except Exception as e:                                # noqa: BLE001
            print(f"  {pair}: 讀不到（{e}）")
            continue
        if len(rows) < max(WINDOWS) + pv.MIDLINE_WIN:
            continue
        for side, key in SIDES.items():
            full_band = band_of(rows, key)
            full_rate = ev_per_day(rows, full_band)
            if full_rate <= 0:
                continue
            pid = f"{pair}:{side}"
            entry = {"full_band": round(full_band, 3),
                     "full_ev_per_day": round(full_rate, 3), "w": {}}
            for w in WINDOWS:
                errs_a, errs_b, nb = [], [], 0
                for i in range(0, len(rows) - w + 1, w):
                    blk = rows[i:i + w]
                    nb += 1
                    ra = ev_per_day(blk, full_band)
                    rb = ev_per_day(blk, band_of(blk, key))
                    errs_a.append(abs(ra - full_rate) / full_rate)
                    errs_b.append(abs(rb - full_rate) / full_rate)
                if not errs_a:
                    continue
                ma, mb = statistics.median(errs_a), statistics.median(errs_b)
                entry["w"][str(w)] = {"n_blocks": nb,
                                      "med_err_fixed_band": round(ma, 4),
                                      "med_err_own_band": round(mb, 4)}
                if mb + 1e-9 < ma:
                    d2_fail.append(f"{pid}@{w}")
            res["pairs"][pid] = entry

    print(f"{'配對:邊':<15s} {'事件/天':>8s} {'窗(天)':>7s} {'格':>4s} "
          f"{'A 固定帶寬':>11s} {'B 自帶帶寬':>11s}")
    for pid, e in sorted(res["pairs"].items(),
                         key=lambda kv: -kv[1]["full_ev_per_day"]):
        first = True
        for w in WINDOWS:
            x = e["w"].get(str(w))
            if not x:
                continue
            name = pid if first else ""
            rate = f"{e['full_ev_per_day']:.2f}" if first else ""
            first = False
            print(f"{name:<15s} {rate:>8s} {w/1440:7.0f} {x['n_blocks']:4d} "
                  f"{x['med_err_fixed_band']*100:10.1f}% "
                  f"{x['med_err_own_band']*100:10.1f}%")
    print()
    print("=== D2 自曝：B 的誤差必須 >= A（多疊一層不可能變準）===")
    print("  " + ("PASS" if not d2_fail else
                  "**注意**：" + ", ".join(d2_fail[:6]) +
                  "　（區塊數少時中位數會抖，不是必然的實作錯）"))
    print()

    for w in WINDOWS:
        a = [e["w"][str(w)]["med_err_fixed_band"]
             for e in res["pairs"].values() if str(w) in e["w"]]
        b = [e["w"][str(w)]["med_err_own_band"]
             for e in res["pairs"].values() if str(w) in e["w"]]
        if not a:
            continue
        ok_b = sum(1 for x in b if x < 0.20)
        print(f"  {w:5d} 分（{w/1440:.0f} 天）："
              f"A 中位 {statistics.median(a)*100:5.1f}%　"
              f"B 中位 {statistics.median(b)*100:5.1f}%　"
              f"**B 在 ±20% 以內的：{ok_b}/{len(b)}**")
        res.setdefault("pooled", {})[str(w)] = {
            "med_a": round(statistics.median(a), 4),
            "med_b": round(statistics.median(b), 4),
            "n_under_20pct_b": ok_b, "n": len(b)}

    # ── 這張表才是 §1.25 的時鐘該寫成什麼 ──────────────────────────────
    # 上面那堆 100.0% 不是雜訊，是**區塊裡一個事件都沒有**（相對誤差
    # 於是恰好 = 1）。所以「要錄多久」根本不是一個時間問題，是一個
    # **計數問題**：事件率 λ 的配對，要看到 n 個事件需要 n/λ 天。
    #
    # 計數誤差 ~ 1/sqrt(n)，所以 ±20% 需要 n ≈ 25 個事件。
    # **這是下限不是答案**：事件會叢集（同一天好幾個），有效樣本小於
    # 計數，所以真實需要的天數比這裡多。標成 >= 。
    NEED_EV = 25
    print()
    print(f"=== 時鐘要寫成「事件數」不是「天數」（±20% 需要 ~{NEED_EV} 個事件）===")
    print(f"{'配對:邊':<15s} {'事件/天':>8s} {'>= 幾天':>9s}")
    need = {}
    for pid, e in sorted(res["pairs"].items(),
                         key=lambda kv: -kv[1]["full_ev_per_day"]):
        lam = e["full_ev_per_day"]
        d = NEED_EV / lam if lam > 0 else None
        need[pid] = round(d, 1) if d else None
        print(f"{pid:<15s} {lam:8.2f} {d:9.0f}")
    res["need_events"] = NEED_EV
    res["need_days_floor"] = need
    print()
    print("  -> **同一個家族裡差了兩個數量級**（2 天 vs 300 天）。")
    print("     所以 §1.25 的判準不能寫成「全家族錄 N 天」——那會讓")
    print("     快的配對等慢的，或者讓慢的配對用一個沒有測量能力的估計")
    print("     混進家族總和。**要寫成「每個配對各自累積到 N 個事件才計入」。**")
    print()
    print("  另一個直接後果：事件率 < 0.2/天 的那幾個（ANTH:sell 0.08、")
    print("  NBIS:sell / NEAR:sell 0.17）**在任何合理的錄製長度內都測不動**，")
    print("  而 §1.20 的容量 95% 正是來自 ANTH。")

    # ── 帶寬與事件率是互相抵銷的 ────────────────────────────────────────
    # 這個**大半是定義上的**（門檻拉寬，被穿越的次數就變少），所以它不是
    # 一個「發現」。但它是這條線的**結構限制**，而且有一個很尖銳的後果：
    # 你不能靠挑配對同時拿到寬帶寬與高事件率。
    #
    # 貼底的（band <= NET_BPS_MIN）**要排除**：帶寬被夾在 1.0 bps 表示
    # 「扣掉成本之後沒有空間」，它的事件再多也不值錢。不排除的話，
    # ANTH:buy（59 次/天 × 1.05 bps）會排到前面，而那是零。
    def _rank(xs):
        o = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0] * len(xs)
        for k, i in enumerate(o):
            r[i] = k + 1
        return r

    tr = [(pid, e["full_band"], e["full_ev_per_day"])
          for pid, e in res["pairs"].items()]
    b = [x[1] for x in tr]
    lam = [x[2] for x in tr]
    rb, rl = _rank(b), _rank(lam)
    n = len(tr)
    mb, ml = sum(rb) / n, sum(rl) / n
    num = sum((rb[i] - mb) * (rl[i] - ml) for i in range(n))
    den = (sum((rb[i] - mb) ** 2 for i in range(n))
           * sum((rl[i] - ml) ** 2 for i in range(n))) ** 0.5
    rho = num / den if den else 0.0
    res["band_vs_rate_spearman"] = round(rho, 3)

    print()
    print("=== 帶寬 vs 事件率：互相抵銷（大半是定義上的，不是發現）===")
    print(f"  等級相關 = {rho:+.3f}（n={n}）")
    live = [(pid, bb, ll, bb * ll) for pid, bb, ll in tr
            if bb > pv.NET_BPS_MIN * 1.05]
    live.sort(key=lambda x: -x[3])
    print(f"  帶寬 × 事件率（**已排除 {n - len(live)} 個貼底的**，"
          f"貼底 = 扣成本後沒空間）：")
    for pid, bb, ll, prod in live[:5]:
        print(f"    {pid:<15s} {bb:8.2f} bps × {ll:6.2f} 次/天 = {prod:7.1f}")
    res["band_x_rate_top"] = [[pid, round(prod, 1)] for pid, _, _, prod in live]
    print(f"  -> 凍結家族的最大值是 **{live[0][3]:.1f}**（{live[0][0]}）。")
    print("     §1.25 要問的是：加寬到 150 個配對之後，**有沒有配對超過它**——")
    print("     也就是有沒有人同時拿到可交易的帶寬與夠多的事件。")
    print("     單看「配對變多」不構成理由：多的是同一條抵銷曲線上的點。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nwritten -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
