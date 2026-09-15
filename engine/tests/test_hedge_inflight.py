# -*- coding: utf-8 -*-
"""對沖進行中時，淨額對沖不可以自己去平那個失衡。

**這是開了掛單對沖之後才出現的問題，而它會讓風控變成虧損的來源。**

`_hedge` 減的是**帶著失衡的那一腿**。掛單對沖期間帶著失衡的正好是
**掛單腿**（Lighter 剛成交、對沖腿還沒上），所以它會去 Lighter 把我們
剛成交的單買回去 —— 吃單 2.80 ＋ 穿 10.7 bps 價差，而捕獲只有 7.67。

venue lock 擋不住:`_hedge_try_maker` 只在送單那一瞬間握鎖，輪詢期間
兩把鎖都放開（放開是對的，不然 30 秒的對沖會把報價與對帳一起凍住）。

**而每一道 HALT 都必須照跑** —— 擋的只有「自動去平它」。否則這道修法
會把「對沖卡住」變成看不見的，那比原本的問題嚴重。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
from test_maker import make_engine                          # noqa: E402


class _V:
    def __init__(self, pos):
        self.position = pos


def _arm(eng, net, cap=100.0):
    eng.cfg.max_net_base = cap
    eng.venues = {"a": _V(net), "b": _V(0.0)}
    eng.halted = False
    eng._net_over_since = None
    eng._hedge_calls = []
    eng._hedge = lambda n: _rec(eng, n)
    eng._self_rescue = lambda n: _noop()
    return eng


async def _rec(eng, n):
    eng._hedge_calls.append(n)


async def _noop():
    return None


def _run(eng):
    asyncio.get_event_loop().run_until_complete(eng._maybe_hedge())


def test_net_hedge_fires_when_nothing_is_inflight():
    """**先證明這支測得到東西。** 沒有對沖在飛的時候，淨額對沖照樣開火 ——
    不然下面那一關可能只是因為 `_hedge` 從來不會被呼叫而通過。"""
    eng = make_engine()
    _arm(eng, net=50.0, cap=100.0)          # 超過容忍、沒超過水位
    assert eng._hedge_maker_inflight == 0, "預設不是 0"
    _run(eng)
    assert eng._hedge_calls == [50.0], "淨額對沖沒開火 —— 這支測不到東西"


def test_net_hedge_stands_down_while_a_hedge_is_inflight():
    """對沖進行中 -> 不去平。**這就是那一筆的生死。**"""
    eng = make_engine()
    _arm(eng, net=50.0, cap=100.0)
    eng._hedge_maker_inflight = 1
    _run(eng)
    assert eng._hedge_calls == [], \
        "對沖還在飛，淨額對沖卻去平了 —— 它會把剛成交的那一腿買回去"


def test_halt_still_fires_while_inflight():
    """**每一道 HALT 照跑。** 這道修法只擋「自動去平」，不擋守衛 ——
    否則「對沖卡住」會變成看不見的,那比原本的問題嚴重。"""
    eng = make_engine(net_grace_sec=10.0)
    _arm(eng, net=150.0, cap=100.0)         # 超過水位
    eng._hedge_maker_inflight = 1
    eng._net_over_since = time.time() - 11.0        # 已經超過寬限
    _run(eng)
    assert eng.halted, "對沖卡住超過寬限期，竟然沒有 HALT"


def test_daily_loss_kill_still_fires_while_inflight():
    """每日虧損那條是 kill switch，它更不可以被這個旗標影響。"""
    eng = make_engine(max_daily_loss_usd=1.0)
    _arm(eng, net=50.0, cap=100.0)
    eng._hedge_maker_inflight = 1
    eng.session_pnl = lambda: -50.0
    _run(eng)
    assert eng.halted, "每日虧損下限被 inflight 旗標擋掉了"


def test_counter_returns_even_when_the_hedge_raises():
    """**計數器一定要回得來。** 回不來的話淨額對沖就永遠停擺了 ——
    那是一個會靜默累積裸曝險的狀態。"""
    eng = make_engine()
    _arm(eng, net=0.0)

    async def boom(*a, **k):
        raise RuntimeError("hedge blew up")
    eng._hedge_maker_fill = boom

    from entropy_arb.maker import MakerOrder
    o = MakerOrder(venue_key="entropy", is_buy=True, qty=10.0, px=100.0,
                   sent_ts=time.time())
    o.filled_base = 10.0
    v = eng.entropy
    v.set_book(100.0, 100.2)
    try:
        asyncio.get_event_loop().run_until_complete(
            eng._consume_maker_fill(v, eng.hedge, o))
    except RuntimeError:
        pass
    assert eng._hedge_maker_inflight == 0, \
        "對沖拋例外之後計數器沒回來 —— 淨額對沖會永遠停擺"


def test_counter_is_a_count_not_a_flag():
    """兩腿同時在對沖時，先回來的那個不可以把旗標清掉。

    **這一關必須走生產程式碼的加減。** 第一版用手設計數器，於是
    「`-= 1` 被改成 `= 0`」這個注入完全命不中它 —— 它測的是我自己在
    測試裡寫的算術（mistake.md 2026-09-14:反向證明沒有變紅，先懷疑
    注入；而這次錯的是那一關本身沒有碰到被注入的那幾行）。

    **而分辨力在「一腿回來、另一腿還在飛」那一刻**:兩腿同時放行的話，
    `-= 1` 與 `= 0` 的終點都是 0，注入命中也不會紅。所以兩個閘要分開放。
    """
    eng = make_engine()
    _arm(eng, net=50.0, cap=100.0)
    gates, seen = [asyncio.Event(), asyncio.Event()], []

    async def slow(*a, **k):
        i = len(seen)
        seen.append(eng._hedge_maker_inflight)
        await gates[i].wait()

    eng._hedge_maker_fill = slow
    from entropy_arb.maker import MakerOrder

    def mk():
        o = MakerOrder(venue_key="entropy", is_buy=True, qty=10.0, px=100.0,
                       sent_ts=time.time())
        o.filled_base = 10.0
        return o

    eng.entropy.set_book(100.0, 100.2)

    async def drive():
        a = asyncio.ensure_future(
            eng._consume_maker_fill(eng.entropy, eng.hedge, mk()))
        await asyncio.sleep(0)
        b = asyncio.ensure_future(
            eng._consume_maker_fill(eng.entropy, eng.hedge, mk()))
        await asyncio.sleep(0)
        assert eng._hedge_maker_inflight == 2, \
            "兩腿在飛，計數器是 %d" % eng._hedge_maker_inflight

        gates[0].set()                      # 只放第一腿回來
        await a
        assert eng._hedge_maker_inflight == 1, \
            "一腿回來就把計數器清成 %d —— 另一腿還在飛" \
            % eng._hedge_maker_inflight
        await eng._maybe_hedge()
        assert eng._hedge_calls == [], "還有一腿在飛，卻已經放行去平倉了"

        gates[1].set()
        await b

    asyncio.get_event_loop().run_until_complete(drive())
    assert seen == [1, 2], "生產程式碼不是逐一遞增:%s" % seen
    assert eng._hedge_maker_inflight == 0, "全部回來後計數器不是 0"
    _run(eng)
    assert eng._hedge_calls == [50.0], "全部回來了卻不開火"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
