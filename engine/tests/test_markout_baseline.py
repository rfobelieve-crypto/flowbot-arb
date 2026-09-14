# -*- coding: utf-8 -*-
"""M3 的基準必須是成交當下的 mid，不是成交價。

===========================================================================
為什麼（2026-09-14，實盤打出來的）
===========================================================================
X4 的 M3 判準逐字寫著「相對**成交價**的中價漂移，> −1 bps」。而我們掛單
成交在自己掛的那個價（賣在 ask、買在 bid），mid 在中間 —— 所以**行情完全
不動時，那個算式就讀出 +半個價差**。

    MET 實測 2026-09-14：mean_bps +30.26（n=95），Lighter 半價差中位 33.98
                         真實漂移 −3.72 bps，而 session 損益 −$0.18（同號）

後果不是「數字有點高」，是**這個閘門在價差越寬的市場上越會誤判為通過** ——
而寬價差正好是 HMM 的篩選在挑的東西。一個系統性放行它該擋的那一類的守衛，
比沒有守衛糟（mistake.md 2026-09-12「守衛的統計量沒有做到它宣稱的那件事」）。

這支釘住的是：**被逆選擇時，新讀數必須是負的。** 舊讀數保留但不判決。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from entropy_arb.markout import MarkoutTracker           # noqa: E402

# MET 2026-09-14 的真實盤口
BID, ASK = 0.22109, 0.22279
MID = (BID + ASK) / 2.0                                   # 半價差 ~38.3 bps


def _one(fill_px, mid_at_fill, mid_later, maker_is_buy):
    m = MarkoutTracker(horizon_sec=60.0)
    m.record(ts=0.0, px=fill_px, usd=15.0,
             maker_is_buy=maker_is_buy, mid=mid_at_fill)
    m.settle(now=61.0, mid=mid_later)
    return m.summary()


def test_flat_market_reads_zero_not_half_spread():
    """行情完全不動 = 逆選擇 0。舊讀數會讀出半價差，新讀數不可以。"""
    s = _one(ASK, MID, MID, maker_is_buy=False)
    assert abs(s["vs_mid_bps"]) < 1e-6, (
        "行情沒動，vs_mid 必須是 0，實際 %s" % s["vs_mid_bps"])
    assert s["mean_bps"] > 30.0, (
        "舊讀數在這裡本來就會讀出半價差 —— 若不是，代表舊算式被改了，"
        "而它是被引用過的數字，不可以悄悄消失")
    assert abs(s["captured_half_bps"] - s["mean_bps"]) < 1e-6


def test_adverse_fill_is_negative_even_though_old_reading_is_positive():
    """**這支是整個檔案的理由。**

    賣出之後 mid 往上跑 10 bps = 被挑走。舊讀數仍然是 +28 -> M3 的
    `> −1 bps` 會放行；新讀數必須是 −10。
    """
    up = MID * (1.0 + 10e-4)
    s = _one(ASK, MID, up, maker_is_buy=False)
    assert s["mean_bps"] > 0, (
        "舊讀數在被挑走時仍為正 —— 這正是 2026-09-14 那個病，"
        "它必須留在這裡當對照，不是被修掉")
    assert abs(s["vs_mid_bps"] + 10.0) < 0.05, (
        "新讀數必須抓到 −10 bps，實際 %s" % s["vs_mid_bps"])


def test_buy_side_sign_is_symmetric():
    """掛買單成交後 mid 往下跑 = 也是被挑走，符號要對稱。"""
    dn = MID * (1.0 - 10e-4)
    s = _one(BID, MID, dn, maker_is_buy=True)
    assert abs(s["vs_mid_bps"] + 10.0) < 0.05, s["vs_mid_bps"]


def test_missing_mid_stays_unknown_not_zero():
    """拿不到掛單腿的 mid 時，未知必須長得像未知。

    填 0 會被讀成「沒有逆選擇」，而那是最壞的方向
    （mistake.md 2026-09-13：未知狀態不可以長得像一個已知狀態）。
    """
    m = MarkoutTracker(horizon_sec=60.0)
    m.record(ts=0.0, px=ASK, usd=15.0, maker_is_buy=False)   # 不給 mid
    m.settle(now=61.0, mid=MID)
    s = m.summary()
    assert s["vs_mid_bps"] is None and s["vs_mid_n"] == 0
    assert s["captured_half_bps"] is None
    assert s["n"] == 1, "舊讀數照樣要算得出來"


def test_engine_passes_the_maker_venue_mid():
    """引擎必須傳**掛單腿**的 mid。

    engine.py 另外有一個 `mid_at_fill`，那是**對沖腿**的，用途不同。
    拿錯會把兩所之間的基差混進逆選擇裡（markout.py 檔頭第 2 點），
    而那是一個不會報錯、只會給出合理數字的錯。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "entropy_arb", "engine.py")
    with open(p, encoding="utf-8") as fh:
        src = fh.read()
    assert 'order.stats["maker_mid_at_fill"] = maker_v.book.mid()' in src, \
        "掛單腿的 mid 沒有被記下來"
    # 第一版用 `re.search(r"self\\.mark\\.record\\((.*?)\\)")` 抓參數 ——
    # 非貪婪的 `)` 停在 `time.time()` 裡面，於是抓到半個呼叫。
    # 直接比對那一行就好：要釘的本來就是「有沒有把掛單腿的 mid 傳進去」。
    assert 'mid=st.get("maker_mid_at_fill")' in src, \
        "mark.record 沒有收到掛單腿的 mid —— M3 會退回舊基準而且不會報錯"
