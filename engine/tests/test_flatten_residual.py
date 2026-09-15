# -*- coding: utf-8 -*-
"""`tools/flatten_residual.py`：**沒有帳戶串流就不送單，而且兩個寫入者不搶部位。**

2026-09-15 使用者：「缺少帳戶 WS 串流，這個也處理一下」。那天清 MON 的 −0.8
顆殘量，工具只起了簽章沒起串流，送單回 `sent-unconfirmed` —— 最後靠讀 REST
部位才知道平掉了。這裡用假場館把三條路徑逼出來：串流不 ready、串流 ready、
引擎還在跑。
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "tools"))
import flatten_residual as fr                                   # noqa: E402


class FakeLighter:
    """只做這支工具用得到的表面。`ready_after` = 幾次 ready 詢問之後才 ready。"""

    def __init__(self, conf, session, settle):
        self.min_base, self.min_quote = 350.0, 10.0
        self.pos = -0.8
        self.ready_after = FakeLighter.READY_AFTER
        self._asks = 0
        self.sent, self.cancelled_all, self.started = [], 0, False
        FakeLighter.last = self

    async def load_market(self):
        pass

    def init_signer(self):
        pass

    async def fetch_position(self):
        return self.pos

    def px_round(self, px, up):
        return round(px, 5)

    def start_tasks(self, stop, notify, live):
        self.started = live

        async def idle():
            await stop.wait()
        return [asyncio.ensure_future(idle())]

    def ready_to_trade(self):
        self._asks += 1
        return self.started and self._asks > self.ready_after

    async def cancel_open_orders(self):
        self.cancelled_all += 1
        return 0

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        self.sent.append((is_buy, qty, limit_px, reduce_only))
        self.pos = 0.0
        return {"status": "filled", "filled_base": qty, "avg_px": limit_px,
                "err": None, "unresolved": False}

    async def close(self):
        pass


async def _mark(session, sym):
    return 0.02233


async def _nosleep(_s):
    await asyncio.sleep(0)


def _args(live=True, cfgdir=None):
    return argparse.Namespace(symbol="MON", config=_cfg(cfgdir), hedge="lighter",
                              max_usd=30.0, live=live)


def _cfg(d):
    d = d or tempfile.mkdtemp(prefix="arb-flat-")
    p = os.path.join(d, "cfg.yaml")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(
            "thresholds: {midline_bps: 0.0, upper_bps: 3.0, lower_bps: 3.0}\n"
            "logging:\n  trades_csv: %s/trades.csv\n  status_json: %s/status.json\n"
            % (d.replace("\\", "/"), d.replace("\\", "/")))
    return p


def _run(**kw):
    return asyncio.run(fr.run(kw.pop("a"), venue_cls=FakeLighter, mark_fn=_mark,
                              sleep=_nosleep))


def test_refuses_to_send_when_the_account_stream_never_becomes_ready():
    """**事故的形狀**：沒有串流 = 送出去也確認不了。必須不送。"""
    FakeLighter.READY_AFTER = 10 ** 9
    rc = _run(a=_args())
    v = FakeLighter.last
    assert v.started, "沒有起帳戶串流"
    assert rc == 3, "串流沒 ready 卻沒有以 3 結束（rc=%s）" % rc
    assert not v.sent, "串流沒 ready 還送了單 —— 那就是 09-15 的 sent-unconfirmed"


def test_sends_reduce_only_after_the_stream_is_ready():
    FakeLighter.READY_AFTER = 3
    rc = _run(a=_args())
    v = FakeLighter.last
    assert rc == 0, rc
    assert v.sent and v.sent[0][3] is True, "沒有送 reduce_only"
    is_buy, qty, _px, _ro = v.sent[0]
    assert is_buy is True and abs(qty - 0.8) < 1e-12, "空單殘量應該買回 0.8"
    assert v.cancelled_all == 1, "送單前沒有先撤掛單"


def test_dry_run_starts_nothing_and_sends_nothing():
    FakeLighter.READY_AFTER = 0
    rc = _run(a=_args(live=False))
    v = FakeLighter.last
    assert rc == 0 and not v.sent and not v.started


def test_engine_guard_refuses_a_running_unhalted_engine():
    d = tempfile.mkdtemp(prefix="arb-flat-")
    sp = os.path.join(d, "status.json")
    json.dump({"ok": True, "reason": ""}, open(sp, "w", encoding="utf-8"))
    ok, why = fr.engine_guard(sp, time.time())
    assert not ok, "引擎在跑而且沒有 HALT 竟然放行：%s" % why
    # 整條路徑：rc 4、連場館都不建
    FakeLighter.READY_AFTER = 0
    FakeLighter.last = None
    rc = _run(a=_args(cfgdir=d))
    assert rc == 4 and FakeLighter.last is None, "守衛擋了但仍然建了場館／送了單"


def test_engine_guard_allows_halted_or_stale_engines():
    d = tempfile.mkdtemp(prefix="arb-flat-")
    sp = os.path.join(d, "status.json")
    json.dump({"ok": False, "reason": "HALTED: restart required"},
              open(sp, "w", encoding="utf-8"))
    assert fr.engine_guard(sp, time.time())[0], "停在 HALT 的引擎應該放行"
    json.dump({"ok": True, "reason": ""}, open(sp, "w", encoding="utf-8"))
    assert fr.engine_guard(sp, time.time() + 10 * fr.ENGINE_FRESH_SEC)[0], \
        "狀態檔早就不更新了，應該放行"
    assert fr.engine_guard(os.path.join(d, "nope.json"), time.time())[0]


def test_engine_guard_refuses_an_unreadable_fresh_status():
    d = tempfile.mkdtemp(prefix="arb-flat-")
    sp = os.path.join(d, "status.json")
    open(sp, "w", encoding="utf-8").write("{not json")
    assert not fr.engine_guard(sp, time.time())[0], "讀不懂新的狀態檔卻放行（fail-open）"
