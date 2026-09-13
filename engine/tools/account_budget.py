# -*- coding: utf-8 -*-
"""N 個行程共用一個帳戶,而每一道風控閘門都是逐行程的 —— 上線前把它加總一次。

===========================================================================
這支在補的洞
===========================================================================
引擎一次只允許**一張**掛單（`engine._scan_maker`：`if self._maker_open:
return None`）,而一個行程只跑一個 ticker。所以五個市場 = **五個行程**,
而它們共用同一個 Lighter 帳號與同一個 HL 帳號。

`cap_usd` 是逐場館、`max_gross_usd` 是逐行程 —— **五個行程各自守住 $1,000,
帳戶層可以是 $5,000**,而引擎裡每一道閘門都是從 `self.venues` 算的,
也就是這個行程自己的兩條腿。那正是 flow_system 付過兩次代價的形狀
（CLAUDE.md 兩次手動爆倉:kill switch 分不出虧損是誰造成的）,
只是這次對手是我們自己的另一個行程。

**執行期那一半已經修好了**（`entropy_arb/account.py` + `risk.
max_account_gross_usd`：直接讀交易所回報的帳戶層曝險,零額外 API 呼叫）。
這支補的是**啟動前**那一半:把「N 個行程各自的上限」加起來變成一個
**看得見、會紅**的數字。兩者的分工:

    執行期   偵測 —— 已經超了就 HALT（另一個行程造成的也算）
    本支     預防 —— 加總還沒開始跑的那些設定,超了就不要啟動

===========================================================================
「誰會跑」的真相源是看門狗,不是 config 的 glob
===========================================================================
`ops/arb_watchdog.ps1` 的 `$Members` 表是這台機器實際會拉起來的清單。
拿 `config_*.yaml` 去 glob 會數到從來沒被排程的設定檔,那是第二份
「什麼在跑」的真相源（mistake.md 2026-08-26：複製一個數字＝第二份實作）。

**但只讀那張表會漏掉一種情況**,而它正好是下一步要做的事:有人寫了
`run_GMX.bat` 卻還沒加進 `$Members`。所以本支同時掃 `engine/*.bat`,
把「會跑 live 但不在註冊表裡」的啟動器單獨報出來。

===========================================================================
判準（全部是內部一致性,不是判斷題）
===========================================================================
  B1  每個 live 設定都要有 `max_account_gross_usd` 與 `min_account_free_usd`
      （引擎的 REQUIRED_RISK 本來就會拒絕啟動,這裡提前講）
  B2  共用同一個帳戶的設定,對 `max_account_gross_usd` 必須**同值**
      （一個資金池兩個天花板,其中一個一定是錯的）
  B3  共用同一個帳戶的 live 行程,**Σ 逐行程 max_gross_usd ≤ 該帳戶的天花板**
  B4  `engine/*.bat` 裡沒有「會跑 live 卻不在 $Members」的啟動器

刻意**不做**的一關:拿帳戶權益去推槓桿夠不夠。那需要一個我們沒有量過的
保證金模型,而把一個設計參數寫成排除閘門是本 session 重複犯過的病
（TODO §1.41）。權益的對照留給 `tools/check_env.py`（它本來就連線）。

用法
    python tools/account_budget.py              # 不連網,看門狗每 5 分鐘跑這個
    python tools/account_budget.py --verbose    # 連 record-only 的也列出來
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
ARB = os.path.dirname(ENGINE)
WATCHDOG = os.path.join(ARB, "ops", "arb_watchdog.ps1")
FLAG = os.path.join(ARB, "results", "account_budget.json")

# $Members = [ordered]@{ 'NAME' = @('signature', 'launcher.bat') }
_MEMBER = re.compile(r"^\s*'([^']+)'\s*=\s*@\(\s*'[^']*'\s*,\s*'([^']+)'\s*\)",
                     re.M)


def registered_launchers() -> dict:
    """name -> launcher .bat, straight out of the watchdog's own table."""
    with open(WATCHDOG, encoding="utf-8") as fh:
        src = fh.read()
    out = dict(_MEMBER.findall(src))
    if not out:
        # 空輸出在這裡永遠不是合法狀態（家族至少十個成員）。
        # mistake.md 2026-09-11：回空容器會讓下游安靜地繼續用舊數字。
        raise RuntimeError(
            "parsed 0 members out of %s — the $Members table's shape changed; "
            "fix the regex rather than letting this return an empty set"
            % WATCHDOG)
    return out


def parse_bat(path: str) -> dict | None:
    """What this launcher actually runs. None when it is not a main.py leg."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return None
    line = next((l for l in src.splitlines() if " main.py" in l), None)
    if line is None:
        return None          # scanner / record_universe: no positions, no caps

    def flag(name, default=None):
        m = re.search(r"--%s\s+(\S+)" % name, line)
        return m.group(1) if m else default

    return dict(bat=os.path.basename(path),
                live="--record-only" not in line,
                symbol=flag("symbol"),
                hedge=flag("hedge"),
                config=flag("config", "config.yaml"))


def read_cfg(name: str) -> dict:
    p = os.path.join(ENGINE, name)
    if not os.path.exists(p):
        return {"__missing__": p}
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def accounts_of(job: dict, raw: dict) -> list:
    """The margin pools this job touches: [(pool_id, that leg's cap_usd), ...].

    A pool is what shares collateral, not what shares a login:
      HL        address + HIP-3 dex   (HIP-3 clearinghouses fund separately)
      Lighter   deployment + index    (mainnet and the RH chain are different
                                       chains and different accounts)
    The address/index live in .env and are NOT read here -- this check never
    touches credentials. One account per (kind, deployment/dex) is the right
    granularity for a launch-time sum, because one machine has one .env.

    **每一條腿帶的是它自己那個場館的 `max_position_usd`,不是
    `max_gross_usd`。** 第一版 B3 加總的是 `max_gross_usd`(兩條腿的和),
    而它要比的天花板是**單一帳戶**的 —— 兩條腿坐在兩個不共用抵押品的
    帳戶上,把兩腿的和拿去比單一帳戶是單位錯(mistake.md 2026-09-03)。
    """
    ent = raw.get("entropy") or {}
    hed = raw.get("hedge") or {}
    ev = str(ent.get("venue") or "hl").lower()
    dex = str(ent.get("dex") or "")
    a = ("lighter:%s" % ev) if ev.startswith("lighter") \
        else "hl:%s" % (dex or "core")
    h = job.get("hedge") or "?"
    b = ("lighter:%s" % h) if h.startswith("lighter") else "hl:xyz"
    return [(a, float(ent.get("max_position_usd") or 0.0)),
            (b, float(hed.get("max_position_usd") or 0.0))]


def main(argv=None) -> int:
    # argv is a parameter so the tests can call main([]) -- parse_args() with
    # no argument reads sys.argv, which under pytest is pytest's own.
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    reg = registered_launchers()
    jobs = []
    for name, bat in reg.items():
        j = parse_bat(os.path.join(ENGINE, bat))
        if j is None:
            continue
        j["member"] = name
        j["raw"] = read_cfg(j["config"])
        jobs.append(j)

    live = [j for j in jobs if j["live"]]
    rec = [j for j in jobs if not j["live"]]
    print("看門狗註冊 %d 個成員,其中 %d 個是 main.py 的腿："
          "**live %d / record-only %d**"
          % (len(reg), len(jobs), len(live), len(rec)))
    if args.verbose and rec:
        print("  record-only（不持倉,不佔帳戶）：%s"
              % ", ".join(j["member"] for j in rec))

    reds: list = []

    # ---- B4 先做：不在註冊表裡的 live 啟動器 ----------------------------
    known = {os.path.basename(b).lower() for b in reg.values()}
    stray = []
    for p in sorted(glob.glob(os.path.join(ENGINE, "*.bat"))):
        if os.path.basename(p).lower() in known:
            continue
        j = parse_bat(p)
        if j and j["live"]:
            stray.append(j["bat"])
    if stray:
        reds.append("B4 有 live 啟動器不在看門狗的 $Members 裡：%s"
                    "（看門狗不會拉它、本支也數不到它的額度）"
                    % ", ".join(stray))
    else:
        print("B4 沒有遊蕩的 live 啟動器 ✓")

    if not live:
        print("\n**沒有 live 行程 —— 沒有東西共用帳戶,B1–B3 不適用。**")
        print("  （今天全家族都是 `--record-only`,錄價不持倉。）")
        ok = not reds
        for r in reds:
            print("  **紅：%s**" % r)
        write_flag(ok, reds, len(live), len(jobs))
        return 0 if ok else 1

    # ---- B1 每個 live 設定都要把兩個帳戶級開關寫出來 --------------------
    pools: dict = {}
    for j in live:
        raw = j["raw"]
        if "__missing__" in raw:
            reds.append("B1 %s 的設定檔不存在：%s"
                        % (j["member"], raw["__missing__"]))
            continue
        risk = raw.get("risk") or {}
        cap_a = float(risk.get("max_account_gross_usd") or 0.0)
        free = float(risk.get("min_account_free_usd") or 0.0)
        cap_p = float(risk.get("max_gross_usd") or 0.0)
        j.update(cap_account=cap_a, min_free=free, cap_process=cap_p)
        for k, v in (("max_account_gross_usd", cap_a),
                     ("min_account_free_usd", free),
                     ("max_gross_usd", cap_p)):
            if v <= 0:
                reds.append("B1 %s（%s）沒有設 risk.%s —— 引擎的 "
                            "REQUIRED_RISK 會拒絕啟動"
                            % (j["member"], j["config"], k))
        for pool, leg_cap in accounts_of(j, raw):
            pools.setdefault(pool, []).append((j, leg_cap))
            if leg_cap <= 0:
                reds.append("B1 %s（%s）在 %s 那條腿沒有 max_position_usd"
                            % (j["member"], j["config"], pool))

    # ---- B2 / B3 逐帳戶 -------------------------------------------------
    # **加總的單位是「那條腿在這個資金池上的 max_position_usd」**,不是
    # max_gross_usd（兩條腿的和）—— 兩條腿坐在兩個不共用抵押品的帳戶上。
    print("\n逐帳戶（只算 live；額度用該腿的 max_position_usd）")
    print("  %-22s %5s %14s %14s %s"
          % ("帳戶（資金池）", "行程", "Σ該腿上限", "帳戶天花板", "判定"))
    for pool, entries in sorted(pools.items()):
        caps = {j.get("cap_account", 0.0) for j, _ in entries}
        total = sum(c for _, c in entries)
        verdict = "ok"
        if len(caps) > 1:
            verdict = "**B2 天花板不一致**"
            reds.append("B2 %s 上的 %d 個行程對 max_account_gross_usd 不同意："
                        "%s —— 一個資金池只能有一個天花板"
                        % (pool, len(entries),
                           ", ".join("%s=$%.0f" % (j["member"],
                                                   j.get("cap_account", 0.0))
                                     for j, _ in entries)))
        cap_a = max(caps) if caps else 0.0
        if cap_a > 0 and total > cap_a:
            verdict = "**B3 加起來超過**"
            reds.append("B3 {}：Σ 該腿 max_position_usd ${:,.2f} 超過帳戶天花板 "
                        "${:,.2f}（{}）—— 每個行程都守得住自己的上限,"
                        "帳戶層仍然會爆"
                        .format(pool, total, cap_a,
                                ", ".join("%s $%.0f" % (j["member"], c)
                                          for j, c in entries)))
        print("  %-22s %5d %14s %14s %s"
              % (pool, len(entries), "${:,.0f}".format(total),
                 "${:,.0f}".format(cap_a) if cap_a else "—", verdict))

    ok = not reds
    print()
    if ok:
        print("**全部通過** —— 逐行程的額度加起來仍在帳戶天花板之內。")
    for r in reds:
        print("**紅：%s**" % r)
    write_flag(ok, reds, len(live), len(jobs))
    return 0 if ok else 1


def write_flag(ok: bool, reds: list, n_live: int, n_jobs: int) -> None:
    """新鮮度看板讀這份（json_flag 觀測）。判準是 ok 與 asof,不是行程在不在。"""
    os.makedirs(os.path.dirname(FLAG), exist_ok=True)
    reason = ("live %d / %d 腿,逐行程額度加總在帳戶天花板內" % (n_live, n_jobs)
              if ok else " ｜ ".join(reds)[:500])
    tmp = FLAG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"ok": bool(ok), "reason": reason,
                   "asof": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
                   "ts": time.time(), "live": n_live, "legs": n_jobs},
                  fh, ensure_ascii=False, indent=2)
    os.replace(tmp, FLAG)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
