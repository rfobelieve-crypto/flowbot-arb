# -*- coding: utf-8 -*-
"""設定稽核：把**隨價格縮放**的參數代進真實價格，看它還是不是那個意思。

    python arblib/config_audit.py

===========================================================================
為什麼（2026-09-14，從一個真的抓到的 bug 長出來）
===========================================================================
開 MET 的設定時是從 `config_HMM_GMX.yaml` 複製的，而 `max_net_base: 2.0`
被原封帶過去。那個值的**單位是基礎單位**：

    GMX $7.80   ->  2.0 顆 = $15.60 的裸曝險容忍   （設計值）
    MET $0.234  ->  2.0 顆 = **$0.47**             （每一筆成交都踩線）

沒有任何東西會報錯 —— 它只是讓一道風控閘門在一個標的上變成恆真、
在另一個標的上變成恆假。這是這個專案最熟悉的形狀（守衛存在但量不到
它以為在量的東西）的設定版。

所以這支把每一份設定的價格相依參數**代進當前價格**，用美元比，
而不是用那個數字本身比。

===========================================================================
查什麼
===========================================================================
    max_net_base        基礎單位。× 價格 = 裸曝險容忍（美元）
                        判準：**必須 < 1 張單** —— 它要擋的是「對沖沒成功」，
                        而對沖完全失敗時 net 正好是一張單。
                        紅：>= 1 張單（那時它跳不起來）
                        也報 < 0.05 張單（可能被粉塵誤觸）

    min_order_notional  美元，但場館的最小單有兩種寫法
                        （min_quote_amount 美元 / min_base_amount 顆）
                        低於場館下限 = 送出去必被拒

    max_order_notional  美元。它應該來自**對沖腿的深度**，而那是逐標的的。
                        這支只報「它跟一張單的最小值差幾倍」，深度要另外量。

不查的：`max_position_usd` / `max_gross_usd` / `max_account_gross_usd`
本來就是美元，不隨價格縮放。
"""
from __future__ import annotations

import glob
import io
import json
import os
import sys
import time
import urllib.request

import requests
import yaml

ENG = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "engine")
HL = "https://api.hyperliquid.xyz/info"
LIGHTER = "https://mainnet.zklighter.elliot.ai"


def post(body):
    req = urllib.request.Request(
        HL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def lighter_get(path):
    """Lighter 在 CloudFront 後面，被限流時回 text/html —— 退避重試，
    不要把「被擋」讀成「沒有這個市場」（mistake.md 2026-09-13）。"""
    for i in range(5):
        r = requests.get(LIGHTER + path, timeout=30)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        if i < 4:
            time.sleep(15)
    raise RuntimeError("Lighter REST 一直被限流 —— 沒有價格就沒有這份稽核")


def main(argv=None) -> int:
    # 價格：兩邊都要，因為一份設定的兩條腿可能在不同場館
    mids = dict(post({"type": "allMids"}))
    lt = {}
    for m in lighter_get("/api/v1/orderBookDetails")["order_book_details"]:
        try:
            lt[m["symbol"]] = (float(m.get("last_trade_price") or 0),
                               float(m.get("min_quote_amount") or 0),
                               float(m.get("min_base_amount") or 0))
        except (TypeError, ValueError):
            continue
    if not lt:
        raise RuntimeError("Lighter 市場表是空的 —— 空輸出不是合法狀態")

    rows, reds = [], 0
    for p in sorted(glob.glob(os.path.join(ENG, "config_*.yaml"))):
        name = os.path.basename(p)[len("config_"):-len(".yaml")]
        if name.endswith("_shadow"):
            continue
        raw = yaml.safe_load(io.open(p, encoding="utf-8")) or {}
        hedge = raw.get("hedge") or {}
        sym = hedge.get("symbol") or raw.get("symbol") or name
        px = None
        if sym in lt and lt[sym][0] > 0:
            px = lt[sym][0]
        elif sym in mids:
            try:
                px = float(mids[sym])
            except (TypeError, ValueError):
                px = None
        risk = raw.get("risk") or {}
        siz = raw.get("sizing") or {}
        mnb = risk.get("max_net_base")
        mon = siz.get("max_order_notional_usd")
        mino = siz.get("min_order_notional_usd")
        if mnb is None or px is None:
            rows.append((name, sym, px, mnb, None, mon, mino, "價格或值讀不到"))
            continue
        naked = mnb * px
        clips = naked / mon if mon else float("nan")
        # **判準的方向（2026-09-14 改正，第一版是反的）**
        # config.py:479 寫著設計意圖：「tight enough that a single stuck leg
        # trips it, loose enough that normal settle lag does not」。
        # 而 `_maybe_hedge` 是在報價**結算之後**才檢查（engine.py:1478 的
        # 註解：the only moment that matters after a partial fill），
        # 也就是立即對沖已經試過了。所以：
        #     對沖完全失敗 -> net = 一張單 -> **必須跳**
        # => max_net_base 必須 **< 1 張單**。
        # 第一版我寫成「< 0.5 張單 = 恆真」，方向完全相反 —— 而它會把
        # 唯一兩份設對的（NBIS/NVDA 0.22 張單）判成紅的，
        # 把要上線的那份（GMX 1.04 張單）判成綠的。
        why = []
        if clips >= 1.0:
            why.append("**對沖完全失敗不會跳**（容忍 %.2f 張單 >= 1）" % clips)
        elif clips < 0.05:
            why.append("**可能被粉塵誤觸**（容忍 %.3f 張單）" % clips)
        vmin = lt.get(sym, (0, 0, 0))
        if vmin[1] and mino and mino < vmin[1]:
            why.append("min_order $%.0f < 場館下限 $%.0f" % (mino, vmin[1]))
        if why:
            reds += 1
        rows.append((name, sym, px, mnb, naked, mon, mino,
                     "；".join(why) or "ok"))

    print("設定稽核：把隨價格縮放的參數代進當前價格（%d 份）" % len(rows))
    print()
    print("%-10s %-10s %10s %9s %10s %7s %7s  %s"
          % ("設定", "標的", "價格", "max_net", "裸曝險$", "一張$", "最小$", "判定"))
    print("-" * 104)
    for name, sym, px, mnb, naked, mon, mino, why in rows:
        print("%-10s %-10s %10s %9s %10s %7s %7s  %s"
              % (name, sym,
                 ("%.5g" % px) if px else "—",
                 ("%.4g" % mnb) if mnb is not None else "—",
                 ("%.2f" % naked) if naked is not None else "—",
                 ("%.0f" % mon) if mon else "—",
                 ("%.0f" % mino) if mino else "—", why))
    print()
    print("  %d 份有問題" % reds)
    print("  提醒：record-only 的設定現在不痛 —— 這些閘門只在送單時才起作用。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
