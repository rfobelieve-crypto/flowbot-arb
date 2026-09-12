"""Websocket order-book feeds, writing into entropy_arb.book.OrderBook.

Two protocols, one per exchange family:

LighterBookFeed: zkLighter order_book channel (snapshot + diffs, server
    pings, diff-nonce gap detection — a gapped book is dropped and
    resubscribed rather than traded as a fiction).
HLBookFeed: the official Hyperliquid websocket (wss://api.hyperliquid.xyz/ws)
    l2Book channel with fast snapshots and client app-pings. Every price this
    bot trades on comes straight from the exchange that will fill the order.

Both touch the book on any inbound frame (connection-based freshness: a quiet
market is not stale, only a dead feed is) and reconnect with backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Optional

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook

log = logging.getLogger("feeds")

# WebSocket connection settings, revised 2026-09-05 from evidence + the
# settings perp-dex-tools uses against the same venue (helpers/lighter_ws.py,
# whose comment says the newer Lighter server REQUIRES regular client pings).
#
# What the logs said: 161 ws errors across the recording family, of which
#   142  "no close frame received or sent"      <- server/network dropped us
#    10  "keepalive ping timeout"               <- WE closed it, code 1011
# Lighter dropped 118 times to HL's 43.
#
# ping_interval 15 -> 50, ping_timeout 15 -> 20. At 15/15 a SINGLE slow pong
# kills the connection; one such drop on 2026-09-05 took the shadow engine's
# book stale and (under the then-current rule) halted it for two hours.
#
# max_queue 32 (the library default) -> 1024. This is the likely cause of the
# 142: every inbound frame calls notify(), which wakes the strategy loop, and
# the strategy walks both books. While it works the reader is not draining;
# at 32 frames the queue fills, TCP backs up, and the server hangs up -- which
# is exactly "no close frame received". A deeper queue absorbs the burst.
WS_KWARGS = {
    "max_size": 2 ** 23,
    "open_timeout": 10,
    "ping_interval": 50,
    "ping_timeout": 20,
    "max_queue": 1024,
}


def _chan_id(channel: str) -> Optional[int]:
    """'order_book:32' / 'order_book/32' -> 32."""
    for sep in (":", "/"):
        if sep in channel:
            try:
                return int(channel.rsplit(sep, 1)[1])
            except ValueError:
                return None
    return None


class LighterBookFeed:
    """zkLighter order book for one market over one connection."""

    def __init__(self, name: str, ws_url: str, market_id: int, book: OrderBook,
                 notify: Callable[[], None]) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.book = book
        self.notify = notify
        self._nonce: Optional[int] = None
        self._synced = False
        self._taped = False

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"order_book/{self.market_id}"}))
        # 成交掛在**同一條連線**上：不新增連線、不動重連邏輯。
        # 頻道名是實測出來的（2026-09-12，公開端點探測，帶 order_book 對照組）
        # —— 前三次探測分別被 HTTP 429、「沒等 connected 就訂閱」、
        # 以及「把 chain_id 當成 market_id」擋下來，三次都是對照組抓到的。
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"trade/{self.market_id}"}))

    def _handle_trades(self, msg: dict) -> None:
        """`trades` 與 `liquidation_trades` 兩個陣列，欄位同形。

        方向看 `is_maker_ask`：掛單方在賣 -> **吃單方是買**。
        `usd_amount` 是交易所直接給的名目，不用自己乘（少一個單位錯的機會）。
        這裡**不呼叫 notify()** —— 見 book.on_trade 的註解。
        """
        if _chan_id(msg.get("channel", "")) != self.market_id:
            return
        for key, is_liq in (("trades", False), ("liquidation_trades", True)):
            for t in (msg.get(key) or []):
                try:
                    self.book.on_trade(bool(t.get("is_maker_ask")),
                                       float(t["price"]),
                                       float(t["usd_amount"]), is_liq)
                except (KeyError, TypeError, ValueError):
                    continue
        if not self._taped and self.book.tape.n:
            self._taped = True
            log.info("[%s] trade tape live (first frame)", self.name)

    async def _handle_book(self, ws, msg: dict, snapshot: bool) -> None:
        if _chan_id(msg.get("channel", "")) != self.market_id:
            return
        ob = msg["order_book"]
        if snapshot:
            self._nonce = ob.get("nonce")
            self._synced = True
            self.book.apply_lighter(ob, snapshot=True)
            log.info("[%s] snapshot: %d bids / %d asks", self.name,
                     len(self.book.bids), len(self.book.asks))
            self.notify()
            return
        # diff: a skipped nonce means we lost a level update — the book is now
        # a fiction. Drop it and resubscribe rather than quote off a ghost.
        if not self._synced:
            return  # no snapshot yet (fresh connection, or one pending after a gap)
        prev, begin, end = self._nonce, ob.get("begin_nonce"), ob.get("nonce")
        if prev is not None and begin is not None and begin > prev + 1:
            log.warning("[%s] diff gap (had %s, got %s) — resubscribing",
                        self.name, prev, begin)
            self._nonce = None
            self._synced = False
            self.book.clear()
            self.notify()
            await ws.send(json.dumps({"type": "unsubscribe",
                                      "channel": f"order_book/{self.market_id}"}))
            await self._subscribe(ws)
            return
        if end is not None:
            self._nonce = end
        self.book.apply_lighter(ob, snapshot=False)
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, **WS_KWARGS) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self.book.clear()
                    self._nonce = None
                    self._synced = False
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        self.book.touch()
                        if t == "update/order_book":
                            await self._handle_book(ws, msg, snapshot=False)
                        elif t == "subscribed/order_book":
                            await self._handle_book(ws, msg, snapshot=True)
                        elif t in ("update/trade", "subscribed/trade"):
                            self._handle_trades(msg)
                        elif t == "connected":
                            await self._subscribe(ws)
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class HLBookFeed:
    """Official Hyperliquid l2Book consumer for one coin (e.g. 'io:SNDK')."""

    def __init__(self, name: str, ws_url: str, coin: str, book: OrderBook,
                 notify: Callable[[], None], ping_sec: float = 5.0) -> None:
        self.name = name
        self.ws_url = ws_url
        self.coin = coin
        self.book = book
        self.notify = notify
        self.ping_sec = ping_sec
        self._snapped = False
        self._taped = False

    def _on_trades(self, data) -> None:
        """HL 的 `trades`：side 'B' = 吃單方買、'A' = 吃單方賣（與 hl_tape 一致）。

        HL 沒有像 Lighter 那樣分出清算陣列，所以 liq 一律 False ——
        那一欄在 HL 這側會是 0，**那是「這個場館沒給」不是「沒有清算」**。
        不呼叫 notify()，見 book.on_trade。
        """
        for t in (data or []):
            try:
                if t.get("coin") != self.coin:
                    continue
                px = float(t["px"])
                sz = float(t["sz"])
                self.book.on_trade(str(t.get("side")) == "B", px, px * sz, False)
            except (KeyError, TypeError, ValueError):
                continue
        if not self._taped and self.book.tape.n:
            self._taped = True
            log.info("[%s] trade tape live (first frame)", self.name)

    def _on_frame(self, msg: dict) -> None:
        self.book.touch()
        if msg.get("channel") == "trades":
            self._on_trades(msg.get("data"))
            return
        if msg.get("channel") == "l2Book":
            d = msg.get("data") or {}
            if d.get("coin") == self.coin:
                self.book.apply_hl(d["levels"])
                if not self._snapped:
                    self._snapped = True
                    log.info("[%s] snapshot: %d bids / %d asks", self.name,
                             len(self.book.bids), len(self.book.asks))
                self.notify()

    async def _pinger(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_sec)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            try:
                async with ws_connect(self.ws_url, **WS_KWARGS) as ws:
                    log.info("[%s] connected (official ws, %s)", self.name, self.coin)
                    self.book.clear()
                    self._snapped = False
                    self._taped = False
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": self.coin,
                                         "fast": True}}))
                    # 成交掛在同一條連線（見 LighterBookFeed._subscribe 的理由）
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": self.coin}}))
                    ptask = asyncio.create_task(self._pinger(ws))
                    async for raw in ws:
                        backoff = 1.0
                        self._on_frame(json.loads(raw))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
