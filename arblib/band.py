# -*- coding: utf-8 -*-
"""§0.75 家族「帶」的**單一定義**（2026-09-13 定案）

===========================================================================
為什麼這個檔案存在
===========================================================================
2026-09-13 查出同一條線上有**三個**不同的「帶」定義在做決定，而其中兩個的
docstring 都寫著「照 `analyze.py` 的方法」：

                        analyze.py（原）   premium_verdict   scan_rank
    p90 的母體           全部列             全部列  OK        **只取正值**
    扣 midline（中位溢價） **是**            **否**            **否**
    扣手續費             是                 否（另外算）      否（另外算）
    百分位               線性內插           `v[int(0.9*n)]`   pandas（線性）OK
    四捨五入到 0.5       是                 否                否
    下限                 1.0                NET_BPS_MIN=1.0   無

差多少（實測 ANTH，截到凍結 json 自己的 asof、18,422 分鐘）：

    賣側   只取正值 467.942   全部列 422.097     差 +10.9%
    買側   只取正值  40.849   全部列   4.645     差 **8.8 倍**

而 **midline 那一項影響更大，而且打到關鍵的那個配對**：

    GOLD_LL   賣帶 7.90 -> **2.47**   （中位溢價 +5.42）
    NBIS      賣帶 26.39 -> 14.76     （中位溢價 +11.64）
    SNDK      賣帶 4.12 -> **6.74**   （中位溢價 −2.62，所以帶反而升）

GOLD_LL 是零費率之下**唯一**還為正的配對，而它的帶會掉到三分之一。

===========================================================================
定案：照 analyze.py，因為它是原主人
===========================================================================
兩個下游的 docstring 都聲稱照它，所以這不是在三個之中選一個，是**回到那一個**。
它的三個特徵各有理由，不是風格：

1. **p90 取全部列**，所以「帶」的語意是「**有多少比例的時間**超過它」。
   `analyze.py` 的註解逐字寫著「the band that fired in ~10% of minutes」。
   只取正值會回答另一個問題（多少比例的**機會**），而那個問題的分母
   （正邊際的分鐘數）本身隨配對變動 —— 兩個配對的「10%」不可比。

2. **扣 midline（溢價的中位數）**，所以帶量的是**相對這個配對自己的中位溢價
   的偏離**，不是相對零。這一項是這次最重要的發現：
   `small-trader-alpha-6` 說跨場館溢價的常態差異有三個來源
   （穩定幣基差、結構差異、合約差異），而**扣中位數一次吸收掉三個**。
   TODO §1.21（扣穩定幣基差）其實是在重新推導同一個修正的一個子集。
   **不扣它，一個常態偏移會被記成可捕獲的空間。**

3. **下限 1.0 bps**：低於這個的帶在任何成本結構下都不可能是交易。

**刻意不照的一件**：`analyze.py` 在帶裡面扣手續費（它的輸出是要餵給
config 的淨門檻）。這裡不扣 —— 下游的 `cost_model` 已經逐項算費用，
在帶裡再扣一次就是重複計算。所以 `fees_bps` 預設 0，要扣的呼叫端自己傳。

===========================================================================
不要靜默換掉凍結的判準
===========================================================================
`scan_rank` 的升格指標**凍結於 2026-08-30**、`premium_verdict` 的閘門也已經
做過判決。所以本檔提供定義，**但兩個呼叫端要同時報舊值與新值**
（`band_bps` 與 `band_bps_legacy`），讓差異可見。
採不採用是一個要寫下來的決定，不是一次 import 的副作用
（「判準事前寫死、事後不放寬」）。
"""
from __future__ import annotations

import math

FLOOR_BPS = 1.0        # analyze.py: max(..., 1.0)
ROUND_TO = 0.5         # analyze.py: round(x*2)/2


def pctl(vals, q: float) -> float:
    """線性內插的百分位，逐行照 `engine/tools/analyze.py`。

    **不要換成 `v[int(q/100*n)]`** —— 那是 `premium_verdict.side_stats`
    現在的寫法，在小樣本或重尾上與內插版差得出來。
    """
    v = sorted(vals)
    if not v:
        return float("nan")
    k = (len(v) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return v[int(k)]
    return v[lo] * (hi - k) + v[hi] * (k - lo)


def midline_bps(premiums) -> float:
    """配對自己的常態溢價 = premium 的中位數（analyze.py L106）。

    `round(median, 1) or 0.0` —— 後半是為了把 -0.0 正規化成 0.0。
    """
    v = [x for x in premiums if x is not None]
    if not v:
        return 0.0
    return round(pctl(v, 50), 1) or 0.0


def band_bps(edges, midline: float, side: str, fees_bps: float = 0.0,
             q: float = 90.0, floor_bps: float = FLOOR_BPS,
             round_to: float = ROUND_TO) -> float:
    """家族的**單一**帶定義。

    edges     該側每分鐘的可執行邊際（`sell_max` 或 `buy_max`），**全部列**
    midline   `midline_bps(premiums)`
    side      "sell" 或 "buy" —— 決定 midline 的符號（analyze.py L111/L113：
              賣側減、買側加）
    fees_bps  預設 **0**：下游的 cost_model 已逐項算費用，這裡再扣是重複計算
    """
    if side not in ("sell", "buy"):
        raise ValueError("side must be 'sell' or 'buy', got %r" % side)
    sign = -1.0 if side == "sell" else +1.0
    room = [e + sign * midline - fees_bps for e in edges if e is not None]
    if not room:
        return float("nan")
    p = pctl(room, q)
    if round_to:
        p = round(p / round_to) * round_to
    return max(p, floor_bps)


def band_bps_legacy_positive_only(edges, q: float = 90.0) -> float:
    """`scan_rank.side_metric` 現在的寫法 —— **只取正值、不扣 midline**。

    保留它是為了讓升格指標可以同時報舊值與新值（差異要可見，不可靜默換掉
    一個凍結於 2026-08-30 的判準）。**新的研究不要用這個。**
    """
    pos = [e for e in edges if e is not None and e > 0]
    return pctl(pos, q) if pos else float("nan")


def band_bps_legacy_all_rows(edges, floor_bps: float = FLOOR_BPS,
                             q: float = 90.0) -> float:
    """`premium_verdict.side_stats` 現在的寫法 —— 全部列、**不扣 midline**、
    索引式百分位（非內插）。同樣只為了並列報告而保留。"""
    v = sorted(e for e in edges if e is not None)
    if not v:
        return float("nan")
    p = v[int(q / 100.0 * len(v))]
    return max(p, floor_bps)


__all__ = ["pctl", "midline_bps", "band_bps",
           "band_bps_legacy_positive_only", "band_bps_legacy_all_rows"]
