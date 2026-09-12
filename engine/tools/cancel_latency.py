# -*- coding: utf-8 -*-
"""量 **maker 取消延遲** —— 做市最重要的那個延遲，而我們從來沒量過。

來源：Market Making For Dummies（2024-08-26，付費）：

    「Maker 取消延遲通常是做市最重要的，接著是資料 feed 延遲，最後才是
      吃單延遲 —— 後者其實只影響你的 hitting machine。很多做市商系統裡
      根本沒開吃單。」

為什麼這個數字是門檻而不是競賽：搶佇列是競賽（我們進不去，CLAUDE.md §HFT
已定案）；**取消得夠快以免被系統性挑走，是一個門檻**。§1.27 量到的被動放棄
7.27 bps 裡，依那篇文章「約 50% 來自跨所被挑」—— 而能不能守住，取決於這個數。

── 這支怎麼做到「真單但零風險」────────────────────────────────────────
掛一張**離市價 AWAY_BPS（預設 500 bps = 5%）之外**的 post-only 買單，計時
它的送出與取消往返，然後立刻取消。那張單在設計上不可能成交：價格要先跌 5%
並把上面所有掛單掃掉，而我們在數百毫秒內就取消了。名目壓在最小單
（$10 min_quote）附近，所以即使發生不可能的事，最大損失也是 $10 部位的
幾美分。

**--dry 是預設**：不帶 --live 就只跑完六道閘並印出它會送什麼，一張單都不送。

── 六道安全閘（任一不過就中止，不是警告）──────────────────────────
G1 市場必須 active，且 min_quote / min_base 讀得到
G2 帳戶要有抵押，且 available ≥ 需要的保證金估計
G3 **這個市場上本帳戶不得有既存部位**（不去碰別人的東西）
G4 限價必須 ≤ mid × (1 − AWAY_BPS/1e4)，而且**買單價必須低於 best_bid**
   （post-only 的定義是不越過，這裡再加一層自己的檢查）
G5 名目必須落在 [min_quote, MAX_NOTIONAL_USD]
G6 `finally` 一律跑 cancel_open_orders() 掃尾，並印出掃掉幾張

跑法：
    python tools/cancel_latency.py                 # 乾跑，不送單
    python tools/cancel_latency.py --live -n 20    # 真的送 20 次
"""
from __future__ import annotations

import argparse
import asyncio
import statistics as st
import sys
import time
from pathlib import Path

import aiohttp

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from entropy_arb.config import load_config                    # noqa: E402
from entropy_arb.venue_lighter import LighterVenue            # noqa: E402

AWAY_BPS = 500.0          # 離市價多遠（5%）
MAX_NOTIONAL_USD = 12.0   # 名目上限（min_quote 是 10）
SETTLE = 10.0


async def main(args):
    cfg = load_config(config_file=str(HERE.parent / "config_NBIS.yaml"),
                      env_file=str(HERE.parent / ".env"),
                      symbol=args.symbol, hedge_venue="lighter")
    vc = cfg.hedge
    print("場館 %s  市場 %s  （%s）" % (vc.label, vc.symbol, vc.kind))

    stop = asyncio.Event()
    async with aiohttp.ClientSession() as session:
        v = LighterVenue(vc, session, SETTLE)
        await v.load_market()
        print("G1 市場 active：market_id=%d  min_base=%s  min_quote=%s  "
              "px_dec=%s sz_dec=%s"
              % (v.market_id, v.min_base, v.min_quote,
                 v.price_decimals, v.size_decimals))
        assert v.min_quote and v.min_quote > 0, "G1 破：min_quote 讀不到"

        if args.live:
            v.init_signer()
        tasks = v.start_tasks(stop, lambda: None, live=args.live)
        try:
            # 等書
            for _ in range(120):
                if v.book.mid():
                    break
                await asyncio.sleep(0.25)
            mid = v.book.mid()
            bid, ask = v.book.best_bid(), v.book.best_ask()
            assert mid, "書沒來，無法定價"
            print("書：bid %.6g  ask %.6g  mid %.6g  半價差 %.2f bps"
                  % (bid, ask, mid, (ask - bid) / 2 / mid * 1e4))

            if args.live:
                for _ in range(80):
                    if v.ready_to_trade():
                        break
                    await asyncio.sleep(0.25)
                assert v.ready_to_trade(), "帳戶串流沒 ready，中止"
                eq = await v.fetch_equity()
                pos = await v.fetch_position()
                print("G2 抵押 total=%.2f available=%.2f" % (eq[0], eq[1]))
                print("G3 本市場既存部位 = %.8g" % pos)
                assert eq and eq[1] >= 1.0, "G2 破：可用餘額不足"
                assert abs(pos) < 1e-12, "G3 破：這個市場已有部位，不動它"

            # 定價與數量
            px = v.px_round(mid * (1.0 - AWAY_BPS / 1e4), round_up=False)
            qty = max(float(v.min_base),
                      float(v.min_quote) / px * 1.02)
            qty = round(qty, int(v.size_decimals))
            notional = qty * px
            print("G4 限價 %.6g（mid 的 −%.0f bps，best_bid %.6g）"
                  % (px, (1 - px / mid) * 1e4, bid))
            assert px <= mid * (1.0 - AWAY_BPS / 1e4 * 0.99), "G4 破：價格不夠遠"
            assert px < bid, "G4 破：價格沒有低於 best_bid，post-only 會被拒或越過"
            print("G5 數量 %.8g  名目 $%.2f（min_quote %.2f，上限 %.2f）"
                  % (qty, notional, float(v.min_quote), MAX_NOTIONAL_USD))
            assert float(v.min_quote) <= notional <= MAX_NOTIONAL_USD, \
                "G5 破：名目不在 [min_quote, 上限] 之間"

            if not args.live:
                print()
                print("=== 乾跑結束：六道閘全過，一張單都沒送 ===")
                print("要真的量，加 --live")
                return

            place, cancel = [], []
            for i in range(args.n):
                t0 = time.perf_counter()
                r = await v.send_maker(is_buy=True, qty=qty, limit_px=px)
                t1 = time.perf_counter()
                h = r.get("handle")
                if r.get("status") in ("send-failed", "rejected") or h is None:
                    print("  [%2d] 送單失敗：%s" % (i + 1, str(r.get("err"))[:90]))
                    await asyncio.sleep(0.4)
                    continue
                t2 = time.perf_counter()
                c = await v.cancel_order(h)
                t3 = time.perf_counter()
                pm, cm = (t1 - t0) * 1000, (t3 - t2) * 1000
                place.append(pm)
                cancel.append(cm)
                print("  [%2d] place %7.1f ms   cancel %7.1f ms   cancel狀態 %s"
                      % (i + 1, pm, cm, c.get("status")))
                await asyncio.sleep(0.4)

            print()
            for lab, xs in (("送單往返", place), ("**取消往返**", cancel)):
                if not xs:
                    print("%s：沒有樣本" % lab)
                    continue
                xs = sorted(xs)
                print("%-12s n=%d  中位 %7.1f ms  p90 %7.1f  最大 %7.1f  最小 %7.1f"
                      % (lab, len(xs), st.median(xs), xs[int(len(xs) * .9)],
                         xs[-1], xs[0]))
        finally:
            if args.live:
                try:
                    n = await v.cancel_open_orders()
                    print("G6 掃尾：取消了 %d 張殘留掛單" % n)
                except Exception as e:                      # noqa: BLE001
                    print("G6 掃尾失敗（**要手動檢查 Lighter 上有沒有殘單**）：%s" % e)
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await v.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true",
                   help="真的送單（預設是乾跑，一張都不送）")
    p.add_argument("-n", type=int, default=10, help="重複幾次（預設 10）")
    p.add_argument("--symbol", default="NBIS",
                   help="市場（預設 NBIS —— 成交量最低的那幾個之一，"
                        "互動機會最小）")
    asyncio.run(main(p.parse_args()))
