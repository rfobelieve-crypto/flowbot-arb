# -*- coding: utf-8 -*-
"""兩個場館的抵押品夠不夠跑 —— 唯讀，不碰金鑰、不轉帳、不下單。

===========================================================================
為什麼要一支專門的
===========================================================================
`check_env.py` 回答「金鑰對不對」，這支回答**「錢夠不夠、能開多大」**。
兩件事會被混成一句「準備好了沒」,而它們的失敗長得完全不同:
金鑰錯是**開機就炸**,抵押品不足是**引擎安靜地不開倉**
（B6 的 `min_account_free_usd` 一擋,log 只會多一行 `account limit binds`）。

===========================================================================
它算的那條式子
===========================================================================
跨場館對沖的部位,**在每一個場館的保證金引擎眼裡都是單邊的** ——
Lighter 只看到你多 $N,它不管你在 HL 空著。所以兩邊都要各自撐得住。

一條腿:抵押品 C、名目 N、該場館維持保證金率 mmf，
逆向移動 f 時 equity = C − N·f，維持要求 ≈ N·mmf，所以

    爆倉發生在   f = C/N − mmf
    反過來       N = C / (f + mmf)      <- 這支印的就是這個

`--buffer` 就是 f，預設 0.40（想撐住 40% 的逆向移動）。
維持保證金率**從交易所讀**,不寫死:
  Lighter  /api/v1/orderBookDetails 的 maintenance_margin_fraction（1e-4）
  HL       meta 的 maxLeverage,維持率 = 初始率/2 = 1/(2·maxLev)

用法
    python tools/funding_status.py                 # 預設 GMX、每所目標 $50
    python tools/funding_status.py --symbol PROVE --target 50 --buffer 0.3
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.request

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
LIGHTER = "https://mainnet.zklighter.elliot.ai"
HL = "https://api.hyperliquid.xyz/info"


def env(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    for ln in io.open(path, encoding="utf-8"):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def hl_info(body: dict):
    req = urllib.request.Request(
        HL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))


def lighter_get(path: str, params: dict | None = None):
    """Lighter 坐在 CloudFront + WAF 後面：一陣猛打會回 429，接著是帶
    `x-amzn-waf-action: captcha` 的 405（那是挑戰不是壞路由）。唯一正確的
    反應是慢下來，所以這裡退避重試而不是硬打。"""
    for i in range(4):
        r = requests.get(LIGHTER + path, params=params, timeout=30)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        if i < 3:
            time.sleep(15)
    raise RuntimeError("Lighter REST 被限流（回 text/html），稍後再跑")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GMX")
    ap.add_argument("--target", type=float, default=50.0,
                    help="每所目標抵押品（X4 §0 凍結值 $50）")
    ap.add_argument("--buffer", type=float, default=0.40,
                    help="要撐住的逆向移動幅度 f，預設 0.40")
    args = ap.parse_args(argv)
    e = env(os.path.join(ENGINE, ".env"))
    sym = args.symbol
    rows = []
    unread: list = []      # 誰讀不到 —— 絕不補 0

    # ---------------------------------------------------------------- HL
    addr = e.get("HL_ACCOUNT_ADDRESS")
    if addr:
        perp = hl_info({"type": "clearinghouseState", "user": addr})
        spot = hl_info({"type": "spotClearinghouseState", "user": addr})
        eq = float((perp.get("marginSummary") or {}).get("accountValue") or 0)
        usdc = 0.0
        for b in spot.get("balances") or []:
            if b.get("coin") == "USDC":
                usdc = float(b.get("total") or 0)
        meta = hl_info({"type": "meta"})
        a = next((x for x in meta["universe"] if x["name"] == sym), None)
        lev = float(a["maxLeverage"]) if a else 0.0
        # HL 的維持保證金率是初始率的一半（初始率 = 1/maxLeverage）
        mmf = (1.0 / lev) / 2.0 if lev else float("nan")
        rows.append(dict(venue="HL core", listed=bool(a), equity=eq,
                         idle=usdc, idle_where="現貨（要內部劃轉到永續）",
                         mmf=mmf, lev=lev))
    # ----------------------------------------------------------- Lighter
    # 讀不到就**說讀不到**,不要少印一列 —— 一個安靜消失的場館會被讀成
    # 「那一邊沒問題」,而這支的用途正好是回答「夠不夠」。
    idx = e.get("LIGHTER_ACCOUNT_INDEX")
    if idx:
        try:
            # `or [{}]` 曾經在這裡,而它把「讀不到」變成一個**看起來合法的
            # $0.00**：2026-09-13 這支對一個有 $88.78 的帳戶報了 $0.00,
            # 因為 REST 回了 JSON 但不含 accounts（限流的錯誤體也是 JSON）。
            # 一個回答「錢夠不夠」的工具報零,是它能犯的最糟的錯。
            # 空輸出在這裡永遠不是合法狀態 —— 帳號索引是我們自己填的。
            # （mistake.md 2026-09-11：空輸出必須 raise,不可以回空容器。）
            j = lighter_get("/api/v1/account", {"by": "index", "value": idx})
            accts = j.get("accounts") or []
            if not accts:
                raise RuntimeError(
                    "帳戶 %s 查不到（回了 JSON 但沒有 accounts：%.200s）"
                    % (idx, json.dumps(j, ensure_ascii=False)))
            acct = accts[0]
            obd = lighter_get("/api/v1/orderBookDetails")
            ob = {o["symbol"]: o for o in
                  (obd.get("order_book_details") or [])}
            if not ob:
                raise RuntimeError("orderBookDetails 回了 0 個市場")
            o = ob.get(sym)
            mmf = (float(o["maintenance_margin_fraction"]) / 1e4) if o \
                else float("nan")
            lev = (1e4 / float(o["default_initial_margin_fraction"])) \
                if o else 0.0
            rows.append(dict(venue="Lighter mainnet", listed=bool(o),
                             equity=float(acct.get("total_asset_value") or 0),
                             idle=0.0, idle_where="",
                             mmf=mmf, lev=lev))
        except Exception as exc:                             # noqa: BLE001
            unread.append("Lighter mainnet")
            print("  **Lighter 讀不到：%s**" % exc)
            print("  （WAF 挑戰會持續幾分鐘。下面**不會**幫它補一個 0 —— "
                  "讀不到與帳上是零是兩件事，而這支工具的用途正好是分辨它們。）\n")

    print("標的 **%s**｜每所目標抵押品 $%.0f｜要撐住的逆向移動 %.0f%%\n"
          % (sym, args.target, 100 * args.buffer))
    print("  %-16s %8s %8s %9s %8s %12s %12s"
          % ("場館", "已上市", "永續權益", "閒置", "維持率", "現在能開",
             "補滿能開"))
    ok = True
    now_caps, full_caps = [], []
    for r in rows:
        f = args.buffer + r["mmf"]
        now = r["equity"] / f if f > 0 else 0.0
        full = args.target / f if f > 0 else 0.0
        now_caps.append(now)
        full_caps.append(full)
        if r["equity"] + 1e-9 < args.target:
            ok = False
        print("  %-16s %8s %8s %9s %8s %12s %12s"
              % (r["venue"], "是" if r["listed"] else "**否**",
                 "$%.2f" % r["equity"],
                 ("$%.2f" % r["idle"]) if r["idle"] else "—",
                 "%.1f%%" % (100 * r["mmf"]),
                 "$%.0f" % now, "$%.0f" % full))
        if r["idle"]:
            print("       ^ 這 $%.2f 在%s —— **不是入金問題，是內部劃轉**"
                  % (r["idle"], r["idle_where"]))

    cap_now = min(now_caps) if now_caps else 0.0
    cap_full = min(full_caps) if full_caps else 0.0
    if unread:
        # 讀不到的場館不進最小值，所以下面那個上限是
        # **上界不是結論** —— 說清楚，不要讓它看起來像完整的。
        print("\n  **注意：%s 這次沒讀到，下面的上限只算了讀得到的"
              "那些，是上界不是結論。**" % "、".join(unread))
    print("\n  **每腿名目上限 = 兩邊的較小值**（對沖部位在各場館眼裡都是單邊的）")
    print("     現在：**$%.0f/腿**｜補滿到每所 $%.0f 後：**$%.0f/腿**"
          % (cap_now, args.target, cap_full))
    # 最小可交易單位:兩個場館的最小單都是 $10
    print("     而最小下單額是 $10，所以現在約 **%.1f 個最小 clip** 的庫存空間"
          % (cap_now / 10.0))
    print("\n  %s" % ("**兩所都已達標**" if ok else
                      "**還沒達標** —— 缺口："
                      + "、".join("%s +$%.2f" % (r["venue"],
                                                args.target - r["equity"])
                                  for r in rows
                                  if r["equity"] + 1e-9 < args.target)))
    print("\n  轉帳與劃轉由操作者執行；本支唯讀，不碰金鑰。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
