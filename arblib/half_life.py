# -*- coding: utf-8 -*-
"""溢價的 AR(1) 半衰期：這個引擎能不能吃到這個配對（2026-09-06）

**問題**：兩腿價差張開之後，多久收斂一半？如果半衰期遠長於我們的持有能力，
那不是「參數沒調好」，是**這個標的不歸這個引擎管**——收斂型套利需要溢價會回來，
而回不來的溢價只會讓我們單邊堆倉（NBIS 09-03 就是那個形狀）。

**做法**
  序列    premium_close_bps，重取樣到規律的 1 分鐘格點。缺格不補值，
          只用**相鄰**的 (t−1, t) 配對做迴歸——跨越斷線的那一步不是一分鐘。
  迴歸    p_t = a + b·p_{t−1} + e   （OLS）
  半衰期  ln(0.5) / ln(b)，單位分鐘。只在 0 < b < 1 時有定義：
          b ≥ 1 → 不收斂（隨機遊走或發散），記為 inf；b ≤ 0 → 一步內翻號，記 0。
  CI      按**日**做 block bootstrap（跟 venue_toxicity 同一種，因為同樣的資料
          有很強的日內自相關），1000 次，取 b 的 2.5/97.5 百分位再換算成半衰期。
  兩半    前後各半各跑一次。**這是既有紀律**（SNDK 判決文的閘門就是前後兩半都
          成立）——半衰期在兩半差一個數量級，代表量到的是 regime 不是性質。

**判準（使用者 2026-09-06 給的）**：只留**半衰期 < 5 分鐘**的配對，
其餘歸到「不是這個引擎的問題」。

**這個判準會漏掉什麼（先寫下來，免得之後拿它當全部）**
  AR(1) 假設均值回歸到一個**固定**中樞。NBIS 09-03 的階躍讓中樞自己搬家，
  全樣本迴歸會把「搬家」讀成「不回歸」，把 b 推向 1、半衰期推向無限大。
  所以全樣本半衰期長 ≠ 一定不能做，也可能是**中樞錯了**（見
  NBIS_REGIME_20260903.md §十的滾動中位數提案）。兩半那一欄就是用來分辨的。

Run: python arblib/half_life.py [--max-hl 5.0] [--boot 1000]
Out: results/arb_half_life.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "engine" / "logs"


def load_series(path: Path):
    """回傳 (分鐘格點 ts, premium_close)，缺格為 NaN。"""
    ts, px = [], []
    with io.open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                ts.append(int(r["minute_ts"]) // 60 * 60)
                px.append(float(r["premium_close_bps"]))
            except (KeyError, TypeError, ValueError):
                continue
    if len(ts) < 60:
        return None, None
    t = np.asarray(ts, dtype=np.int64)
    p = np.asarray(px, dtype=float)
    o = np.argsort(t, kind="stable")
    t, p = t[o], p[o]
    keep = np.concatenate(([True], np.diff(t) > 0))       # 去重複分鐘
    t, p = t[keep], p[keep]
    grid = np.arange(t[0], t[-1] + 60, 60, dtype=np.int64)
    out = np.full(grid.shape, np.nan)
    out[np.searchsorted(grid, t)] = p
    return grid, out


def pairs(grid, p):
    """相鄰分鐘的 (x=p_{t-1}, y=p_t, day)。跨斷線的一步被 NaN 自動排除。"""
    x, y = p[:-1], p[1:]
    ok = np.isfinite(x) & np.isfinite(y)
    day = (grid[1:] // 86400).astype(np.int64)
    return x[ok], y[ok], day[ok]


def ar1_b(x, y):
    if len(x) < 30:
        return float("nan")
    vx = x - x.mean()
    d = float(vx @ vx)
    return float("nan") if d <= 0 else float(vx @ (y - y.mean()) / d)


def hl(b):
    """b → 半衰期（分鐘）。"""
    if not np.isfinite(b) or b <= 0:
        return 0.0
    if b >= 1:
        return float("inf")
    return math.log(0.5) / math.log(b)


def boot_ci(x, y, day, n):
    days = np.unique(day)
    if len(days) < 2:
        return [float("nan"), float("nan")]
    idx = {d: np.where(day == d)[0] for d in days}
    rng = np.random.default_rng(20260906)
    bs = []
    for _ in range(n):
        pick = rng.choice(days, size=len(days), replace=True)
        s = np.concatenate([idx[d] for d in pick])
        b = ar1_b(x[s], y[s])
        if np.isfinite(b):
            bs.append(b)
    if len(bs) < 50:
        return [float("nan"), float("nan")]
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return [hl(float(lo)), hl(float(hi))]        # b 越大半衰期越長，順序不變


def run_pair(name, path, boot):
    grid, p = load_series(path)
    if grid is None:
        return None
    x, y, day = pairs(grid, p)
    if len(x) < 200:
        return None
    b = ar1_b(x, y)
    half = len(x) // 2
    b1, b2 = ar1_b(x[:half], y[:half]), ar1_b(x[half:], y[half:])
    return {
        "pair": name, "n_steps": int(len(x)), "days": int(len(np.unique(day))),
        "minutes_span": float((grid[-1] - grid[0]) / 60),
        "coverage": float(np.isfinite(p).mean()),
        "b": b, "half_life_min": hl(b), "ci": boot_ci(x, y, day, boot),
        "halves_hl": [hl(b1), hl(b2)],
        "sd_bps": float(np.nanstd(p)), "median_bps": float(np.nanmedian(p)),
    }


def main():
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-hl", type=float, default=5.0)
    ap.add_argument("--boot", type=int, default=1000)
    a = ap.parse_args()

    cands = []
    root_csv = LOGS / "minutes.csv"
    if root_csv.exists():
        cands.append(("SNDK", root_csv))
    for d in sorted(os.scandir(LOGS), key=lambda e: e.name):
        if d.is_dir() and (Path(d.path) / "minutes.csv").exists():
            cands.append((d.name, Path(d.path) / "minutes.csv"))

    out = [r for r in (run_pair(n, p, a.boot) for n, p in cands) if r]
    for r in out:
        r["keep"] = bool(r["half_life_min"] < a.max_hl)

    def f(v, n=1):
        return "inf" if v == float("inf") else ("nan" if not np.isfinite(v) else f"{v:,.{n}f}")

    print("=" * 104)
    print(f"  溢價 AR(1) 半衰期 · 判準：半衰期 < {a.max_hl:g} 分鐘才留 · "
          f"日 block bootstrap {a.boot} 次")
    print("=" * 104)
    print(f"{'配對':<20}{'步數':>7}{'天':>4}{'覆蓋':>7}{'b':>8}{'半衰期(m)':>11}"
          f"{'CI':>19}{'兩半':>17}{'σ(bps)':>9}  留？")
    print("-" * 104)
    for r in sorted(out, key=lambda r: r["half_life_min"]):
        ci = f"[{f(r['ci'][0])}, {f(r['ci'][1])}]"
        hv = f"{f(r['halves_hl'][0])} / {f(r['halves_hl'][1])}"
        print(f"{r['pair']:<20}{r['n_steps']:>7,}{r['days']:>4}{r['coverage']*100:>6.1f}%"
              f"{r['b']:>8.4f}{f(r['half_life_min'],2):>11}{ci:>19}{hv:>17}"
              f"{r['sd_bps']:>9.2f}  {'留' if r['keep'] else '不是這個引擎的問題'}")
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "arb_half_life.json").write_text(
        json.dumps({"criterion_max_half_life_min": a.max_hl, "boot": a.boot,
                    "pairs": out}, ensure_ascii=False, indent=1, default=float),
        encoding="utf-8")
    print(f"\n  -> results/arb_half_life.json")


if __name__ == "__main__":
    main()
