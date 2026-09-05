# -*- coding: utf-8 -*-
"""場館毒性：無條件掛單的 markout（從量化線交接，2026-09-05）

**問題**：在這個場館掛被動單，成交後價格往哪走？這是「去沒人守的地方」的可量化
版本，也是 B3 掛單路徑該不該做的前置判定——**先量毒性，再蓋掛單**。

**基準（量化線同一測試在 Binance 現貨 BTC 上的結果）**：每 3 分鐘雙邊各掛一張，
T=60，成交 92.9%，**markout_60 = −3.1 bps，CI [−3.4, −2.8]**——守得最緊的場館，
被動單每筆虧 3 bps，逆選擇在成交那一分鐘就完成（成交後漂移 ≈ 0）。
小場館若接近零或為正，「沒人守」就從直覺變成數字。

**定義（照 flow_system/research/subhourly/PREREG_passive_markout.md，逐字）**
  掛單事件  分鐘 t、方向 s（+1 掛在 bid 買／−1 掛在 ask 賣）、價 p = 該側 top-of-book、
            有效期 T 分鐘。每 STEP 分鐘雙邊各一張（無條件）。
  成交判定  主規則：買 bid(t′) ≤ p／賣 ask(t′) ≥ p（我們成為 best 被打到）
            嚴格規則：買 ask(t′) ≤ p／賣 bid(t′) ≥ p（對手方穿過）
            t′ = 第一個滿足的分鐘；成交價 = p。兩規則同報。
  markout   s × (mid(t′+h) − p) / p × 1e4，h ∈ {1, 5, 15, 60}，**從成交價與成交時間量**。
            拆成「成交分鐘內被穿過的幅度」s×(mid(t′)−p)/p 與「成交後漂移」s×(mid(t′+h)−mid(t′))/p。
            （1m 快照只能在 book 已穿過 p 時判成交，所以第一項偏負；漂移項才是
            「之後有沒有人繼續打」。）
  未成交    不給報酬。只報 m_h = s×(mid(t+h)−mid(t))/mid(t) 的成交／未成交對照（選擇效應）。
  資料      本 repo 的 engine/logs/<pair>/minutes.csv（`entropy_*` 與 `hedge_*` 兩條腿各跑一次）。
            **每分鐘一個快照，看不到分鐘內成交與佇列位置**——跟基準是同一種解析度，可比。
  判準      場館「可站被動側」需同時：markout_60 的 CI 下緣 > −1 bps（不比零費場館的
            半價差差）∧ 成交後漂移 CI 含零或為正 ∧ 兩半同號。否則「有人守」。

Run: python arblib/venue_toxicity.py [--pair NBIS] [--leg entropy|hedge|both] [--T 60] [--step 3]
Out: results/venue_toxicity_<pair>.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
HS = (1, 5, 15, 60)


def load(pair: str) -> pd.DataFrame:
    parts = []
    for f in sorted((ROOT / "engine" / "logs" / pair).glob("minutes.csv*")):
        try:
            parts.append(pd.read_csv(f))
        except Exception:  # noqa: BLE001
            continue
    d = pd.concat(parts, ignore_index=True).drop_duplicates("minute_ts").sort_values("minute_ts")
    d["m"] = (d["minute_ts"].astype(np.int64) // 60) * 60
    idx = pd.RangeIndex(int(d["m"].min()), int(d["m"].max()) + 60, 60)
    return d.set_index("m").reindex(idx)


def run_leg(d: pd.DataFrame, leg: str, T: int, step: int) -> dict:
    bid = d[f"{leg}_bid"].astype(float).values; ask = d[f"{leg}_ask"].astype(float).values
    mid = (bid + ask) / 2
    n = len(d); idx = np.arange(n)
    out = {}
    for s in (1, -1):
        p = bid if s > 0 else ask
        kf = np.full(n, -1, np.int64); ks = np.full(n, -1, np.int64)
        for k in range(1, T + 1):
            if s > 0:
                c = bid[k:] <= p[:-k]; cs = ask[k:] <= p[:-k]
            else:
                c = ask[k:] >= p[:-k]; cs = bid[k:] >= p[:-k]
            c = np.nan_to_num(c, nan=False).astype(bool); cs = np.nan_to_num(cs, nan=False).astype(bool)
            f = np.zeros(n, bool); f[:-k] = c & (kf[:-k] < 0); kf[f] = k
            fs = np.zeros(n, bool); fs[:-k] = cs & (ks[:-k] < 0); ks[fs] = k
        ev = idx[(idx % step == 0) & np.isfinite(p) & (idx < n - T - max(HS) - 1)]
        filled = kf[ev] > 0; tf = ev + np.maximum(kf[ev], 0)
        rows = []
        for e, t_, ok in zip(ev, tf, filled):
            r = {"day": int(d.index[e] // 86400), "filled": bool(ok)}
            for h in HS:
                r[f"m{h}"] = s * (mid[e + h] - mid[e]) / mid[e] * 1e4
                if ok:
                    r[f"mo{h}"] = s * (mid[t_ + h] - p[e]) / p[e] * 1e4
                    r[f"thru{h}"] = s * (mid[t_] - p[e]) / p[e] * 1e4
                    r[f"drift{h}"] = s * (mid[t_ + h] - mid[t_]) / p[e] * 1e4
            rows.append(r)
        df = pd.DataFrame(rows)
        out[s] = {"n": int(len(df)), "fill": float(df.filled.mean()), "fill_strict": float((ks[ev] > 0).mean()), "df": df}
    return out


def dblock(v, days, B=2000, seed=3):
    rng = np.random.default_rng(seed); g = {}
    for x, dd in zip(v, days):
        if np.isfinite(x): g.setdefault(dd, []).append(x)
    ks = np.array(list(g))
    if len(ks) < 3:
        return float("nan"), float("nan")
    out = [np.concatenate([g[dd] for dd in rng.choice(ks, len(ks))]).mean() for _ in range(B)]
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def report(pair, leg, res, T):
    df = pd.concat([res[1]["df"], res[-1]["df"]], ignore_index=True)
    f = df[df.filled]; u = df[~df.filled]
    print(f"\n  [{pair} · {leg}] T={T}m  事件 {len(df)}  成交率 {df.filled.mean()*100:.1f}%  "
          f"(嚴格 {np.mean([res[1]['fill_strict'], res[-1]['fill_strict']])*100:.1f}%)  天數 {df.day.nunique()}")
    print(f"    {'h':>4}{'markout|成交':>14}{'CI':>20}{'穿過幅度':>9}{'成交後漂移':>10}{'漂移 CI':>20}{'m_h 成交/未成交':>18}")
    stats = {}
    for h in HS:
        mo = f[f"mo{h}"]; lo, hi = dblock(mo.values, f.day.values)
        dr = f[f"drift{h}"]; dlo, dhi = dblock(dr.values, f.day.values)
        stats[h] = {"markout": float(mo.mean()), "ci": [lo, hi], "thru": float(f[f"thru{h}"].mean()),
                    "drift": float(dr.mean()), "drift_ci": [dlo, dhi],
                    "m_filled": float(f[f"m{h}"].mean()), "m_unfilled": float(u[f"m{h}"].mean()) if len(u) else None}
        st = stats[h]
        print(f"    {h:>3}m{st['markout']:>+13.2f} [{lo:+7.2f},{hi:+7.2f}]{st['thru']:>+9.2f}{st['drift']:>+10.2f}"
              f" [{dlo:+7.2f},{dhi:+7.2f}]{st['m_filled']:>+9.2f}/{(st['m_unfilled'] or 0):>+7.2f}")
    half = len(f) // 2
    h1, h2 = f["mo60"].iloc[:half].mean(), f["mo60"].iloc[half:].mean()
    c1 = stats[60]["ci"][0] > -1.0; c2 = stats[60]["drift_ci"][1] >= 0; c3 = np.sign(h1) == np.sign(h2)
    verdict = "可站被動側" if (c1 and c2 and c3) else "有人守"
    print(f"    兩半 markout60 {h1:+.2f}/{h2:+.2f}  (1) CI下緣>−1: {'過' if c1 else '不過'}  (2) 漂移不為負: {'過' if c2 else '不過'}"
          f"  (3) 兩半同號: {'過' if c3 else '不過'}  ==> {verdict}   [Binance BTC 基準: −3.1 bps]")
    return {"fill": float(df.filled.mean()), "stats": stats, "halves": [float(h1), float(h2)], "verdict": verdict}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="NBIS"); ap.add_argument("--leg", default="both")
    ap.add_argument("--T", type=int, default=60); ap.add_argument("--step", type=int, default=3)
    a = ap.parse_args()
    d = load(a.pair)
    print("=" * 96)
    print(f"  場館毒性 · {a.pair} · {len(d):,} 分鐘 · 無條件掛單每 {a.step} 分鐘雙邊 · T={a.T}")
    print("=" * 96)
    out = {}
    for leg in (("entropy", "hedge") if a.leg == "both" else (a.leg,)):
        out[leg] = report(a.pair, leg, run_leg(d, leg, a.T, a.step), a.T)
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / f"venue_toxicity_{a.pair}.json").write_text(
        json.dumps({"pair": a.pair, "T": a.T, "step": a.step, "legs": out}, ensure_ascii=False, indent=1, default=float),
        encoding="utf-8")


if __name__ == "__main__":
    main()
