# -*- coding: utf-8 -*-
"""讓**掛單對沖**那條路在真實的 HL 上真的跑一次。

2026-09-15。`hedge_maker_timeout_sec` 開了，但它只在 Lighter 成交的那一刻
才會走 —— 而 MON 約每小時 2 筆。於是「程式碼會走那條路」與「它在真實延遲
下走得通」之間，隔著一個我們還沒付的驗證。

**這支不重寫那條路，它跑的就是引擎本尊**：建一個真的 `Engine`
（`__init__` 是純的，不碰 I/O），把 live 的 HL 腿裝進去，然後呼叫
`eng._hedge_try_maker(...)`。所以它驗的是**會送真單的那份程式碼**，
不是一份長得很像的複本（mistake.md 2026-09-14:第二份實作在構造這一層
出錯時不會報錯,它會給你一個看起來合理的數字）。

── 兩輪，順序有理由 ──────────────────────────────────────────────
A **遠價**（離 mid AWAY_BPS，預設 500 = 5%）—— 依建構不可能成交。
  驗:送單被 ALO 接受、輪詢會回話、逾時之後撤單送得出去、**回傳 0**、
     而且**不卡住**（對沖路徑卡住 = 裸著卡住,那是這條路最貴的失效）。
B **觸價** —— 可能成交。驗成交分支與**用掛單費率記帳**。
  沒成交也算有效結果:它仍然走完了整條路,只是沒有走到記帳那幾行。

A 先跑，因為 A 不可能留下部位；A 過不了就不該送 B。

── 七道安全閘（任一不過就中止，不是警告）────────────────────────
G1 市場載入，min_quote / szDecimals 讀得到
G2 帳戶有抵押
G3 **本市場既存部位 = 0**（不去碰別人的東西）
G4 **這個 symbol 不可以是任何一支活著的引擎正在做的** —— 否則它的對帳會
   看到一個自己沒下過的部位變動 -> `unexplained_position_halt` -> HALT。
   這一關是這支專屬的，cancel_latency 沒有（它打的是沒人在跑的市場）。
G5 價格必須是**被動**的:賣 >= best_ask、買 <= best_bid（post-only 的定義,
   自己再檢查一層,不倚賴交易所拒絕）
G6 名目落在 [min_quote, MAX_NOTIONAL_USD]
G7 `finally` 一律:撤掉所有殘留掛單 ＋ **把殘餘部位用吃單平掉** ＋ 印出
   最後的部位。留一個沒人管的部位比什麼都沒測更糟。

**--dry 是預設**:不帶 --live 就只跑完七道閘並印出它會送什麼,一張單都不送。

跑法:
    python tools/hedge_maker_probe.py --symbol SOL
    python tools/hedge_maker_probe.py --symbol SOL --live
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import aiohttp

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
sys.path.insert(0, str(ENGINE))

from entropy_arb.config import load_config                    # noqa: E402
from entropy_arb.engine import Engine                         # noqa: E402
from entropy_arb.maker import MakerOrder                      # noqa: E402
from entropy_arb.venue_hl import HLVenue                      # noqa: E402

AWAY_BPS = 500.0          # A 輪離市價多遠（5%）
MAX_NOTIONAL_USD = 15.0   # 名目上限（HL min_quote 是 10）
SETTLE = 10.0
LIVE_STATUS_SEC = 180.0   # status.json 比這新 = 那一支引擎活著


def live_pairs() -> set:
    """哪些配對現在有引擎活著（判準是**產物**不是行程名）。"""
    out = set()
    root = ENGINE / "logs"
    if not root.is_dir():
        return out
    now = time.time()
    for d in root.iterdir():
        f = d / "status.json"
        if f.is_file() and now - f.stat().st_mtime < LIVE_STATUS_SEC:
            out.add(d.name.upper())
    return out


async def run_one(eng, v, label, is_buy, qty, px, live):
    """跑一次引擎本尊的 _hedge_try_maker，回傳 (成交量, 耗時秒)。"""
    o = MakerOrder(venue_key="probe", is_buy=not is_buy, qty=qty, px=px,
                   sent_ts=time.time())
    pos0, cash0 = v.position, v.cash
    t0 = time.time()
    filled = await eng._hedge_try_maker(v, is_buy, qty, o)
    dt = time.time() - t0
    print("  %s: 成交 %.8g / %.8g   耗時 %.2f 秒   引擎記的 hedge_maker_ms=%s"
          % (label, filled, qty, dt, o.stats.get("hedge_maker_ms")))
    if filled:
        d_pos, d_cash = v.position - pos0, v.cash - cash0
        eff = abs(d_cash) / filled           # 含費之後的每單位有效價
        # **不要在這裡重算它掛在哪個價** —— 那是把 _hedge_try_maker 的定價
        # 邏輯抄第二份,而第二份在構造這層出錯時不會報錯。只報看得到的量,
        # 費率是不是記對由 tests/test_hedge_maker.py 釘住。
        print("     部位 %+.8g   現金 %+.4f   含費有效價 %.8g" %
              (d_pos, d_cash, eff))
    return filled, dt


async def main(args) -> int:
    cfg = load_config(config_file=str(ENGINE / "config_MON.yaml"),
                      env_file=str(ENGINE / ".env"),
                      symbol=args.symbol, hedge_venue="lighter")
    vc = cfg.entropy                      # config_MON.yaml 的 entropy 腿 = HL
    print("場館 %s  市場 %s  （%s）  掛單 %.2f / 吃單 %.2f bps"
          % (vc.label, vc.symbol, vc.kind, vc.maker_fee_bps, vc.fee_bps))
    print("模式 %s\n" % ("**LIVE — 會送真單**" if args.live else "乾跑（不送單）"))

    lp = live_pairs()
    print("G4 現在活著的引擎:%s" % (", ".join(sorted(lp)) or "（無）"))
    assert args.symbol.upper() not in lp, (
        "G4 破:%s 有一支引擎活著。對著它的市場下單會讓它的對帳看到一個"
        "自己沒下過的變動 -> unexplained_position_halt -> HALT。換一個 symbol。"
        % args.symbol)

    stop = asyncio.Event()
    async with aiohttp.ClientSession() as session:
        v = HLVenue(vc, cfg.hl_api_url, cfg.hl_ws_url, session, SETTLE)
        await v.load_market()
        print("G1 市場載入:asset_id=%s  szDecimals=%s  min_quote=%s"
              % (v.asset_id, v.size_decimals, v.min_quote))
        assert v.min_quote and v.min_quote > 0, "G1 破:min_quote 讀不到"

        if args.live:
            v.init_signer()
        tasks = v.start_tasks(stop, lambda: None, live=args.live)
        try:
            for _ in range(120):
                if v.book.mid():
                    break
                await asyncio.sleep(0.25)
            mid, bid, ask = v.book.mid(), v.book.best_bid(), v.book.best_ask()
            assert mid, "書沒來，無法定價"
            print("   書:bid %.8g  ask %.8g  半價差 %.2f bps"
                  % (bid, ask, (ask - bid) / 2 / mid * 1e4))

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
                assert eq and eq[1] >= 2.0, "G2 破:可用餘額不足"
                assert abs(pos) < 1e-12, "G3 破:這個市場已有部位，不動它"

            is_buy = args.side == "buy"
            # A 輪的遠價:買就掛在下面、賣就掛在上面
            away = (mid * (1 - AWAY_BPS / 1e4) if is_buy
                    else mid * (1 + AWAY_BPS / 1e4))
            px_a = v.px_round(away, round_up=not is_buy)
            # B 輪:觸價（這就是 _hedge_try_maker 自己會選的價，它自己算）
            px_b = bid if is_buy else ask

            qty = float(v.min_quote) / mid * 1.05
            qty = round(qty, int(v.size_decimals))
            ntl = qty * mid
            print("G5 A 輪限價 %.8g（離 mid %.0f bps，被動側 %s）"
                  % (px_a, abs(px_a / mid - 1) * 1e4, "bid" if is_buy else "ask"))
            assert (px_a <= bid) if is_buy else (px_a >= ask), \
                "G5 破:A 輪的價格不是被動的"
            print("G6 數量 %.8g  名目 $%.2f（min_quote %.2f，上限 %.2f）"
                  % (qty, ntl, float(v.min_quote), MAX_NOTIONAL_USD))
            assert float(v.min_quote) <= ntl <= MAX_NOTIONAL_USD, \
                "G6 破:名目不在 [min_quote, 上限] 之間"

            if not args.live:
                print("\n=== 乾跑結束:七道閘全過，一張單都沒送 ===")
                print("A 輪會掛 %s %.8g @%.8g；B 輪會掛在觸價 %.8g"
                      % ("BUY" if is_buy else "SELL", qty, px_a, px_b))
                print("要真的跑，加 --live")
                return 0

            print("\n=== A 輪:遠價，依建構不可能成交 ===")
            # 讓 _hedge_try_maker 自己選價會選觸價,所以 A 輪要繞過那一步 ——
            # 暫時把書換成一本「觸價就在遠處」的假書不行（那是第二份實作）。
            # 改法:A 輪直接驗 venue 層的 送單/輪詢/撤單,B 輪才走整條路。
            t0 = time.perf_counter()
            r = await v.send_maker(is_buy=is_buy, qty=qty, limit_px=px_a)
            t1 = time.perf_counter()
            h = r.get("handle")
            print("  送單 %.0f ms  status=%s  err=%s"
                  % ((t1 - t0) * 1e3, r.get("status"), str(r.get("err"))[:70]))
            assert r.get("status") not in ("send-failed", "rejected") \
                and h is not None, "A 輪送單就失敗了，不要送 B 輪"
            st = await v.poll_order(h)
            print("  輪詢 status=%s terminal=%s filled=%s"
                  % (st.get("status"), st.get("terminal"), st.get("filled_base")))
            t2 = time.perf_counter()
            c = await v.cancel_order(h)
            print("  撤單 %.0f ms  status=%s"
                  % ((time.perf_counter() - t2) * 1e3, c.get("status")))
            assert st.get("status") in ("open", "resting"), \
                "A 輪那張單沒有在簿上 —— ALO 被拒的話整條路每次都白跑"

            print("\n=== B 輪:觸價，走引擎本尊的 _hedge_try_maker ===")
            for i in range(args.n):
                filled, dt = await run_one(eng_for(cfg, v), v,
                                           "第 %d 次" % (i + 1),
                                           is_buy, qty, px_b, args.live)
                assert dt < cfg.hedge_maker_timeout_sec + 5.0, \
                    "**卡住了**:耗時 %.1f 秒 > 逾時 %.1f + 5" \
                    % (dt, cfg.hedge_maker_timeout_sec)
                if filled:
                    break
                await asyncio.sleep(0.5)
            return 0
        finally:
            if args.live:
                try:
                    n = await v.cancel_open_orders()
                    print("\nG7 掃尾:撤掉 %d 張殘留掛單" % n)
                except Exception as e:                      # noqa: BLE001
                    print("\nG7 撤單掃尾失敗（**手動檢查 HL 上有沒有殘單**）:%s" % e)
                try:
                    pos = await v.fetch_position()
                    print("G7 鏈上部位 = %.8g" % pos)
                    if abs(pos) > 1e-9:
                        m = v.book.mid() or 0.0
                        # HL 的 reduce_only 對 $10 最小單放不放行是**未知**的
                        # （venue_hl.py:reduce_only_ignores_min = False）,
                        # 所以殘餘低於最小單時先說出來,不要假裝平得掉。
                        if abs(pos) * m < float(v.min_quote):
                            print("G7 **殘餘 $%.2f 低於最小單 $%.2f** —— "
                                  "HL 對這種殘餘放不放行未知,可能平不掉。"
                                  "手動確認。" % (abs(pos) * m, v.min_quote))
                        lim = v.px_round(m * (1.02 if pos < 0 else 0.98),
                                         round_up=pos < 0)
                        res = await v.send_taker(is_buy=pos < 0, qty=abs(pos),
                                                 limit_px=lim,
                                                 reduce_only=True)
                        print("G7 用吃單平掉:%s filled=%s err=%s"
                              % (res.get("status"), res.get("filled_base"),
                                 str(res.get("err"))[:70]))
                        left = await v.fetch_position()
                        print("G7 平完部位 = %.8g %s"
                              % (left, "" if abs(left) < 1e-9
                                 else "**<- 沒歸零,手動處理**"))
                except Exception as e:                      # noqa: BLE001
                    print("G7 平倉失敗（**手動檢查 HL 上的部位**）:%s" % e)
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await v.close()


_ENG = None


def eng_for(cfg, v):
    """一個真的 Engine，裝上 live 的腿。`__init__` 是純的所以這很便宜,
    而重點是 `_hedge_try_maker` 與 `_book_hedge_fill` 都是**它本尊的**。"""
    global _ENG
    if _ENG is None:
        _ENG = Engine(cfg)
        _ENG.entropy = v
        _ENG.venues = {v.key: v}
    return _ENG


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SOL",
                   help="HL 市場（預設 SOL；**不可以是正在跑的引擎那個**）")
    p.add_argument("--side", default="sell", choices=("buy", "sell"))
    p.add_argument("--live", action="store_true",
                   help="真的送單（預設乾跑，一張都不送）")
    p.add_argument("-n", type=int, default=3,
                   help="B 輪最多試幾次（成交就停）")
    a = p.parse_args()
    raise SystemExit(asyncio.run(main(a)))
