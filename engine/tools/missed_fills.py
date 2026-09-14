# -*- coding: utf-8 -*-
"""我們掛著的時候，市場成交有沒有碰到我們的價？—— 分辨「排隊輸人」與「掛太遠」。

===========================================================================
為什麼要有這一支（2026-09-14）
===========================================================================
HMM 上線四個標的,每一個的症狀都是同一句話:**報價很多、成交很少**。

    GMX    市場一天只成交 45 筆
    AERO   122 次報價 2 筆成交
    XPL    332 次報價 3 筆成交
    MON    340 次報價 1 筆成交

而我們一直分不出這是哪一種病,因為兩種病的症狀一模一樣:

    (a) **掛太遠**：市場成交從來沒碰到我們的價 -> 要掛更近／降門檻
    (b) **排隊輸人**：成交就發生在我們的價上,但先到的單被吃光了
                      -> 要 dime、要更快、或者這個市場不適合我們

**兩種病的下一步動作相反**,而我們到現在都是用推論在猜。2026-09-07 那條
mistake 記著我把「佇列位置」講成事實而完全沒有量過 —— 這支就是那個量測。

===========================================================================
怎麼算
===========================================================================
`maker.csv` 每一列是一張掛完的單:`ts`（結束時刻）、`rest_ms`（掛了多久）、
`side`、`px`。所以掛單視窗是 [ts - rest_ms/1000, ts]。

成交帶每一筆有 `ts`、`px`、`is_maker_ask`（掛單方在賣側 = 吃單方在買）。

對每一張掛單,找視窗內**打到我們那一側**的成交,按價格分三類:

    穿過   我們掛賣而成交價 > 我們的價（或掛買而成交價 < 我們的價）
           -> 比我們好的價都成交了,我們卻沒成交 = **確定被跳過**
    同價   成交價 == 我們的價
           -> 佇列位置
    沒碰到 成交價比我們差 -> 這筆本來就輪不到我們

**「穿過」是最硬的證據**:它不需要任何關於佇列的假設。

===========================================================================
自曝檢查（答案已知）
===========================================================================
    C1  我們真的成交過的那幾張,必須被歸類成「同價」或「穿過」
        —— 一張成交了的單,市場不可能沒碰到它的價。
    C2  成交帶的時間範圍必須蓋住 maker.csv 的時間範圍。
        蓋不住就是在拿一段沒有資料的時間算 0（把「不知道」印成「沒有機會」,
        mistake.md 2026-09-13）。

用法:
    python tools/missed_fills.py --pair MON
    python tools/missed_fills.py --pair XPL --pair AERO
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
TAPE = os.path.join("D:", os.sep, "flowbot_data", "lighter", "trades")

# 價格比較的容忍:半個 tick。浮點與四捨五入不該把「同價」判成「穿過」。
EPS_FRAC = 0.5


def load_tape(coin: str):
    import pandas as pd
    fs = sorted(glob.glob(os.path.join(TAPE, "*", "*.parquet")))
    if not fs:
        raise RuntimeError(
            "讀不到成交帶 %s —— 空結果不是合法狀態（D 槽掛載了嗎）" % TAPE)
    df = pd.concat([pd.read_parquet(f, columns=["ts", "coin", "px", "usd",
                                                "is_maker_ask"])
                    for f in fs[-30:]], ignore_index=True)
    d = df[df.coin == coin].sort_values("ts").reset_index(drop=True)
    if d.empty:
        raise RuntimeError("成交帶裡沒有 %s 的任何成交" % coin)
    return d


def load_quotes(pair: str):
    import pandas as pd
    p = os.path.join(ENGINE, "logs", pair, "maker.csv")
    if not os.path.exists(p):
        raise RuntimeError("沒有 %s —— 這個標的沒跑過掛單路徑" % p)
    q = pd.read_csv(p)
    if q.empty:
        raise RuntimeError("%s 是空的" % p)
    q = q[q.rest_ms.notna() & (q.rest_ms > 0)].copy()
    q["t_end"] = q.ts
    q["t_beg"] = q.ts - q.rest_ms / 1000.0
    return q


def analyse(pair: str) -> int:
    import numpy as np
    import pandas as pd

    q = load_quotes(pair)
    tp = load_tape(pair)
    t_ms = tp.ts.values
    px = tp.px.values
    mk_ask = tp.is_maker_ask.values.astype(bool)
    usd = tp.usd.values

    # C2：成交帶要蓋住掛單的時間範圍。
    #
    # **蓋不到的那幾張要丟掉,不是整批放棄。** 成交帶是每隔一段時間落盤的,
    # 所以一支**還在跑**的引擎,最後幾分鐘的掛單必然蓋不到 —— 把它們算進去
    # 就是拿一段沒有資料的時間算 0,也就是把「不知道」印成「沒有機會」
    # （mistake.md 2026-09-13：未知狀態不可以長得像一個已知狀態）。
    # 而整批放棄則是另一個極端:一支正在跑的引擎永遠分析不了。
    t_lo, t_hi = t_ms.min() / 1000.0, t_ms.max() / 1000.0
    n_before = len(q)
    q = q[(q.t_beg >= t_lo) & (q.t_end <= t_hi)].copy()
    n_dropped = n_before - len(q)
    if q.empty:
        raise RuntimeError(
            "成交帶（%s ~ %s）完全蓋不到任何一張掛單 —— 不是「沒有機會」,"
            "是沒有資料" % (_hhmm(t_lo), _hhmm(t_hi)))
    # 蓋得到的那些一定是被蓋住的,所以 C2 之後永遠 PASS；真正的資訊是
    # 丟掉了幾張,那個數字印出來。
    covered = True

    rows = []
    for _, r in q.iterrows():
        lo, hi = r.t_beg * 1000.0, r.t_end * 1000.0
        i0, i1 = np.searchsorted(t_ms, lo), np.searchsorted(t_ms, hi)
        if i1 <= i0:
            rows.append((0, 0, 0, 0.0))
            continue
        p = px[i0:i1]
        a = mk_ask[i0:i1]
        u = usd[i0:i1]
        # 我們掛 SELL -> 我們是掛單方在賣側 -> 只有 is_maker_ask=True 的成交
        # 打得到我們。掛 BUY 則相反。
        ours = a if r.side == "SELL" else ~a
        if not ours.any():
            rows.append((0, 0, int((~ours).sum()), 0.0))
            continue
        pp, uu = p[ours], u[ours]
        eps = abs(r.px) * 1e-9 + EPS_FRAC * _tick_of(r.px)
        if r.side == "SELL":
            through = pp > r.px + eps      # 比我們貴的賣單都成交了
            same = np.abs(pp - r.px) <= eps
        else:
            through = pp < r.px - eps      # 比我們便宜的買單都成交了
            same = np.abs(pp - r.px) <= eps
        rows.append((int(through.sum()), int(same.sum()),
                     int((~through & ~same).sum()),
                     float(uu[through | same].sum())))

    q[["n_through", "n_same", "n_far", "usd_reachable"]] = pd.DataFrame(
        rows, index=q.index)

    filled = q[q.filled.astype(float) > 0]
    c1 = bool(len(filled) == 0
              or ((filled.n_through + filled.n_same) > 0).all())

    # C3：**同一筆成交不可以被算進兩張掛單**。
    # 第一版對 AERO 數出 41 分鐘裡 378 筆成交 = 9.2 筆/分,而 AERO 的
    # 平均是 0.79 筆/分 —— 差 11 倍。數字大到不合理就先查儀器
    # （mistake.md 2026-09-03）。這一關把「逐張視窗加總」跟「視窗聯集裡的
    # 相異筆數」對起來,重複計數會當場現形。
    seen = np.zeros(len(tp), dtype=bool)
    for _, r in q.iterrows():
        i0 = np.searchsorted(t_ms, r.t_beg * 1000.0)
        i1 = np.searchsorted(t_ms, r.t_end * 1000.0)
        seen[i0:i1] = True
    n_union = int(seen.sum())
    n_sum = int(q.n_through.sum() + q.n_same.sum() + q.n_far.sum())
    c3 = n_sum <= n_union
    # 掛單視窗合計多久、聯集裡的成交率 —— 拿它跟該標的的平均成交率比對
    span_s = float(q.rest_ms.sum()) / 1000.0
    rate_in_window = (n_union / (span_s / 60.0)) if span_s > 0 else 0.0

    print("=== %s ===" % pair)
    print("掛單 %d 張,掛單時間合計 %.0f 秒;成交帶 %d 筆"
          % (len(q), q.rest_ms.sum() / 1000.0, len(tp)))
    print()
    nq = len(q)
    print("  我們掛著時,打到**我們那一側**的市場成交:")
    print("    穿過我們的價   %5d 筆   （出現在 %d/%d 張掛單上）"
          % (q.n_through.sum(), (q.n_through > 0).sum(), nq))
    print("    就在我們的價   %5d 筆   （%d/%d 張）"
          % (q.n_same.sum(), (q.n_same > 0).sum(), nq))
    print("    沒碰到我們     %5d 筆   （%d/%d 張）"
          % (q.n_far.sum(), (q.n_far > 0).sum(), nq))
    print("    我們實際成交   %5d 張" % len(filled))
    reach = q.usd_reachable.sum()
    print("    **搆得到的成交金額合計 $%.2f**" % reach)
    print()

    # **結論看比例,不是看「有沒有」。** 第一版只要有一筆搆得到就說是排隊
    # 問題,於是對 AERO（14 筆搆得到 vs 364 筆沒碰到）給出完全相反的答案。
    on_side = int(q.n_through.sum() + q.n_same.sum() + q.n_far.sum())
    reachable_n = int(q.n_through.sum() + q.n_same.sum())
    frac = (100.0 * reachable_n / on_side) if on_side else float("nan")
    print("  我們那一側的成交裡,搆得到的佔 **%.1f%%**（%d / %d）"
          % (frac, reachable_n, on_side))
    if on_side == 0:
        print("  -> 掛單期間我們那一側**一筆成交都沒有** = 市場太靜,"
              "不是我們的問題也不是排隊")
    elif frac < 20:
        print("  -> 絕大多數成交**碰不到我們的價** = **掛太遠**"
              "（門檻太高／帶太寬）,不是排隊")
    elif q.n_through.sum() > 0:
        print("  -> 有成交**穿過**我們的價而我們沒成交 = **確定被跳過**,"
              "排隊／速度問題")
    else:
        print("  -> 成交都剛好在我們的價上 = 佇列位置問題")
    print()
    print("=== 自曝檢查 ===")
    print("  C1 我們成交過的單必須被歸成同價或穿過          %-6s %s"
          % ("n=%d" % len(filled), "PASS" if c1 else "**FAIL**"))
    print("  C2 只分析成交帶蓋得到的掛單（丟掉 %d/%d 張）    %-6s %s"
          % (n_dropped, n_before, "", "PASS" if covered else "**FAIL**"))
    if n_dropped > n_before * 0.5:
        print("     **丟掉超過一半 —— 成交帶落後太多,等它追上再看**")
    print("  C3 同一筆成交沒有被算進兩張掛單                %-6s %s"
          % ("%d<=%d" % (n_sum, n_union), "PASS" if c3 else "**FAIL**"))
    print("     （掛單視窗合計 %.0f 秒,聯集內 %d 筆 = %.2f 筆/分 —— "
          "拿它跟該標的的平均成交率比,差一個量級就先查這支）"
          % (span_s, n_union, rate_in_window))
    if not (c1 and covered and c3):
        print("  -> **先查這支,不要解讀上面的數字**")
        return 1
    return 0


def _hhmm(epoch_s: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch_s).strftime("%H:%M:%S")


def _tick_of(px: float) -> float:
    """從價格的量級推一個保守的 tick。只用來當比較的容忍值,不當判準。"""
    if px <= 0:
        return 0.0
    import math
    return 10 ** (math.floor(math.log10(abs(px))) - 4)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", action="append", required=True)
    a = ap.parse_args(argv)
    rc = 0
    for p in a.pair:
        try:
            rc |= analyse(p)
        except RuntimeError as e:
            print("=== %s ===\n  跳過:%s\n" % (p, e))
        print()
    return rc


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
