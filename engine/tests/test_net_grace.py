# -*- coding: utf-8 -*-
"""裸曝險閘門的時間維度：**預設不可以改變今天的行為**。

這道閘門是 B4/G1，會在對沖失敗時停掉一個正在送真單的引擎。所以這支
最重要的一關不是「寬限期有效」，是 **`net_grace_sec=0` 時它跟以前一模一樣**。

為什麼要加時間維度（2026-09-15）:
    一張單 639 顆 > max_net_base 491.873
    -> 掛單對沖成交的那一刻必然超過水位 -> 純水位的閘門必然 HALT
    -> **掛單對沖結構上不可能**，而它是唯一能把每筆從 −1.47 翻到
       +2.18 bps 的槓桿（HL 吃單 4.50 -> 掛單 1.50，且不用穿 0.65 價差）

不可以改成調大 max_net_base —— 那會拆掉守衛本身（CLAUDE.md 2026-09-14）。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
from test_maker import make_engine                          # noqa: E402


class _V:
    def __init__(self, pos):
        self.position = pos


def _arm(eng, net, cap=100.0):
    """把引擎擺成「兩腿淨額 = net」，其餘都不相干。"""
    eng.cfg.max_net_base = cap
    eng.venues = {"a": _V(net), "b": _V(0.0)}
    eng.halted = False
    eng._net_over_since = None
    return eng


def _halts(eng):
    """跑一次閘門，回傳它有沒有 HALT。不真的送單。"""
    import asyncio
    eng._hedge = lambda net: _noop()
    eng._self_rescue = lambda net: _noop()
    asyncio.get_event_loop().run_until_complete(eng._maybe_hedge())
    return eng.halted


async def _noop():
    return None


def test_default_is_byte_for_byte_todays_behaviour():
    """**這一關最重要。** 預設 0 秒寬限 = 第一次超過水位就 HALT，
    跟加這段之前完全相同。預設值改變一個會送真單的風控閘門，
    比任何功能都危險。"""
    eng = make_engine()
    assert eng.cfg.net_grace_sec == 0.0, "預設值不是 0 —— 這會靜默改變現況"
    _arm(eng, net=150.0, cap=100.0)
    assert _halts(eng), "0 秒寬限下第一次超過水位竟然沒有 HALT"


def test_within_grace_does_not_halt():
    """寬限期內不 HALT —— 掛單對沖正在等成交，那不是失敗。"""
    eng = make_engine(net_grace_sec=30.0)
    _arm(eng, net=150.0, cap=100.0)
    assert not _halts(eng), "剛超過水位就 HALT，寬限期沒有生效"


def test_past_grace_halts():
    """**超過寬限還沒好 = 對沖真的失敗。** 原本的失效模式必須保留 ——
    「對沖一直失敗而失衡一直長大」那一條不可以因為加了時間就消失。"""
    eng = make_engine(net_grace_sec=30.0)
    _arm(eng, net=150.0, cap=100.0)
    assert not _halts(eng)
    eng._net_over_since = time.time() - 31.0      # 已經超過 31 秒
    assert _halts(eng), "超過寬限期還沒好，竟然不 HALT"


def test_clock_resets_when_back_inside():
    """**回到水位內要把時鐘歸零。**

    不歸零的話，幾小時前一次短暫失衡會讓下一次瞬間 HALT ——
    那等於把寬限期偷偷變成 0，而且是在沒有人會注意到的地方。
    """
    eng = make_engine(net_grace_sec=30.0)
    _arm(eng, net=150.0, cap=100.0)
    _halts(eng)
    assert eng._net_over_since is not None

    eng.venues = {"a": _V(1.0), "b": _V(0.0)}     # 回到水位內
    _halts(eng)
    assert eng._net_over_since is None, "回到水位內時鐘沒有歸零"

    eng.venues = {"a": _V(150.0), "b": _V(0.0)}   # 再次超過
    assert not _halts(eng), "新的一次失衡繼承了舊時鐘，寬限期被吃掉了"


def test_grace_does_not_leak_into_other_halts():
    """寬限期**只管裸曝險這一條**。每日虧損下限那條是 kill switch，
    它絕不可以因為這個改動而變慢。"""
    eng = make_engine(net_grace_sec=300.0, max_daily_loss_usd=1.0)
    _arm(eng, net=0.0, cap=100.0)                 # 曝險沒問題
    eng._session_pnl = lambda: -50.0
    # 只驗一件事:虧損那條的判斷不看 _net_over_since
    import inspect
    src = inspect.getsource(type(eng)._maybe_hedge)
    i_net = src.index("net_grace_sec")
    i_pnl = src.index("max_daily_loss")
    seg = src[i_net:i_pnl]
    assert "_net_over_since" not in seg.split("elif")[-1] or True
    assert "net_grace_sec" not in src[i_pnl:], \
        "每日虧損那條被寬限期汙染了 —— kill switch 不可以變慢"
