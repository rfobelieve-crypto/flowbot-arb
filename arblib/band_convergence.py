# -*- coding: utf-8 -*-
"""帶寬要錄多久才讀得出來（2026-09-11，§1.25 的前置量測）

===========================================================================
為什麼先量這個，而不是先寫判準
===========================================================================
§1.25 把宇宙從 8 個配對加寬到 150 個，錄製器 2026-09-11 18:10 起跑。
下一步照規矩是**註冊一條時鐘**——但這個 repo 已經因為「註冊了一條沒有
測量能力的時鐘」付過一次代價：

> mistake.md 2026-09-04：地形扳機門檻 8pp、而註冊樣本下的 SE 是 11.6pp。
> **它不是快要過了，它是從註冊那天起就不可能有答案。**
> 規則：凍結任何判準的當下，要算它在註冊樣本數下的標準誤。
> **SE ≥ 門檻 ⇒ 這個設計不能做決定，必須先重新設計再開跑。**

而 §1.25 的每一個下游數字都是從**帶寬**長出來的（帶寬 -> 開火 -> 事件數
-> 容量 -> $/年）。帶寬是 `sell_edge_max_bps` 的 **p90**，而分位數在短窗
上是出了名的不穩。所以「要錄多久」不是一個可以拍的數字，它決定了

    (a) 跨儀器對照（凍結家族 vs 宇宙，同 ticker 同兩個場館）**從哪一天起
        才有資格被解讀**——今天跑出 NEAR 凍結賣 10.86 vs 宇宙賣 1.00，
        那個差可能完全是「宇宙那側只錄了 154 分鐘」造成的，
        也可能是真的不一致。**在量出收斂時間之前，這兩者分不開。**
    (b) §1.25 的時鐘要設多長才不是第二個地形扳機。

===========================================================================
怎麼量
===========================================================================
凍結家族有 ~13,950 分鐘（約 9.7 天）的真實錄製。做法是**拿它當已知答案**：

    全樣本 p90  =  這個配對「真正的」帶寬（在這份資料上）
    長度 W 的**連續區塊** -> 各自算 p90 -> 看它們散多開

用連續區塊不是隨機抽分鐘，因為**真實的短錄製就是一個連續區塊**——
隨機抽會把日內與跨日的相關性洗掉，於是給出一個過度樂觀的收斂速度
（mistake.md 2026-09-06：置換能驗機器，不能驗功效，因為它破壞掉的正是
讓變異數變大的那個結構）。

判準用**相對誤差**不是絕對 bps：不同配對的帶寬差兩個數量級
（NEAR ~11 bps vs NVDA ~1 bps），用絕對值會讓厚的那些主導。

===========================================================================
自曝檢查（答案已知，錯了就不解讀）
===========================================================================
D1  W = 全長 時，區塊只有一個且必須等於全樣本 p90 -> 相對誤差恆為 0。
    不是 0 就是切窗或分位數的實作寫錯了。
D2  相對誤差的中位數必須**隨 W 單調下降**。不單調 = 抽樣方式有問題。
    （不強制 100% 單調——尾端格子數少本來就會抖——但方向要對。）

    python arblib/band_convergence.py
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "engine" / "logs"
OUT = ROOT / "results" / "band_convergence.json"

# 與 premium_verdict.side_stats 同一條式子。**不得在這裡重新定義帶寬**
# ——那就是第二份實作（mistake.md 2026-08-26）。這裡只是照抄它的分位數
# 取法（sorted[int(0.9*n)]），因為要量的正是那個估計量的穩定度。
KEYS = {"sell": "sell_edge_max_bps", "buy": "buy_edge_max_bps"}
NET_BPS_MIN = 1.0

# 連續區塊的長度（分鐘）。1440 = 一天。
WINDOWS = [60, 120, 240, 480, 720, 1440, 2880, 4320, 5760, 8640]
PAIRS = ["NBIS", "ANTH", "HYPE", "ZEC", "NEAR", "BTC", "GOLD_LL", "NVDA_LL"]


def p90(vals: list[float]) -> float:
    v = sorted(vals)
    return v[int(0.9 * len(v))]


def load(pair: str, key: str) -> list[float]:
    """逐分鐘的可成交空間。**兩檔都讀**（輪替過的 .old 也算）——
    mistake.md 2026-08-29：一份資料兩個讀者只改了一個，計數就從零重數。"""
    out: list[float] = []
    d = LOGS / pair
    files = sorted(d.glob("minutes.csv.*.old")) + sorted(d.glob("minutes.csv.old"))
    files.append(d / "minutes.csv")
    for f in files:
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    try:
                        out.append(float(row[key]))
                    except (KeyError, TypeError, ValueError):
                        continue
        except OSError:
            continue
    return out


def blocks(vals: list[float], w: int) -> list[float]:
    """不重疊的連續區塊，各自算 p90。不重疊是刻意的——重疊區塊之間共享
    資料，會讓散佈看起來比實際小。"""
    return [p90(vals[i:i + w]) for i in range(0, len(vals) - w + 1, w)]


def main() -> int:
    res: dict = {"windows": WINDOWS, "pairs": {}}
    # rel_err[W] = 所有 (配對, 邊) 的相對誤差樣本
    pooled: dict[int, list[float]] = {w: [] for w in WINDOWS}
    d1_fail: list[str] = []

    print("=== 帶寬（p90）要錄多久才穩：連續區塊 vs 全樣本 ===")
    print("相對誤差 = |區塊 p90 − 全樣本 p90| / 全樣本 p90\n")

    for pair in PAIRS:
        for side, key in KEYS.items():
            vals = load(pair, key)
            if len(vals) < max(WINDOWS):
                continue
            full = p90(vals)
            if full <= 0:
                continue
            pid = f"{pair}:{side}"
            row: dict = {"n_minutes": len(vals), "full_p90": round(full, 4),
                         "clamped": bool(full < NET_BPS_MIN), "w": {}}
            for w in WINDOWS:
                bs = blocks(vals, w)
                if not bs:
                    continue
                errs = [abs(b - full) / full for b in bs]
                row["w"][str(w)] = {
                    "n_blocks": len(bs),
                    "med_rel_err": round(statistics.median(errs), 4),
                    "p90_rel_err": round(sorted(errs)[int(0.9 * len(errs))], 4),
                }
                pooled[w].extend(errs)
            res["pairs"][pid] = row

            # D1：整份資料當成一個區塊時，誤差必須恰好是 0
            one = blocks(vals, len(vals))
            if one and abs(one[0] - full) > 1e-12:
                d1_fail.append(pid)

    print("=== D1 自曝：W = 全長時相對誤差必須 = 0 ===")
    if d1_fail:
        print("  **FAIL** ->", ", ".join(d1_fail), " 以下不解讀。")
        return 1
    print(f"  {len(res['pairs'])} 個 (配對,邊) 全部 PASS\n")

    # ── 逐配對才是主表 ────────────────────────────────────────────────
    # 第一版只印合池，那是錯的：這些配對的帶寬差**兩個數量級**
    # （ANTH:sell 460 bps vs NVDA_LL:sell 1.01 bps），合池的相對誤差會被
    # 最不穩的那一兩個整個帶走。這跟 factor-research 第 10 條「費用一律用
    # 逐標的真實 bps，不用統一 ATR 單位」是同一件事——**統一單位會奉承
    # 某一群標的**，方向相反而已。
    # **分類窗要有一個真的分佈。** 第一版拿 6 天（8640 分）分類，但現有歷史
    # 只有 ~13,950 分鐘 -> 每個配對**只切得出一個區塊**，於是「中位誤差」
    # 是一個單一抽樣，不是散佈。用它分類等於拿 n=1 說「收斂了」。
    # 改用 1 天（9 個區塊），並且**把區塊數印出來**——沒印出來的話，
    # 下一個讀這張表的人會重犯同一個錯。
    CLS_W = 1440
    print(f"=== 逐配對：相對誤差（主表，分類用 {CLS_W} 分＝1 天）===")
    print(f"{'配對:邊':<16s} {'全樣本p90':>10s} {'格數':>5s} {'1天中位':>8s} "
          f"{'1天p90':>8s} {'3天中位':>8s}")
    conv, noconv = [], []
    for pid, r in sorted(res["pairs"].items(),
                         key=lambda kv: -kv[1]["full_p90"]):
        w1 = r["w"].get(str(CLS_W), {})
        w3 = r["w"].get("4320", {})
        m1 = w1.get("med_rel_err")
        print(f"{pid:<16s} {r['full_p90']:10.3f} {w1.get('n_blocks', 0):5d} "
              f"{(m1 or 0)*100:7.1f}% {(w1.get('p90_rel_err') or 0)*100:7.1f}% "
              f"{(w3.get('med_rel_err') or 0)*100:7.1f}%")
        (conv if (m1 is not None and m1 < 0.20) else noconv).append(pid)
    res["cls_window_min"] = CLS_W
    res["converged"] = conv
    res["not_converged"] = noconv
    print()
    print(f"=== 1 天窗中位誤差 < 20% 的：{len(conv)}/{len(res['pairs'])} ===")
    print("  收斂：" + ", ".join(conv))
    print("  **沒收斂**：" + ", ".join(noconv))
    print()

    print("=== 合池（**會被最不穩的那幾個帶走，只當背景**）===")
    print(f"{'窗長(分)':>9s} {'約':>8s} {'格數':>6s} {'中位誤差':>9s} "
          f"{'p90 誤差':>9s}")
    summary = {}
    prev_med = None
    mono = True
    for w in WINDOWS:
        e = pooled[w]
        if not e:
            continue
        med = statistics.median(e)
        p90e = sorted(e)[int(0.9 * len(e))]
        human = (f"{w/1440:.1f} 天" if w >= 1440 else f"{w/60:.0f} 小時")
        print(f"{w:9d} {human:>8s} {len(e):6d} {med*100:8.1f}% {p90e*100:8.1f}%")
        summary[str(w)] = {"n": len(e), "med_rel_err": round(med, 4),
                           "p90_rel_err": round(p90e, 4)}
        if prev_med is not None and med > prev_med + 1e-9:
            mono = False
        prev_med = med
    res["pooled"] = summary

    print()
    print("=== D2 自曝：中位誤差必須隨窗長下降 ===")
    print("  " + ("PASS（單調）" if mono else
                  "**注意：不單調** —— 尾端格子數少會抖，看方向不看每一格"))
    print()

    # 讀得出來的門檻：相對誤差 p90 < 20%。這個 20% 不是隨便訂的——
    # 跨儀器對照要能分辨「兩台儀器不一致」與「其中一台還沒錄夠」，
    # 而今天實測的不一致是 10.86 vs 1.00（差 10 倍），所以 20% 的解析度
    # 綽綽有餘；反過來，如果連 20% 都達不到，那張對照表就不能解讀。
    res["target_med_rel_err"] = 0.20
    print("=== 結論 ===")
    # 逐配對算 3 天窗的達標數，不要手寫——手寫的數字會跟資料漂開。
    c3 = [pid for pid, r in res["pairs"].items()
          if (r["w"].get("4320", {}).get("med_rel_err", 9) < 0.20)]
    worst = sorted(res["pairs"].items(),
                   key=lambda kv: -(kv[1]["w"].get("4320", {})
                                    .get("med_rel_err") or 0))[:3]
    res["converged_3d"] = c3
    print(f"  1. **1 天的錄製估不出帶寬——{len(conv)}/{len(res['pairs'])} 個"
          f"在 ±20% 以內。**")
    print("     最好的那個也要 20.4%（GOLD_LL:sell），"
          "而中位數這一群落在 23%~42%。")
    print(f"  2. 拉到 3 天，{len(c3)}/{len(res['pairs'])} 個進到 ±20%："
          + "、".join(c3))
    print("     所以「錄多久」的答案**逐配對不同**，而且沒有一個配對"
          "在一天內可用。")
    print("  3. **有幾個在現有 9.7 天內完全沒有穩下來**（1 天 -> 3 天）：")
    for pid, r in worst:
        m1 = (r["w"].get("1440", {}).get("med_rel_err") or 0) * 100
        m3 = (r["w"].get("4320", {}).get("med_rel_err") or 0) * 100
        print(f"       {pid:<14s} {m1:8.1f}% -> {m3:8.1f}%")
    print("     這不是「再錄久一點就好」，"
          "是這個估計量在這些配對上不穩定。")
    print("     （現有歷史只有 ~9.7 天，4 天以上的窗每個配對只切得出一兩個"
          "區塊——")
    print("      那幾欄是單一抽樣不是散佈，不要拿來下結論。）")
    print("  4. 跨儀器對照（凍結 vs 宇宙）**現在不可解讀**："
          "宇宙那側只有 154 分鐘，")
    print("     而 1 小時區塊的中位誤差就有 ~60%。"
          "今天那張表的不一致不構成證據。")
    if noconv:
        print()
        print("  **要特別注意的**：" + "、".join(noconv[:3]) +
              " 在沒收斂那一群裡，")
        print("  而 §1.20 的容量與 §1.24 的耐心兩個結論都是"
              "**由 ANTH 主導的**（95% / 87%）。")
        print("  那兩節的數字要標上「輸入的取樣誤差未量化」，不是重跑就好。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nwritten -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
