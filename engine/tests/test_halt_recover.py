# -*- coding: utf-8 -*-
"""HALT 自動恢復：**證明每一道關卡都擋得住**，不只證明它會動。

這支測的是一個會**殺掉正在送真單的行程**的工具，所以重要的不是
「該恢復的時候會恢復」，是「**不該恢復的時候每一條路都被擋住**」。

用的 HALT 字串全部是 `engine._risk_halt` 真的會產生的那些
（從 entropy_arb/engine.py 逐字抄，不是我編的）。
"""
import io
import json
import os
import sys
import time

import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ENGINE, "tools"))

import halt_recover as hr                                   # noqa: E402

# 真實字串。前四個來自 engine.py 的 _risk_halt 呼叫點。
R_NET = ("HALTED: net imbalance +638.2 exceeds max_net_base 491.873 — the "
         "legs are no longer hedging each other")
R_PNL = "HALTED: session PnL $-10.4000 below floor $10.00"
R_GROSS = "HALTED: gross exposure $141.20 exceeds max_gross_usd $130.00"
R_ERRS = "HALTED: 3 consecutive errors"
R_CRASH = ("HALTED: the maker order loop crashed while an order may still "
           "be resting")


def _mk(tmp, pair, ok, reason, net, tol=2.0):
    """造一個假的 logs/<pair>/ 與設定，把 hr 指到它。"""
    os.makedirs(os.path.join(tmp, "logs", pair), exist_ok=True)
    io.open(os.path.join(tmp, "config_%s.yaml" % pair), "w",
            encoding="utf-8").write(
        yaml.safe_dump({"execution": {"net_tolerance_base": tol}}))
    sj = os.path.join(tmp, "logs", pair, "status.json")
    io.open(sj, "w", encoding="utf-8").write(json.dumps(
        {"ok": ok, "reason": reason, "private": {"net_base": net}}))
    os.utime(sj, (time.time(), time.time()))
    hr.ENGINE = tmp
    return sj


@pytest.fixture(autouse=True)
def _no_kill(monkeypatch):
    """測試絕不可以真的殺行程。被呼叫就記下來。"""
    killed = []
    monkeypatch.setattr(hr, "_pids", lambda pair: [4242])
    monkeypatch.setattr(
        hr.subprocess, "run",
        lambda *a, **k: killed.append(a) or type("R", (), {"stdout": ""})())
    return killed


def _run(pair="MON"):
    return hr.main.__wrapped__() if hasattr(hr.main, "__wrapped__") else None


def _call(tmp, pair, argv):
    sys.argv = ["halt_recover.py", "--pair", pair] + argv
    return hr.main()


def test_recovers_when_the_condition_has_cleared(tmp_path, _no_kill, capsys):
    """唯一該恢復的情況：net imbalance HALT，而裸曝險已經平掉了。"""
    _mk(str(tmp_path), "MON", ok=False, reason=R_NET, net=0.2, tol=2.0)
    _call(str(tmp_path), "MON", [])
    out = capsys.readouterr().out
    assert "自動恢復[HALT/裸曝險]" in out, out
    assert _no_kill, "該重啟卻沒有殺行程"


def test_does_not_recover_while_the_imbalance_is_still_there(tmp_path,
                                                             _no_kill, capsys):
    """**這一關是整支工具的核心。**

    同一種 HALT、同一個原因字串 —— 只差裸曝險還在。平倉沒成功就重啟，
    等於讓一個部位對不上的引擎重新開始交易。
    """
    _mk(str(tmp_path), "MON", ok=False, reason=R_NET, net=638.2, tol=2.0)
    _call(str(tmp_path), "MON", [])
    out = capsys.readouterr().out
    assert "裸曝險還在" in out, out
    assert not _no_kill, "裸曝險還在卻殺了行程"


@pytest.mark.parametrize("reason,label", [
    (R_PNL, "每日虧損 kill switch"),
    (R_GROSS, "曝險上限"),
    (R_ERRS, "連續錯誤"),
    (R_CRASH, "maker 迴圈崩潰"),
])
def test_other_halt_kinds_are_never_auto_recovered(tmp_path, _no_kill,
                                                   capsys, reason, label):
    """其他每一類 HALT 都要人。**裸曝險是 0 也一樣不准恢復** ——
    用 net=0 是刻意的：證明擋住它的是**種類**，不是狀態。"""
    _mk(str(tmp_path), "MON", ok=False, reason=reason, net=0.0, tol=2.0)
    _call(str(tmp_path), "MON", [])
    out = capsys.readouterr().out
    assert "不自動恢復" in out, "%s 竟然被放行：%s" % (label, out)
    assert not _no_kill, "%s 竟然殺了行程" % label


def test_budget_blocks_a_restart_loop(tmp_path, _no_kill, capsys):
    """持續出問題時不可以一直拉它起來 —— 那會變成無人看管的重啟迴圈。"""
    tmp = str(tmp_path)
    _mk(tmp, "MON", ok=False, reason=R_NET, net=0.2, tol=2.0)
    now = time.time()
    hr._save("MON", {"restarts": [now - 60, now - 30]})     # 已用滿 2 次
    _call(tmp, "MON", [])
    out = capsys.readouterr().out
    assert "預算用完" in out, out
    assert not _no_kill, "預算用完卻還是殺了行程"


def test_stale_status_is_the_watchdogs_job_not_ours(tmp_path, _no_kill,
                                                    capsys):
    """status.json 很舊 = 行程死了，不是 HALT。那是看門狗的事，別插手。"""
    tmp = str(tmp_path)
    sj = _mk(tmp, "MON", ok=False, reason=R_NET, net=0.2, tol=2.0)
    old = time.time() - hr.STATUS_MAX_AGE_SEC - 60
    os.utime(sj, (old, old))
    _call(tmp, "MON", [])
    out = capsys.readouterr().out
    assert "沒動" in out, out
    assert not _no_kill


def test_healthy_engine_is_silent(tmp_path, _no_kill, capsys):
    """ok=True 什麼都不做、什麼都不印 —— 每 5 分鐘一行「還好」會變雜訊。"""
    _mk(str(tmp_path), "MON", ok=True, reason="running", net=0.2)
    _call(str(tmp_path), "MON", [])
    assert capsys.readouterr().out.strip() == ""
    assert not _no_kill


def _log(tmp, pair, attempts, tail_pad=0):
    """造一份 runner.log，最後幾行帶 UNRESOLVED。tail_pad = 之後再墊幾行。"""
    p = os.path.join(tmp, "logs", pair, "runner.log")
    lines = ["09:00:0%d INFO engine: [status] ok" % (i % 10) for i in range(50)]
    if attempts is not None:
        lines.append("09:59:07.736 CRITICAL engine: MAKER ORDER STILL "
                     "UNRESOLVED (%d cancel attempts): hedge BUY 461@0.02275"
                     % attempts)
    lines += ["09:59:1%d INFO engine: [status] ok" % (i % 10)
              for i in range(tail_pad)]
    io.open(p, "w", encoding="utf-8").write("\n".join(lines))


def test_recovers_from_the_cancel_deadlock(tmp_path, _no_kill, capsys):
    """撤單死鎖：**ok=True**，所以任何從 HALT 出發的檢查都看不到它 ——
    而它是 13 小時裡吃掉 20% 時間的那個（HALT 那個只發生過一次）。"""
    tmp = str(tmp_path)
    _mk(tmp, "MON", ok=True, reason="live, quoting", net=0.2, tol=2.0)
    _log(tmp, "MON", attempts=814)
    _call(tmp, "MON", [])
    out = capsys.readouterr().out
    assert "撤單死鎖" in out, out
    assert _no_kill, "卡死了卻沒重啟"


def test_normal_retries_are_left_alone(tmp_path, _no_kill, capsys):
    """撤單重試本來就會發生（cancel_timeout_sec 3 秒）。
    **次數少 = 正常，不可以碰** —— 不然每次撤單都在重啟引擎。"""
    tmp = str(tmp_path)
    _mk(tmp, "MON", ok=True, reason="live, quoting", net=0.2, tol=2.0)
    _log(tmp, "MON", attempts=5)
    _call(tmp, "MON", [])
    out = capsys.readouterr().out
    assert "正常重試範圍" in out, out
    assert not _no_kill, "正常重試竟然觸發重啟"


def test_old_deadlock_further_up_the_log_is_history_not_now(tmp_path,
                                                            _no_kill, capsys):
    """**這一關擋的是「已經好了的舊事故」。**

    卡住時引擎每分鐘印一次，所以 UNRESOLVED 出現在 log 尾端 = 現在還在卡。
    只出現在更前面 = 它已經恢復了，那是歷史 —— 拿歷史去重啟一個正在
    好好報價的引擎，比不重啟還糟。
    """
    tmp = str(tmp_path)
    _mk(tmp, "MON", ok=True, reason="live, quoting", net=0.2, tol=2.0)
    _log(tmp, "MON", attempts=814, tail_pad=60)   # 之後又正常跑了 60 行
    _call(tmp, "MON", [])
    assert not _no_kill, "拿舊事故重啟了一個健康的引擎"


def test_deadlock_with_exposure_still_open_is_not_touched(tmp_path,
                                                          _no_kill, capsys):
    """死鎖 + 裸曝險還在 -> 不碰。重啟會讓一個部位對不上的引擎重新交易。"""
    tmp = str(tmp_path)
    _mk(tmp, "MON", ok=True, reason="live, quoting", net=638.2, tol=2.0)
    _log(tmp, "MON", attempts=814)
    _call(tmp, "MON", [])
    assert "裸曝險還在" in capsys.readouterr().out
    assert not _no_kill


def test_budget_is_shared_between_both_kinds(tmp_path, _no_kill, capsys):
    """預算**共用**：兩種狀態加起來不得超過上限。
    分開算的話，一個每五分鐘換一種症狀的引擎會被無限重啟。"""
    tmp = str(tmp_path)
    _mk(tmp, "MON", ok=True, reason="live, quoting", net=0.2, tol=2.0)
    _log(tmp, "MON", attempts=814)
    now = time.time()
    hr._save("MON", {"restarts": [now - 60, now - 30]})   # HALT 那邊用掉的
    _call(tmp, "MON", [])
    assert "預算用完" in capsys.readouterr().out
    assert not _no_kill


def test_tolerance_comes_from_config_not_a_constant(tmp_path, _no_kill,
                                                    capsys):
    """容忍值要從設定讀。寫死就是第二份實作，會安靜地跟引擎不一致。"""
    tmp = str(tmp_path)
    # 同一個 net=1.5：容忍 2.0 時該恢復，容忍 1.0 時不該
    _mk(tmp, "MON", ok=False, reason=R_NET, net=1.5, tol=1.0)
    _call(tmp, "MON", [])
    assert "裸曝險還在" in capsys.readouterr().out
    assert not _no_kill

    _mk(tmp, "MON", ok=False, reason=R_NET, net=1.5, tol=2.0)
    _call(tmp, "MON", [])
    assert "自動恢復[HALT/裸曝險]" in capsys.readouterr().out
    assert _no_kill
