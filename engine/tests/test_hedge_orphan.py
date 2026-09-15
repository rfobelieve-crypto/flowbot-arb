# -*- coding: utf-8 -*-
"""撤單沒確認的對沖單：**不可以忘掉它，也不可以把它的成交判成「別人」**。

事故（2026-09-15，MON，engine/logs/MON/runner.log）:

    15:13:27  Lighter SELL 661 成交 -> HL 先掛 post-only BUY 661 對沖
    15:13:58  [HEDGE MAKER] 撤單回錯:RATE_LIMITED: HTTP 429
    15:14:01  撤單沒有確認 -> 殘量吃單 BUY 661（對的）-> **那張掛單被忘掉**
    ……那張掛單後來成交 -> HL +1522 vs 本地 +861
    15:34:52  HALTED: position moved +661 with no order from us in 1251s
              -> SELF-RESCUE 賣 660 -> net +0.2

兩個根因各一組測試:
  A. 輪詢間隔：HL 的 poll 是 REST（權重 2、IP 上限 1200/分），而舊版沿用
     報價腿的 0.05 秒 -> 一支迴圈就能吃光額度 -> 撤單拿到 429。
  B. 追蹤：撤單被拒要在預算內重送；預算用完仍未確認就登記成 orphan，
     由對帳迴圈繼續撤到交易所給出終態，期間它的成交歸因給我們。

每一關都有一個「故意觸發」的反向：把修法拿掉，它必須變紅。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
from test_maker import FakeVenue, make_engine, run             # noqa: E402
from entropy_arb import maker as mk                            # noqa: E402
from entropy_arb.maker import MakerOrder                       # noqa: E402


def _order():
    """報價腿**買進** 10 顆成交 -> 對沖是賣。"""
    return MakerOrder(venue_key="entropy", is_buy=True, qty=10.0, px=100.0,
                      sent_ts=time.time())


def _hl(key="hedge"):
    """對沖腿，用引擎自己的 venues 字典裡那個 key，否則 _we_touched 找不到它。"""
    v = FakeVenue(key, "HL", fee=4.5, maker_fee=1.5)
    v.set_book(99.0, 101.0)
    return v


def _engine_with_hl(**over):
    eng = make_engine(**over)
    v = _hl()
    eng.hedge = v
    eng.venues["hedge"] = v
    return eng, v


def _stuck_order(eng, v):
    """跑一次掛單對沖，交易所什麼都不肯說 -> 撤單未確認 -> 應該登記 orphan。"""
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    v.ex_filled, v.ex_status = 0.0, "open"
    v.poll_answers = False
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))


# ============================================================ A. 輪詢間隔

def test_poll_interval_default_is_the_old_behaviour():
    """**沒寫這個 key 的設定檔必須逐位元組不變**（CLAUDE.md §5）。"""
    eng = make_engine(hedge_maker_poll_sec="OMIT", maker_poll_sec=0.05)
    assert eng.cfg.hedge_maker_poll_sec == 0.0, "載入器預設不是 0"
    assert eng._hedge_poll_interval() == 0.05, "預設不再等於 min(maker_poll_sec, 0.25)"
    eng2 = make_engine(hedge_maker_poll_sec="OMIT", maker_poll_sec=0.5)
    assert eng2._hedge_poll_interval() == 0.25, "舊行為的 0.25 上限不見了"


def test_poll_interval_is_honoured_by_the_hedge_loop():
    """設 0.2 秒、試 1 秒 -> 輪詢次數必須是個位數，而不是 0.01 秒的上百次。

    反向：`_hedge_poll_interval` 回舊值（0.01）時這一關的次數會衝到五十以上。
    """
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=1.0,
                             hedge_maker_poll_sec=0.2, maker_poll_sec=0.01,
                             cancel_timeout_sec=0.4)
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    v.ex_filled, v.ex_status = 0.0, "open"

    def cancel_confirms(vv):
        vv.ex_status = "canceled"
    v.on_cancel = cancel_confirms
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert v.poll_count <= 8, \
        "1 秒內輪詢了 %d 次 —— 間隔沒有被採用（HL 上這就是 429 的來源）" % v.poll_count
    assert v.sent_takers, "沒成交卻沒吃單"


# ================================================ B1. 撤單在預算內重送

def test_rate_limited_cancel_is_resent_inside_the_budget():
    """**事故那一行**：第一次撤單回 429。舊版只送一次、剩下的預算都在輪詢
    一張沒被撤的單。修法之後必須重送，而第二次撤成功 -> 確認 -> 不留 orphan。
    """
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05,
                             hedge_maker_poll_sec=0.05, cancel_timeout_sec=1.5)
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    v.ex_filled, v.ex_status = 0.0, "open"
    v.cancel_result = {"status": "rejected",
                       "err": "RATE_LIMITED: HTTP 429 null"}
    calls = {"n": 0}

    def second_cancel_works(vv):
        calls["n"] += 1
        if calls["n"] >= 2:
            vv.cancel_result = {"status": "accepted", "err": None}
            vv.ex_status = "canceled"
    v.on_cancel = second_cancel_works
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert len(v.cancels) >= 2, "撤單被 429 拒絕之後沒有重送"
    assert not eng._hedge_orphans, "撤單已確認卻還登記成 orphan"
    assert v.sent_takers, "沒有吃殘餘"


def test_accepted_cancel_is_not_spammed():
    """交易所已經接受撤單就不要重送 —— 重送是給被拒的，不是給慢的。"""
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05,
                             hedge_maker_poll_sec=0.05, cancel_timeout_sec=0.6)
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    v.ex_filled, v.ex_status = 0.0, "open"      # 接受了，但遲遲沒終態
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert len(v.cancels) == 1, "撤單已被接受卻重送了 %d 次" % len(v.cancels)


# ============================================== B2. 未確認 -> 登記、不忘掉

def test_unconfirmed_cancel_is_tracked_not_forgotten():
    """**CLAUDE.md §2.3。** 反向：拿掉登記那段，這一關紅。"""
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    assert v.sent_takers, "未確認就不吃單 —— 那會裸著"
    assert len(eng._hedge_orphans) == 1, "撤單未確認的對沖單被忘掉了"
    o = eng._hedge_orphans[0]
    assert o.venue_key == "hedge" and o.handle == 1
    assert o.state == mk.UNKNOWN and not o.is_terminal
    assert o.is_buy is False, "orphan 的方向記反了（對沖是賣）"


def test_orphan_makes_a_later_position_move_ours():
    """**事故的 HALT 就死在這裡。** 21 分鐘後部位動了，歸因窗口早就過了。

    已知答案兩半：有 orphan -> 不 HALT、採信鏈上；沒有 orphan（控制組）
    -> 照舊 HALT。第二半確保這個修法沒有把「真的有人動帳戶」的守衛拆掉。
    """
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    v.last_traded_ts = time.time() - 1251.0        # 事故當下的數字
    # orphan 是**賣單**（對沖是賣）-> 它事後成交會讓部位**再少 10**。
    # 第一版寫成 +10 還是綠的,因為當時的歸因不看方向 —— 那正是審查抓到的洞。
    v.chain_position = v.position - 10.0
    run(eng._reconcile_venue(v, strict=False))
    assert not eng.halted, "自己的 orphan 成交被判成『有人動了帳戶』"
    assert abs(v.position - v.chain_position) < 1e-9, "沒有採信鏈上部位"

    # 控制組：同樣的變動，沒有 orphan -> 守衛必須照響
    eng2, v2 = _engine_with_hl()
    v2.last_traded_ts = time.time() - 1251.0
    v2.chain_position = 10.0
    run(eng2._reconcile_venue(v2, strict=False))
    assert eng2.halted, "沒有 orphan 的不明變動竟然不 HALT —— 守衛被拆了"


def test_orphan_does_not_excuse_a_move_in_the_wrong_direction():
    """**審查（2026-09-15）抓到的最大風險。** orphan 可以無限期存在,而第一版
    在那段期間把**任何**變動都算我們的 -> 真的強平／ADL 不會 HALT。
    賣單 orphan 不可能讓部位變多。"""
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    v.last_traded_ts = time.time() - 1251.0
    v.chain_position = v.position + 10.0           # 方向跟 orphan 相反
    run(eng._reconcile_venue(v, strict=False))
    assert eng.halted, "反方向的變動被 orphan 吞掉了 —— 守衛等於關掉"


def test_orphan_does_not_excuse_a_move_bigger_than_its_residual():
    """同方向但**比那張單還大** -> 多出來的是別人的 -> HALT。"""
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    v.last_traded_ts = time.time() - 1251.0
    v.chain_position = v.position - 500.0          # orphan 只剩 10 顆
    run(eng._reconcile_venue(v, strict=False))
    assert eng.halted, "一張 10 顆的 orphan 解釋了 500 顆的變動"


def test_orphan_does_not_explain_another_venue():
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    other = eng.entropy
    other.last_traded_ts = time.time() - 1251.0
    other.chain_position = other.position - 10.0
    run(eng._reconcile_venue(other, strict=False))
    assert eng.halted, "HL 上的 orphan 替 Lighter 的變動背書"


def test_orphan_pauses_new_quotes_and_counts_as_exposure():
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    assert eng._scan_maker(time.time()) is None, "有 orphan 還在開新報價"
    assert eng._has_exposure(), "一張可能還活著的單不算曝險"


# ===================================== B3. 對帳迴圈把它追到交易所的終態

def test_service_keeps_cancelling_until_terminal():
    """交易所還說 open -> 繼續撤、繼續留著。**不放棄。**"""
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    n0 = len(v.cancels)
    v.poll_answers = True
    v.ex_status = "open"
    run(eng._service_hedge_orphans())
    run(eng._service_hedge_orphans())
    assert len(eng._hedge_orphans) == 1, "還沒終態就被移除了 —— 那是『以為撤掉了』"
    assert len(v.cancels) >= n0 + 2, "對帳迴圈沒有繼續撤單"


def test_service_resolves_a_late_fill_without_booking_it_twice():
    """它事後成交了 -> 移除、蓋 last_traded_ts、觸發對帳。**本地部位不動**:
    鏈上讀數是完整的，採信之後不准再疊本地 delta（CLAUDE.md §2.4）。
    """
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    pos_before = v.position
    eng._reconcile_evt.clear()
    v.poll_answers = True
    v.ex_status, v.ex_filled = "filled", 10.0
    t = time.time()
    run(eng._service_hedge_orphans())
    assert not eng._hedge_orphans, "交易所說成交了卻還留著"
    assert v.position == pos_before, "orphan 的成交被記進本地部位 —— 對帳會再記一次"
    assert v.last_traded_ts >= t, "沒有蓋時間戳，接下來的對帳會把它判成別人"
    assert eng._reconcile_evt.is_set(), "沒有觸發對帳"
    # 接下來的對帳（在歸因窗口內）必須認得它。
    #
    # **第一版這一段是空轉的**（審查 2026-09-15 抓到）：時間戳剛蓋上,
    # `_reconcile_venue` 撞到 RECONCILE_GRACE_SEC（5 秒）第一行就 return,
    # 根本沒讀部位、沒判歸因 —— 下面兩個 assert 在任何實作下都會過。
    # 真實迴圈是「跳過這一輪、15 秒後下一輪、仍在 30 秒歸因窗內」。
    # 這裡把時鐘往前撥到那一刻（寬限已過、窗口未過）,而不是關掉寬限。
    v.last_traded_ts -= eng.RECONCILE_GRACE_SEC + cfg_reconcile_gap(eng)
    v.chain_position = v.position - 10.0
    run(eng._reconcile_venue(v, strict=False))
    assert abs(v.position - v.chain_position) < 1e-9, \
        "對帳沒有真的跑（沒採信鏈上）—— 下面那個 assert 會是空轉"
    assert not eng.halted, "orphan 已解決後的對帳仍然 HALT"


def cfg_reconcile_gap(eng):
    """對帳迴圈跳過寬限期之後，下一輪離時間戳多遠：一個 reconcile_sec。
    必須仍小於歸因窗口，否則這條路徑在真實節奏下本來就會 HALT。"""
    gap = eng.cfg.reconcile_sec
    assert eng.RECONCILE_GRACE_SEC + gap < eng._attribution_window(), \
        "設定讓『解決後的下一輪對帳』落在歸因窗口外 —— 真實迴圈會誤判 HALT"
    return gap


def test_late_fill_after_the_attribution_window_still_halts():
    """控制組：orphan 解決之後**超過歸因窗口**才出現的變動不是我們的。
    確保蓋時間戳沒有變成一張永久的免責卡。"""
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    v.poll_answers = True
    v.ex_status, v.ex_filled = "filled", 10.0
    run(eng._service_hedge_orphans())
    v.last_traded_ts -= eng._attribution_window() + 1.0
    v.chain_position = v.position - 10.0
    run(eng._reconcile_venue(v, strict=False))
    assert eng.halted, "歸因窗口外的變動被當成我們的"


def test_service_resolves_a_clean_cancel():
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)
    ts_before = v.last_traded_ts
    v.poll_answers = True
    v.ex_status, v.ex_filled = "canceled", 0.0
    run(eng._service_hedge_orphans())
    assert not eng._hedge_orphans
    assert v.last_traded_ts == ts_before, "乾淨撤掉的單不該蓋時間戳（會把真的不明變動藏起來）"


def test_service_survives_venue_errors():
    """輪詢與撤單都炸 -> 不拋、不移除。對帳迴圈不可以被它弄死。"""
    eng, v = _engine_with_hl(hedge_maker_timeout_sec=0.05)
    _stuck_order(eng, v)

    async def boom(handle):
        raise RuntimeError("down")
    v.poll_order = boom
    v.cancel_order = boom
    run(eng._service_hedge_orphans())
    assert len(eng._hedge_orphans) == 1


def test_unresolved_log_does_not_trip_the_quote_leg_auto_restart():
    """`tools/halt_recover.py` 看到 `MAKER ORDER STILL UNRESOLVED (N cancel
    attempts)` 就會殺行程重啟。對沖腿的 orphan 用**不同的字**，免得被當成
    報價腿死鎖 —— 那個自動恢復的判準是為另一種狀況寫的。
    """
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "entropy_arb", "engine.py"), encoding="utf-8").read()
    i = src.index("async def _service_hedge_orphans")
    body = src[i:src.index("def _book_hedge_fill", i)]
    assert "HEDGE ORDER STILL UNRESOLVED" in body
    assert "MAKER ORDER STILL UNRESOLVED" not in body


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
