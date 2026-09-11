#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§1.25 宇宙級分鐘錄製器：150 個配對、一個行程、三條 WS（2026-09-11）

===========================================================================
為什麼不是改既有的引擎
===========================================================================
既有的錄製方式是**一個配對一個行程、一份 yaml、一支 .bat**（現在九份）。
加寬到 150 個配對在那個形狀下等於 150 個行程、150 份設定——不可行，
而且 `main.py` 裡**有實盤下單路徑**，只為了錄資料去跑 150 份它是錯的。

所以這一支是**純錄製**：它不 import `Engine`、不碰憑證、不可能下單。

===========================================================================
新的程式碼只有一層：**一條連線帶多個簿口**
===========================================================================
其餘全部重用，**一行都不重寫**：

    entropy_arb.book.OrderBook          apply_hl() / apply_lighter() 的唯一實作
    entropy_arb.recorder.MinuteRecorder HEADER 與分鐘聚合的唯一實作
    arblib.universe                     凍結的宇宙規則與原生代號
    engine/tools/scanner.py             標的跨場館對齊（universe 再 import 它）

production 的 `HLBookFeed` / `LighterBookFeed` 是**一個幣一條連線**。
而 2026-09-11 實測：

    HL          一條 WS 訂 234 個 l2Book（整個主場）-> 234 個全部回資料，100%
    lighter-rh  一條 WS 訂 57 個（整個場館）        -> 57 個全部回資料，100%

所以多工是對的，而且**那兩個數字是實測的，不是假設**。本支要的是
HL 95 + lighter 126 + lighter-rh 43 = **264 個訂閱、三條連線**。

===========================================================================
簿口是**共用**的，這是省下來的地方
===========================================================================
簿口以 `(場館, ticker)` 為單位（264 個），而配對只是指向其中兩個：

    BTC@HL-lighter 與 BTC@HL-lighter-rh **共用同一個 HL:BTC 簿口**

所以 150 個配對只需要 264 個簿口、不是 300 個，而 `MinuteRecorder` 的建構子
剛好就收兩個 book 物件——它本來就是這樣設計的。

===========================================================================
**已知的缺口，要先寫出來**
===========================================================================
1. **資金費欄位會是空的。** `MinuteRecorder(funding=None)` -> 三個
   `fund_*` 欄位留白。既有九個配對有資金費是因為它們跑完整引擎（帶
   FundingPoller）。後果：新配對算不出成本模型的**桶 4（carry）**。
   這是已知缺口不是 bug；要補就另外接一支逐場館的資金費輪詢。
2. **只有頂檔。** 欄位 schema 與既有 `minutes.csv` **逐欄相同**（刻意的：
   下游 `premium_verdict.load()` 因此一行都不用改），而那個 schema 只存
   最佳買賣與頂檔量。分檔深度仍然只有 REST 掃描器有。
3. **`premium_verdict.PAIRS` 仍然寫死九個。** 本支只負責**產生資料**；
   判決要吃這 150 個配對是下一步。先錄是因為**錄不到的不可回填**。

===========================================================================
三道接線（CLAUDE.md：缺一個它就在某個方向上隱形）
===========================================================================
    freshness   本支每輪寫 `logs/universe/_flag.json`（{ok, reason, asof}）
    watchdog    `ops/arb_watchdog.ps1` 要加一條（行程名 record_universe）
    manifest    `logs/universe/` 要進資料清冊

**`ok` 的語意是「連得上且設定對」，不是「有資料」**（mistake.md 2026-09-03）
——所以啟動那一瞬間就寫一次 ok=True，否則看門狗會殺掉剛起來的行程
（mistake.md 2026-09-11）。

    python engine/tools/record_universe.py                 # 全宇宙
    python engine/tools/record_universe.py --limit 5       # 先小跑驗證
    python engine/tools/record_universe.py --seconds 120   # 限時（驗收用）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                       # …/arb
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))

from entropy_arb.book import OrderBook                       # noqa: E402
from entropy_arb.config import LIGHTER_PROFILES              # noqa: E402
from entropy_arb.feeds import _chan_id                       # noqa: E402
from entropy_arb.recorder import MinuteRecorder              # noqa: E402

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:                                           # pragma: no cover
    from websockets import connect as ws_connect              # type: ignore

log = logging.getLogger("record_universe")
HL_WS = "wss://api.hyperliquid.xyz/ws"
OUTDIR = ROOT / "engine" / "logs" / "universe"
FLAG = OUTDIR / "_flag.json"
STALENESS_SEC = 10.0
WS_KWARGS = dict(ping_interval=20, ping_timeout=20, close_timeout=5,
                 max_queue=None)

_stat = dict(frames=0, reconnects=0, books_ready=0, rows=0, started=time.time())

_LOCK_FH = None          # 故意是模組層的全域：句柄被回收就等於解鎖


def acquire_single_instance() -> bool:
    """獨佔鎖；已經有一個實例在跑就回 False。

    **為什麼一定要有**：2026-09-11 我在驗收 `hl_tape` 的修法時自己起了第二個
    實例，兩個寫入者對同一批 parquet 做 read-modify-write，**損失事後算不出來**
    （mistake.md 同日）。這裡更糟：150 個 CSV 會被兩個行程交錯 append，
    而錯開的列**不會報錯**。用檔案鎖不用 pid 檔——行程被殺時 OS 自動釋放。
    """
    global _LOCK_FH
    if _LOCK_FH is not None:
        return True
    lk = ROOT / "results" / ".record_universe.lock"
    lk.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lk, "a+b")
    try:
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return False
    except ImportError:                      # 非 Windows
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
    _LOCK_FH = fh
    return True


def write_flag(ok: bool, reason: str) -> None:
    """freshness 讀的旗標。**啟動那一瞬間就要寫一次 ok=True**——
    看門狗每 5 分鐘回來一次，它讀到舊旗標會殺掉剛起來的行程
    （mistake.md 2026-09-11，成交帶踩過）。"""
    try:
        OUTDIR.mkdir(parents=True, exist_ok=True)
        FLAG.write_text(json.dumps(dict(
            ok=bool(ok), reason=reason,
            asof=time.strftime("%Y-%m-%d %H:%M:%S"),
            frames=_stat["frames"], reconnects=_stat["reconnects"],
            books_ready=_stat["books_ready"], rows=_stat["rows"],
            uptime_s=round(time.time() - _stat["started"], 1)),
            ensure_ascii=False), encoding="utf-8")
    except Exception:                                         # noqa: BLE001
        log.exception("flag write failed")


class MuxHLFeed:
    """一條 HL WS，N 個 coin，分派到各自的簿口。"""

    def __init__(self, subs: dict, books: dict):
        # subs: ticker -> coin（原生名）
        self.subs = subs
        self.by_coin = {c: t for t, c in subs.items()}
        self.books = books

    async def _pinger(self, ws):
        try:
            while True:
                await asyncio.sleep(5)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:                                     # noqa: BLE001
            try:
                await ws.close()
            except Exception:                                 # noqa: BLE001
                pass

    async def run(self, stop: asyncio.Event):
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            try:
                async with ws_connect(HL_WS, **WS_KWARGS) as ws:
                    log.info("HL ws connected — 訂閱 %d 個 coin", len(self.subs))
                    for coin in self.subs.values():
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": coin,
                                             "fast": True}}))
                    ptask = asyncio.create_task(self._pinger(ws))
                    async for raw in ws:
                        backoff = 1.0
                        _stat["frames"] += 1
                        d = json.loads(raw)
                        if d.get("channel") != "l2Book":
                            continue
                        data = d.get("data") or {}
                        t = self.by_coin.get(data.get("coin"))
                        if t is None:
                            continue
                        b = self.books.get(("HL", t))
                        if b is not None:
                            b.apply_hl(data["levels"])
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:                            # noqa: BLE001
                _stat["reconnects"] += 1
                log.warning("HL ws error: %s — %.0fs 後重連", e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class MuxLighterFeed:
    """一條 lighter/lighter-rh WS，N 個 market，分派到各自的簿口。

    **必須等 `connected` 幀才訂閱、而且要回 pong** —— 這兩件事 production
    的 `LighterBookFeed.run()` 有做，而我第一版的探測腳本沒做，
    結果每個 size 都回 0%（那是儀器壞了不是場館拒絕）。
    """

    def __init__(self, venue: str, subs: dict, books: dict):
        self.venue = venue
        self.ws_url = LIGHTER_PROFILES[venue].ws_url
        self.subs = subs                          # ticker -> market_id
        self.by_mid = {int(m): t for t, m in subs.items()}
        self.books = books
        self._nonce = {}                          # market_id -> 上一個 nonce
        self._synced = set()

    async def _subscribe_all(self, ws):
        for mid in self.subs.values():
            await ws.send(json.dumps({"type": "subscribe",
                                      "channel": "order_book/%d" % int(mid)}))

    async def run(self, stop: asyncio.Event):
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, **WS_KWARGS) as ws:
                    log.info("%s ws connected — 訂閱 %d 個 market",
                             self.venue, len(self.subs))
                    self._nonce.clear()
                    self._synced.clear()
                    subscribed = False
                    async for raw in ws:
                        backoff = 1.0
                        _stat["frames"] += 1
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "connected":
                            if not subscribed:
                                await self._subscribe_all(ws)
                                subscribed = True
                            continue
                        if t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue
                        if t not in ("subscribed/order_book", "update/order_book"):
                            continue
                        mid = _chan_id(msg.get("channel", ""))
                        tick = self.by_mid.get(mid)
                        if tick is None:
                            continue
                        b = self.books.get((self.venue, tick))
                        if b is None:
                            continue
                        ob = msg.get("order_book") or {}
                        snap = (t == "subscribed/order_book")
                        if snap:
                            self._nonce[mid] = ob.get("nonce")
                            self._synced.add(mid)
                            b.apply_lighter(ob, snapshot=True)
                        else:
                            # diff：nonce 跳號 = 簿口已經是虛構的,重訂而不是
                            # 拿鬼價報價（production 的同一條紀律）
                            if mid not in self._synced:
                                continue
                            prev = self._nonce.get(mid)
                            begin, end = ob.get("begin_nonce"), ob.get("nonce")
                            if prev is not None and begin is not None and begin > prev + 1:
                                self._synced.discard(mid)
                                self._nonce[mid] = None
                                b.clear()
                                await ws.send(json.dumps({
                                    "type": "unsubscribe",
                                    "channel": "order_book/%d" % mid}))
                                await ws.send(json.dumps({
                                    "type": "subscribe",
                                    "channel": "order_book/%d" % mid}))
                                continue
                            if end is not None:
                                self._nonce[mid] = end
                            b.apply_lighter(ob, snapshot=False)
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:                            # noqa: BLE001
                _stat["reconnects"] += 1
                log.warning("%s ws error: %s — %.0fs 後重連",
                            self.venue, e, backoff)
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def heartbeat(stop: asyncio.Event, books: dict, recs: list):
    """每 30 秒寫一次旗標＋印一行。`ok` = 連得上且設定對。"""
    while not stop.is_set():
        try:
            ready = sum(1 for b in books.values() if b.ready)
            _stat["books_ready"] = ready
            _stat["rows"] = sum(r.rows_written for r in recs)
            ok = (_stat["frames"] > 0)
            write_flag(ok, "ready=%d/%d rows=%d"
                       % (ready, len(books), _stat["rows"]))
            log.info("frames=%d ready=%d/%d rows=%d reconnects=%d",
                     _stat["frames"], ready, len(books), _stat["rows"],
                     _stat["reconnects"])
        except Exception:                                     # noqa: BLE001
            log.exception("heartbeat failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


async def amain(a) -> int:
    if not acquire_single_instance():
        print("**已經有一個 record_universe 在跑 —— 拒絕啟動。**")
        print("兩個實例會交錯 append 同一批 CSV，而錯開的列不會報錯。")
        return 2
    u = json.loads((ROOT / "results" / "arb_universe.json")
                   .read_text(encoding="utf-8"))
    if u.get("errors"):
        print("**宇宙檔帶著錯誤（有場館抓不到清單），先重跑 arblib/universe.py**")
        return 2
    pairs = u["pairs"]
    if a.limit:
        keep = {p["ticker"] for p in pairs[:a.limit]}
        pairs = [p for p in pairs if p["ticker"] in keep]
    tickers = {p["ticker"] for p in pairs}

    # 簿口：(場館, ticker) -> OrderBook（**共用**）
    books, subs = {}, {v: {} for v in u["ws_venues"]}
    for v in u["ws_venues"]:
        nat = u["native"].get(v) or {}
        for t in sorted(tickers):
            if t not in nat:
                continue
            books[(v, t)] = OrderBook()
            subs[v][t] = nat[t].get("coin") if v == "HL" else nat[t]["market_id"]

    recs = []
    for p in pairs:
        a_, b_ = books.get((p["leg_a"], p["ticker"])), books.get((p["leg_b"], p["ticker"]))
        if a_ is None or b_ is None:
            continue
        d = OUTDIR / ("%s@%s-%s" % (p["ticker"], p["leg_a"], p["leg_b"]))
        recs.append(MinuteRecorder(str(d / "minutes.csv"), a_, b_,
                                   staleness_sec=STALENESS_SEC, interval_sec=1.0))
    print("配對 %d 個、簿口 %d 個、訂閱 %s"
          % (len(recs), len(books),
             " / ".join("%s %d" % (v, len(s)) for v, s in subs.items())))

    # **啟動那一瞬間就寫旗標**（ok 的語意是「連得上且設定對」）
    write_flag(True, "啟動中（已建 %d 個錄製器，還沒有資料）" % len(recs))

    stop = asyncio.Event()
    tasks = [asyncio.create_task(MuxHLFeed(subs["HL"], books).run(stop))]
    for v in ("lighter", "lighter-rh"):
        if subs[v]:
            tasks.append(asyncio.create_task(MuxLighterFeed(v, subs[v], books).run(stop)))
    tasks += [asyncio.create_task(r.run(stop)) for r in recs]
    tasks.append(asyncio.create_task(heartbeat(stop, books, recs)))

    if a.seconds:
        try:
            await asyncio.wait_for(stop.wait(), timeout=a.seconds)
        except asyncio.TimeoutError:
            pass
        stop.set()
    else:
        await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for r in recs:
        try:
            r.close()
        except Exception:                                     # noqa: BLE001
            pass
    _stat["rows"] = sum(r.rows_written for r in recs)
    write_flag(_stat["frames"] > 0, "已停止（rows=%d）" % _stat["rows"])
    print("收工：frames=%d rows=%d reconnects=%d"
          % (_stat["frames"], _stat["rows"], _stat["reconnects"]))
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="只錄前 N 個 ticker（小跑驗證用）")
    ap.add_argument("--seconds", type=int, default=0,
                    help="跑幾秒就停（驗收用；0 = 常駐）")
    a = ap.parse_args()
    try:
        return asyncio.run(amain(a))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
