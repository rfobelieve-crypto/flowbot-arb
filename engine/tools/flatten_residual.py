# -*- coding: utf-8 -*-
"""把一個**低於場館最小單、引擎關不掉**的殘量平掉。

    python tools/flatten_residual.py --symbol MET --config config_MET.yaml
    python tools/flatten_residual.py --symbol MET --config config_MET.yaml --live

===========================================================================
為什麼需要這一支（2026-09-14，一個真的卡住的部位）
===========================================================================
HMM 在 MET 上跑了 20 分鐘、收到 17 筆成交、每筆 2.3 顆（$0.51），而**兩腿
的最小單都是 $10**。於是：

* 立刻對沖（-> HL）被 `pend * px < taker_v.min_quote` 擋住（$0.51 < $10），
  engine.py:1344。它會等後續成交累積，但報價 20 秒就超時，而成交每 30 秒
  才一筆 —— 一張報價幾乎接不到第二筆。
* net-delta 對沖與 `flat` 都要求 `qty >= v.min_base`（Lighter MET 是 50 顆），
  engine.py:1644 / 2050。**而第一道守衛是靜默的 `continue`** —— 所以
  `flat` 收到了、跑了、什麼都沒做、也沒留下一行說它沒做。

結果是一個 25.9 顆的空單，引擎在結構上關不掉它。

**而引擎從來沒去問過交易所這件事。** `min_base` 是**開新倉**的下限；
多數場館對 `reduce_only` 的平倉單是放行的（平倉不會製造新曝險，擋它只會
把人鎖在部位裡）。這支去問那個問題，而且用引擎自己的下單路徑問，
不寫第二份實作（mistake.md 2026-09-13）。

===========================================================================
安全性：這支只能讓部位變小
===========================================================================
* `reduce_only=True` —— 交易所端保證它不能開倉、不能翻向。
* 數量取自**交易所回報的部位**（`fetch_position`），不是任何記憶體狀態。
* 名目上限 `--max-usd`（預設 $30）：殘量依定義是小的，這道擋的是
  「拿它去平一個正常大小的部位」——那種要用引擎的 `flat`。
* IOC ＋ 價格保護（`hedge_slippage_bps`）。
* **預設乾跑**；要真的送單必須明寫 `--live`。
* 跑之前引擎必須是停的，否則兩個寫入者會搶同一個部位。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from entropy_arb.config import load_config          # noqa: E402
from entropy_arb.venue_lighter import LighterVenue  # noqa: E402

BOOKS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails"


async def mark_price(session: aiohttp.ClientSession, sym: str) -> float:
    async with session.get(BOOKS, timeout=aiohttp.ClientTimeout(total=20)) as r:
        js = await r.json()
    books = js.get("order_book_details") or []
    if not books:
        raise RuntimeError("orderBookDetails 回空 —— 那不是『沒有市場』，"
                           "是讀不到（mistake.md 2026-09-13）")
    hit = [b for b in books if b.get("symbol") == sym]
    if not hit:
        raise RuntimeError("Lighter 上沒有 %s" % sym)
    return float(hit[0]["last_trade_price"])


async def run(a) -> int:
    cfg = load_config(a.config, symbol=a.symbol, hedge_venue=a.hedge)
    session = aiohttp.ClientSession()
    v = LighterVenue(cfg.hedge, session, cfg.settle_timeout_sec)
    try:
        await v.load_market()
        v.init_signer()
        pos = await v.fetch_position()
        px = await mark_price(session, a.symbol)
        print("交易所回報的部位：%+.6g 顆 @ ~%.6g = $%.2f"
              % (pos, px, abs(pos) * px))
        print("場館最小單：%.6g 顆 / $%.2f  <- 那是**開新倉**的下限"
              % (v.min_base, v.min_quote))
        if abs(pos) < 1e-12:
            print("部位已經是 0，沒事可做。")
            return 0
        if abs(pos) * px > a.max_usd:
            print("**拒絕**：$%.2f 超過 --max-usd $%.2f。這支只處理殘量。"
                  % (abs(pos) * px, a.max_usd))
            return 2
        is_sell = pos > 0                      # 多單就賣掉，空單就買回
        slip = cfg.hedge_slippage_bps / 1e4
        ref = px * (1 - slip) if is_sell else px * (1 + slip)
        limit = v.px_round(ref, not is_sell)
        qty = abs(pos)
        below = qty < v.min_base or qty * limit < v.min_quote
        print("要送：%s %.6g 顆 @%.6g（reduce_only=True, IOC）%s"
              % ("SELL" if is_sell else "BUY", qty, limit,
                 "\n      ^ **低於最小單 —— 這正是要問交易所的那個問題**"
                 if below else ""))
        if not a.live:
            print("\n乾跑，沒有送出任何東西。確認無誤後加 --live。")
            return 0
        print("\n先撤掉所有掛單（不然殘量會邊平邊長）...")
        await v.cancel_open_orders()
        info = await v.send_taker(is_buy=not is_sell, qty=qty,
                                  limit_px=limit, reduce_only=True)
        print("回應：%r" % (info,))
        if info.get("err"):
            print("\n**交易所拒絕了** —— reduce-only 一樣受最小單限制。"
                  "那是一個要寫進判決的場館事實：這個場館上低於最小單的殘量"
                  "關不掉，只能把部位做大到最小單以上再平。")
            return 1
        await asyncio.sleep(2.0)
        after = await v.fetch_position()
        print("\n平倉後部位：%+.6g 顆（原本 %+.6g）" % (after, pos))
        print("結論：**Lighter 的 reduce-only 不受 min_base 限制**"
              if abs(after) < abs(pos) * 0.5
              else "部位沒有明顯變小 —— 回應要逐欄讀，不要當成成功")
        return 0
    finally:
        await v.close()
        await session.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--hedge", default="lighter")
    ap.add_argument("--max-usd", type=float, default=30.0)
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args(argv)
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
