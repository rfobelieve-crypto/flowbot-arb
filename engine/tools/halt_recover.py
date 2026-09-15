# -*- coding: utf-8 -*-
"""從**兩種**已知的卡死狀態自動恢復，而判準是「狀態已經好了」不是「原因」。

2026-09-15，使用者選了「只自動重啟那一類，其他照舊要人」。

**兩種狀態，而第二種是後來才量出來是主要的那個：**

    1. HALT/裸曝險   ok=False  —— 13 小時內發生 **1 次**，停機 106 分鐘
    2. 撤單死鎖      ok=True   —— 13 小時內發生 **2 次**，停機 155 分鐘（20%）

第二種不是 HALT，`status.json` 是 `ok=True` —— 引擎只是永遠重試。所以
任何從 HALT 出發的檢查**結構上都看不到它**，而它才是貴的那個。

===========================================================================
為什麼需要它
===========================================================================
2026-09-15 06:10 MON 成交的那一瞬間，HL 對沖被限流擋掉（HTTP 429）：

    06:10:48.812  [QUOTE FILL] LIGHTER BUY 639
    06:10:48.863  [QUOTE HEDGE] HL SELL 0/639 send-failed — RATE_LIMITED 429
    06:10:48.956  CRITICAL HALTED: net imbalance +638.2 exceeds max_net_base
    06:11:04.989  [HEDGE] net +638.2 — SELL 638 on LIGHTER  <- 自己平掉了
    06:11:05.231  部位回到 net +0.2

**風控全部做對了**：144 毫秒內 HALT、16 秒內把對不到沖的部位就地平掉。
而 HALT 是單向的（設計如此：要人看一眼再重啟），所以它就停在那裡 ——
**1 小時 46 分，佔那個累積窗口的 23%**，而我七小時後才看到。

===========================================================================
允許清單：**一條**，而且它是狀態不是原因
===========================================================================
`engine._risk_halt` 有七類 HALT。只有這一類可以自動恢復：

    net imbalance ... exceeds max_net_base     ∧   現在 |net| <= net_tolerance_base

第二個條件才是重點：**它說「當初讓它 HALT 的那件事已經不成立了」**。
引擎會試 `halt_flatten_attempts` 次 reduce-only 把它平掉；平成功了，重啟
（會 strict=True 重讀真實部位）就是安全的。**沒平成功就不要碰** ——
那時重啟等於讓一個部位對不上的引擎重新開始交易。

比對「原因是不是 429」是**錯的判準**：429 只是起因，而下一次可能是別的
起因造成同一個狀態，也可能 429 造成一個完全不同、不該自動恢復的狀態。

**明文不可自動恢復**（列出來，因為清單的形狀決定它看得見什麼）：

    session PnL below floor  -> 那是每日虧損 kill switch。自動重啟它
                                = 把 kill switch 拆掉，這條絕不放寬
    gross exposure / 帳戶上限 -> 曝險本身太大，不是暫時的
    N consecutive errors      -> 引擎在壞，不是市場在壞
    maker 迴圈崩潰            -> 可能有一張我們不知道的單在簿上
    簿口過期 / 波動熔斷        -> 要人判斷市場狀況

===========================================================================
邊界
===========================================================================
這支住在 arb，因為**重啟的權限屬於 arb**（CLAUDE.md §第 4 線的隔離：
flow_system 讀它，它不讀 flow_system）。它不送 Discord —— 它寫一份
`logs/<pair>/halt_recover.json`，由 `ops/hmm_watch.py` 讀去報。
（2026-09-15 之前看護住在 flow_system；HMM 整條搬進 arb 之後兩者同在這裡，
告警走 arb 自己的 `ops/alert.py`。這支仍然不送：「動手」與「回報」分成
兩個行程，一個卡住不會拖死另一個。）

用法（由 arb_watchdog.ps1 每 5 分鐘呼叫）：
    python tools/halt_recover.py --pair MON
    python tools/halt_recover.py --pair MON --dry     # 只說會做什麼
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)

# **允許清單。加東西進來要寫理由，而且要先有一次真實事故。**
RECOVERABLE = re.compile(r"net imbalance .* exceeds max_net_base")

# **第二種可恢復狀態（2026-09-15 稍晚加）:撤單確認不了的死鎖。**
#
# 它不是 HALT —— `status.json` 是 ok=True,引擎只是永遠重試。所以上面那條
# 路徑完全看不到它,而它是實測**更貴**的那一個:
#
#     09-14 22:58 ~ 09-15 00:13   74 分鐘   一張單都沒掛
#     09-15 08:38 ~ 09-15 09:59   81 分鐘   一張單都沒掛
#     -> 13 小時內 155 分鐘 = **20% 的時間**，而 HALT 那個只發生過一次
#
# 機制（兩次都一樣):`LighterVenue.poll_order` 只讀帳戶 WS 串流的快取,
# 而設定裡明寫 "There is deliberately no REST fallback here"。串流在送出
# 撤單的那一瞬間重連 -> 快取重置 -> 單已經從交易所消失,不會再有它的更新
# 進快取 -> **終態永遠確認不了**。引擎照設計保持悲觀,於是停在那裡。
#
# **為什麼重啟是安全的,而且比等待更安全:** 啟動時會做 REST 掃單
# (`_cancel_stale_orders`,失敗即致命),那是比卡住的引擎手上更強的真相
# 來源。兩次實測掃單都回報**零張掛單** —— 證明那張單早就撤掉了。
#
# 判準用**重試次數**不用時間:log 沒有日期,而看時分秒的窗會撈到別天
# (2026-09-15 00:10 就是這樣讓一盞燈啞掉的)。重試約 10 次/分鐘,
# 所以 100 次 ~ 10 分鐘,遠過正常的 cancel_timeout_sec 3 秒。
UNRESOLVED = re.compile(
    r"MAKER ORDER STILL UNRESOLVED \((\d+) cancel attempts\)")
UNRESOLVED_MIN_ATTEMPTS = 100
UNRESOLVED_TAIL_LINES = 40   # 必須出現在 log 尾端 = 現在還在卡

BUDGET_N = 2                  # 這麼多次
BUDGET_WINDOW_SEC = 6 * 3600  # 在這麼久之內
STATUS_MAX_AGE_SEC = 300      # status.json 比這舊 = 行程死了,那是看門狗的事


def _cfg_tolerance(pair: str) -> float:
    """net_tolerance_base 從設定讀，不寫死 —— 寫死就是第二份實作。"""
    import yaml
    for nm in ("config_%s.yaml" % pair, "config_HMM_%s.yaml" % pair):
        p = os.path.join(ENGINE, nm)
        if os.path.exists(p):
            y = yaml.safe_load(io.open(p, encoding="utf-8")) or {}
            v = (y.get("execution") or {}).get("net_tolerance_base")
            if v is not None:
                return float(v)
    raise RuntimeError("讀不到 %s 的 net_tolerance_base —— 不猜，不動它" % pair)


def _stuck_attempts(pair: str):
    """現在卡在撤單死鎖嗎；是的話回重試次數，不是就回 None。

    **判準用重試次數不用時間。** 引擎的 log 每行只有 `HH:MM:SS`，沒有日期，
    所以任何「最近 N 分鐘」的窗都會撈到前幾天的同一個時刻 —— 2026-09-15
    00:10 就是這樣讓一盞燈啞掉的。重試次數是單調的，不會有這個問題。

    還要求它出現在 log **尾端**：卡住時引擎每分鐘印一次，所以尾端有它
    = 現在還在卡；只在更前面 = 已經好了，那是歷史。
    """
    p = os.path.join(ENGINE, "logs", pair, "runner.log")
    if not os.path.exists(p):
        return None
    try:
        tail = io.open(p, encoding="utf-8", errors="replace"
                       ).read()[-120000:].split("\n")[-UNRESOLVED_TAIL_LINES:]
    except Exception:
        return None
    best = None
    for ln in tail:
        m = UNRESOLVED.search(ln)
        if m:
            best = int(m.group(1))
    return best


def _state_path(pair: str) -> str:
    return os.path.join(ENGINE, "logs", pair, "halt_recover.json")


def _load_state(pair: str) -> dict:
    p = _state_path(pair)
    if not os.path.exists(p):
        return {"restarts": []}
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return {"restarts": []}


def _save(pair: str, st: dict) -> None:
    io.open(_state_path(pair), "w", encoding="utf-8").write(
        json.dumps(st, ensure_ascii=False, indent=1))


def _pids(pair: str) -> list:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object { $_.CommandLine -like '*--symbol %s *' }).ProcessId"
         % pair],
        capture_output=True, text=True, timeout=60)
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    pair = a.pair
    now = time.time()

    sj = os.path.join(ENGINE, "logs", pair, "status.json")
    if not os.path.exists(sj):
        print("沒有 status.json —— 沒跑過")
        return 0
    age = now - os.path.getmtime(sj)
    if age > STATUS_MAX_AGE_SEC:
        print("status.json %.0f 分鐘沒動 —— 行程可能死了，那是看門狗的事"
              % (age / 60))
        return 0
    st = json.load(io.open(sj, encoding="utf-8"))
    reason = str(st.get("reason", ""))

    # ---- 關卡 1：這是哪一種可恢復狀態 ----------------------------------
    if not st.get("ok", True):
        if "HALT" not in reason.upper():
            print("ok=False 但不是 HALT：%s" % reason[:120])
            return 0
        if not RECOVERABLE.search(reason):
            print("**這一類 HALT 不自動恢復**（要人看一眼）：%s" % reason[:140])
            return 0
        kind = "HALT/裸曝險"
    else:
        # **ok=True 也可能卡死** —— 撤單死鎖不會讓 ok 變 False，引擎只是
        # 永遠重試。上面那條路徑結構上看不到它，而它實測更貴（20% vs 一次）。
        n_att = _stuck_attempts(pair)
        if n_att is None:
            return 0                               # 一切正常，安靜
        if n_att < UNRESOLVED_MIN_ATTEMPTS:
            print("撤單未確認 %d 次 —— 還在正常重試範圍（門檻 %d），不動它"
                  % (n_att, UNRESOLVED_MIN_ATTEMPTS))
            return 0
        kind = "撤單死鎖"
        reason = "MAKER ORDER STILL UNRESOLVED (%d cancel attempts)" % n_att

    # ---- 關卡 2：當初讓它卡住的那件事，現在還成立嗎 --------------------
    # **兩種狀態共用這一關**：裸曝險還在就不要碰，因為重啟等於讓一個
    # 部位對不上的引擎重新開始交易。
    tol = _cfg_tolerance(pair)
    net = abs(float((st.get("private") or {}).get("net_base") or 0.0))
    if net > tol:
        print("**裸曝險還在**（|net| %.4g > 容忍 %.4g）—— 平倉沒成功，不重啟"
              % (net, tol))
        return 0

    # ---- 關卡 3：預算。持續出問題就不要一直拉它起來 --------------------
    state = _load_state(pair)
    recent = [t for t in state.get("restarts", [])
              if now - t < BUDGET_WINDOW_SEC]
    if len(recent) >= BUDGET_N:
        print("**預算用完**（%d 小時內已自動重啟 %d 次）—— 這不是暫時性問題，"
              "要人看" % (BUDGET_WINDOW_SEC // 3600, len(recent)))
        state["restarts"] = recent
        state["blocked_at"] = now
        state["blocked_reason"] = reason[:200]
        state["blocked_kind"] = kind
        _save(pair, state)
        return 0

    pids = _pids(pair)
    if not pids:
        print("找不到行程 —— 看門狗會處理")
        return 0

    msg = ("自動恢復[%s]：%s｜裸曝險 %.4g <= 容忍 %.4g｜本窗第 %d/%d 次"
           % (kind, reason[:90], net, tol, len(recent) + 1, BUDGET_N))
    if a.dry:
        print("[乾跑] 會殺 pid %s 讓 .bat 迴圈拉回來" % pids)
        print("[乾跑] " + msg)
        return 0

    for pid in pids:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Stop-Process -Id %d -Force" % pid], timeout=60)
    recent.append(now)
    state["restarts"] = recent
    state["last"] = {"ts": now, "kind": kind,
                     "reason": reason[:200], "net": net,
                     "tol": tol, "pids": pids, "msg": msg}
    _save(pair, state)
    # **不在這裡送 Discord** —— ops/hmm_watch.py 讀這份 json 去報
    # （2026-09-15 起兩者同在 arb，告警走 ops/alert.py）。
    print(msg)
    print("已殺 pid %s —— .bat 的 :loop 會在 30 秒內拉回來（strict 重讀部位）"
          % pids)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
