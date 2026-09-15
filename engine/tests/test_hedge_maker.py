# -*- coding: utf-8 -*-
"""對沖先掛單：**重點是它失敗的時候是安全的失敗**。

這條路徑夾在「我們剛成交」與「我們回到對沖」之間 —— 它卡住就是裸著卡住。
所以這支測的順序是：先證明預設不變，再證明每一種失敗都退回吃單。

為什麼要做（2026-09-15，逐筆分解 n=30）:
    掛單腿毛利 +4.08 bps   對沖成本 −5.55   ->   淨 **−1.47**
    HL 吃單 4.50 -> 掛單 1.50，且掛單不穿 0.65 價差 ->  **+2.18**
費率階梯搆不到（VIP 要 $5M、做市商負費率要日均 $28M，我們累計 $695），
所以這是唯一能單獨翻正符號的槓桿。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
from test_maker import FakeVenue, make_engine, run             # noqa: E402
from entropy_arb.maker import MakerOrder                       # noqa: E402


def _order():
    """一張**買進**的報價成交了 10 顆 —— 所以對沖是賣。"""
    return MakerOrder(venue_key="entropy", is_buy=True, qty=10.0, px=100.0,
                      sent_ts=time.time())


def _venue(**kw):
    """對沖腿：吃單 4.50 bps、掛單 1.50 bps（HL 今天查到的真實費率）。"""
    v = FakeVenue("hl", "HL", fee=4.5, maker_fee=1.5)
    v.set_book(99.0, 101.0)
    for k, val in kw.items():
        setattr(v, k, val)
    return v


def test_default_goes_straight_to_taker():
    """**這一關最重要。** 預設 0.0 = 完全不試掛單。

    這條路徑動的是裸曝險的長度，預設值不可以靜默改變它。
    """
    eng = make_engine()
    assert eng.cfg.hedge_maker_timeout_sec == 0.0, "預設值不是 0 —— 會靜默改變現況"
    v = _venue()
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert v.sent_takers, "預設竟然沒有走吃單"
    assert not v.sent_makers, "預設竟然掛了單"


def test_config_without_the_key_loads_as_off():
    """**live 的設定檔沒有寫這一行。**

    所以真正保護現況的是**載入器的預設值**，不是測試樣板裡的那個 0.0。
    上面那一關驗的是樣板，這一關驗的是 `config_MON.yaml` 實際會走的路。
    """
    eng = make_engine(hedge_maker_timeout_sec="OMIT")
    assert eng.cfg.hedge_maker_timeout_sec == 0.0, \
        "設定檔沒寫這個 key 時，載入器的預設不是 0 —— live 會靜默開啟"
    v = _venue()
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert not v.sent_makers and v.sent_takers


def test_maker_fills_so_no_taker_is_sent():
    """掛單全部成交 -> 不再吃單。這就是省下 3.0 bps 的那條路。"""
    eng = make_engine(hedge_maker_timeout_sec=1.0)
    v = _venue()
    v.send_maker_result = {"status": "filled", "filled_base": 10.0}
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert v.sent_makers, "沒有先試掛單"
    assert not v.sent_takers, "掛單已經成交了卻還去吃單 —— 會變成過度對沖"
    assert abs(v.position + 10.0) < 1e-9, "賣出的部位沒有記進去"


def test_maker_fee_is_used_not_taker_fee():
    """**記帳要用掛單費率。**

    用吃單費率記的話，省下來的 3 bps 只存在於帳面上而不在現金裡 ——
    而下一個看報表的人會以為省到了。
    """
    eng = make_engine(hedge_maker_timeout_sec=1.0)
    v = _venue()
    v.send_maker_result = {"status": "filled", "filled_base": 10.0,
                           "avg_px": 101.0}
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    maker = 10 * 101.0 * (1 - 1.5 / 1e4)
    taker = 10 * 101.0 * (1 - 4.5 / 1e4)
    assert abs(v.cash - maker) < 1e-6, \
        "現金 %.6f，掛單費率該是 %.6f（吃單費率會是 %.6f）" % (v.cash, maker, taker)


def test_venue_reported_price_wins_over_the_one_we_posted():
    """交易所回報均價就以它為準。

    我們掛在 ask(101) 而它回報 100.5 —— 記成 101 會**高估收入 0.5%**，
    而那個方向剛好是「看起來有賺」。同一條規則 `MakerOrder.fill_px` 已經
    寫在報價腿上了，對沖腿不可以例外。
    """
    eng = make_engine(hedge_maker_timeout_sec=1.0)
    v = _venue()
    v.send_maker_result = {"status": "filled", "filled_base": 10.0,
                           "avg_px": 100.5}
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert abs(v.cash - 10 * 100.5 * (1 - 1.5 / 1e4)) < 1e-6, \
        "記在我們掛的價，不是交易所回報的價：%.6f" % v.cash


def test_unfilled_maker_falls_back_to_taker():
    """掛單沒成交 -> 退回吃單。**這是最常發生的路徑**，薄市場尤其。"""
    eng = make_engine(hedge_maker_timeout_sec=0.05)
    v = _venue()
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    v.ex_filled, v.ex_status = 0.0, "open"
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert v.sent_makers and v.sent_takers, "沒有退回吃單 —— 那會裸著"
    assert v.cancels, "沒成交卻沒送撤單"
    assert abs(v.sent_takers[-1][1] - 10.0) < 1e-6, "吃單數量不是全額"


def test_partial_maker_fill_only_takes_the_residual():
    """部分成交 -> 吃單**只吃殘餘**。

    吃整張的話會變成過度對沖到另一邊 —— 而那比沒對沖更難發現，
    因為 net 看起來只是換了個符號。
    """
    eng = make_engine(hedge_maker_timeout_sec=0.05)
    v = _venue()
    v.send_maker_result = {"status": "resting", "filled_base": 4.0}
    v.ex_filled, v.ex_status = 4.0, "open"
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert v.sent_takers, "沒有吃殘餘"
    qty = v.sent_takers[-1][1]
    assert abs(qty - 6.0) < 1e-6, "吃單數量應該是殘餘 6，實際 %s" % qty
    assert abs(v.position + 10.0) < 1e-9, "兩段加起來不是一張單：%s" % v.position


def test_send_failure_falls_back_instead_of_raising():
    """掛單送不出去 -> 安靜退回吃單。**在對沖路徑上拋例外 = 裸著**。"""
    eng = make_engine(hedge_maker_timeout_sec=1.0)
    v = _venue()

    async def boom(**kw):
        raise RuntimeError("venue down")
    v.send_maker = boom
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert v.sent_takers, "掛單送失敗之後沒有退回吃單"
    assert abs(v.sent_takers[-1][1] - 10.0) < 1e-6, "沒有吃全額"


def test_poll_failure_does_not_strand_the_hedge():
    """輪詢炸掉 -> 跳出迴圈退回吃單，**不可以卡在那裡**。

    撤單確認的死鎖今天發生過兩次（74 分 + 81 分），而那是在報價腿上；
    同樣的事發生在對沖腿上就是裸著卡住。
    """
    eng = make_engine(hedge_maker_timeout_sec=5.0)
    v = _venue()
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}

    async def boom(handle):
        raise RuntimeError("poll down")
    v.poll_order = boom
    t0 = time.time()
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert time.time() - t0 < 4.0, "輪詢炸了卻還在等滿 5 秒逾時"
    assert v.sent_takers, "沒有退回吃單"


def test_cancel_failure_still_falls_back_to_taker():
    """**撤單失敗不可以擋住吃單。**

    這是 09-15 那兩次死鎖的形狀：撤單沒有回來。在報價腿上那是空轉，
    在這裡那是裸著 —— 所以這條路只送撤單、不等它，撤單炸了照樣吃。
    """
    eng = make_engine(hedge_maker_timeout_sec=0.05)
    v = _venue()
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}

    async def boom(handle):
        raise RuntimeError("cancel stuck")
    v.cancel_order = boom
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert v.sent_takers, "撤單失敗就不吃單了 —— 那會裸著"


def test_fill_that_lands_during_the_cancel_is_caught():
    """**撤單送出去之後才成交 -> 必須在預算內抓到，不可以再吃一次單。**

    這是實盤 2026-09-15 13:25 咬到的那一筆:撤掉的 post-only SELL 658
    沒有真的被撤掉，90 秒後成交 -> HL 多空了 657 -> 過度對沖 -> HALT ->
    自救買回。那一筆掛單對沖要省 $0.003，這個失效花掉 $0.010 加四分鐘停機。
    """
    eng = make_engine(hedge_maker_timeout_sec=0.05)
    v = _venue()
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    v.ex_filled, v.ex_status = 0.0, "open"

    def fills_on_cancel(vv):
        vv.ex_filled, vv.ex_status = 10.0, "filled"   # 撤單那一刻成交了
    v.on_cancel = fills_on_cancel

    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert not v.sent_takers, \
        "撤單後才成交卻還去吃單 —— 那就是實盤那次的過度對沖"
    assert abs(v.position + 10.0) < 1e-9, "部位不是一張單:%s" % v.position


def test_unconfirmed_cancel_still_hedges():
    """**確認不到仍然要吃單。** 不可以因為不確定就裸著 ——
    裸著比過度對沖貴得多（過度對沖有淨額對沖會收，裸著沒有）。"""
    eng = make_engine(hedge_maker_timeout_sec=0.05)
    v = _venue()
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    v.ex_filled, v.ex_status = 0.0, "open"
    v.poll_answers = False              # 交易所什麼都不肯說
    t0 = time.time()
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    assert v.sent_takers, "撤單確認不到就不吃單了 —— 那會裸著"
    assert time.time() - t0 < 3.0, "確認沒有預算，等太久了"


def test_post_only_rests_on_our_own_side():
    """post-only 要掛在**自己這一側的觸價**。

    掛進對手價會被 ALO 拒絕，而那會讓這條路徑每次都白跑一趟 ——
    白跑不會報錯，只會讓省費率的效果一直是 0。
    """
    eng = make_engine(hedge_maker_timeout_sec=0.05)
    v = _venue()
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    # order.is_buy=True -> 對沖是 SELL -> 要掛在 ask(101) 不是 bid(99)
    run(eng._hedge_maker_fill(eng.entropy, v, _order(), 10.0))
    is_buy, _, px = v.sent_makers[0]
    assert is_buy is False, "對沖方向反了"
    assert px >= 101.0, "賣單掛在 %s，那會穿進買價被 ALO 拒絕" % px


def test_buy_side_rests_on_the_bid():
    """對稱的另一半：報價腿賣出 -> 對沖買進 -> 掛在 bid(99)。

    只測賣那一側的話，`px_round` 的進位方向寫反了也不會紅。
    """
    eng = make_engine(hedge_maker_timeout_sec=0.05)
    v = _venue()
    v.send_maker_result = {"status": "resting", "filled_base": 0.0}
    o = MakerOrder(venue_key="entropy", is_buy=False, qty=10.0, px=100.0,
                   sent_ts=time.time())
    run(eng._hedge_maker_fill(eng.entropy, v, o, 10.0))
    is_buy, _, px = v.sent_makers[0]
    assert is_buy is True, "對沖方向反了"
    assert px <= 99.0, "買單掛在 %s，那會穿進賣價被 ALO 拒絕" % px


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
