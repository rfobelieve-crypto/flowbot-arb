"""zkLighter venue adapter (Lighter mainnet / Lighter Robinhood chain).

Market data and account state come from Lighter's public REST + websocket
APIs via plain aiohttp/websockets, so --record-only data collection works
without the SDK. Trading lazily imports the official `lighter` SDK
(https://github.com/elliottech/lighter-python) for transaction signing only.

Market orders carry mandatory avg-execution-price protection and settle
asynchronously on the authenticated account_orders websocket; send_taker()
hides that behind the same result shape the HL venue returns:
{status, filled_base, avg_px, err, unresolved}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import OrderedDict
from typing import Optional

import aiohttp

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook
from .config import VenueConf
from .feeds import WS_KWARGS, LighterBookFeed

log = logging.getLogger("lighter")

# Lighter's own enum is lowercase (models/order.py, verified 2026-09-04),
# but the comparison is normalised anyway: an unrecognised status here makes
# an order look TERMINAL, which is the optimistic direction and the one that
# loses a hedge. See maker.TERMINAL_STATUSES for the same reasoning.
OPEN_STATUSES = {"in-progress", "pending", "open", "new", "partially_filled"}
AUTH_REFRESH_SEC = 8 * 60
REST_TIMEOUT = 10.0


class AccountOrdersFeed:
    """Authenticated stream of our own order updates (settlement channel)."""

    def __init__(self, name: str, ws_url: str, market_id: int,
                 account_index: int, signer) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.account_index = account_index
        self.signer = signer
        self.ready = asyncio.Event()
        self._pending: dict[int, asyncio.Future] = {}
        self._terminal: OrderedDict[int, dict] = OrderedDict()
        # B3: the taker path only ever needed terminal outcomes. A maker
        # order is interesting precisely while it is NOT terminal -- resting,
        # or resting with half of it already filled -- so every frame is now
        # remembered, and the still-open ones are indexed separately.
        self._latest: OrderedDict[int, dict] = OrderedDict()
        self.open_orders: dict = {}

    def watch(self, coi: int) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if coi in self._terminal:
            fut.set_result(self._terminal[coi])
            return fut
        self._pending[coi] = fut
        return fut

    def unwatch(self, coi: int) -> None:
        fut = self._pending.pop(coi, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def _resolve(self, coi: int, info: dict) -> None:
        self._terminal[coi] = info
        while len(self._terminal) > 512:
            self._terminal.popitem(last=False)
        fut = self._pending.pop(coi, None)
        if fut is not None and not fut.done():
            fut.set_result(info)

    def latest(self, coi: int) -> Optional[dict]:
        """Last frame seen for this client order index, open or terminal."""
        return self._latest.get(coi)

    def _remember(self, coi: int, info: dict) -> None:
        self._latest[coi] = info
        self._latest.move_to_end(coi)
        while len(self._latest) > 512:
            self._latest.popitem(last=False)

    def _handle_orders(self, msg: dict) -> None:
        for lst in (msg.get("orders") or {}).values():
            for o in lst or []:
                status = str(o.get("status", "")).strip().lower()
                try:
                    coi = int(o.get("client_order_index"))
                except (TypeError, ValueError):
                    continue
                fb = float(o.get("filled_base_amount") or 0.0)
                fq = float(o.get("filled_quote_amount") or 0.0)
                info = {"status": status, "filled_base": fb, "filled_quote": fq,
                        "avg_px": (fq / fb) if fb > 0 else None,
                        "terminal": status not in OPEN_STATUSES}
                self._remember(coi, info)
                if status in OPEN_STATUSES:
                    self.open_orders[coi] = o
                    continue
                self.open_orders.pop(coi, None)
                self._resolve(coi, info)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                auth, err = self.signer.create_auth_token_with_expiry()
                if err is not None:
                    raise RuntimeError(f"auth token: {err}")
                connected_at = time.time()
                # Same settings as the book feeds (feeds.WS_KWARGS): this is
                # the SETTLEMENT channel, so a connection dropped by our own
                # aggressive keepalive is a fill we learn about late.
                async with ws_connect(self.ws_url, **WS_KWARGS) as ws:
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t in ("subscribed/account_orders", "update/account_orders"):
                            if t.startswith("subscribed"):
                                log.info("[%s] account orders stream ready", self.name)
                            self.ready.set()
                            self._handle_orders(msg)
                        elif t == "connected":
                            await ws.send(json.dumps({
                                "type": "subscribe",
                                "channel": f"account_orders/{self.market_id}/"
                                           f"{self.account_index}",
                                "auth": auth}))
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
                        if (time.time() - connected_at > AUTH_REFRESH_SEC
                                and not self._pending):
                            log.info("[%s] refreshing account ws auth", self.name)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] account ws error: %s — retry in %.0fs",
                            self.name, e, backoff)
                self.ready.clear()
                if stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.ready.clear()


class LighterVenue:
    kind = "lighter"

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        assert conf.lighter_profile is not None
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.profile = conf.lighter_profile
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.market_id = -1
        self.price_decimals = 2
        self.size_decimals = 4
        self.min_base = 0.0
        self.min_quote = 10.0
        self.signer = None
        self.orders_feed: Optional[AccountOrdersFeed] = None
        self._coi = int(time.time() * 1000)
        self._warm_fails = 0

    # ------------------------------------------------------------------ REST

    async def _get(self, path: str, params: Optional[dict] = None):
        async with self.session.get(
                self.profile.api_url + path, params=params,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        data = await self._get("/api/v1/orderBooks")
        for ob in data.get("order_books") or []:
            if ob.get("symbol") != self.conf.symbol:
                continue
            if ob.get("status") != "active":
                raise RuntimeError(f"[{self.name}] market status={ob.get('status')}")
            self.market_id = int(ob["market_id"])
            self.price_decimals = int(ob["supported_price_decimals"])
            self.size_decimals = int(ob["supported_size_decimals"])
            self.min_base = float(ob["min_base_amount"])
            self.min_quote = float(ob["min_quote_amount"])
            log.info("[%s] %s market_id=%d px_dec=%d sz_dec=%d min_base=%s "
                     "min_quote=%s taker_fee=%s", self.name, ob["symbol"],
                     self.market_id, self.price_decimals, self.size_decimals,
                     ob["min_base_amount"], ob["min_quote_amount"],
                     ob.get("taker_fee"))
            return
        raise RuntimeError(f"[{self.name}] {self.conf.symbol} not found on "
                           f"{self.profile.name}")

    def init_signer(self) -> None:
        c = self.conf.lighter_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            from lighter import SignerClient
        except ImportError as e:
            raise RuntimeError(
                "live trading on Lighter needs the official SDK — "
                "pip install -r requirements-live.txt "
                "(git+https://github.com/elliottech/lighter-python.git)") from e
        signer = SignerClient(
            url=self.profile.api_url,
            account_index=c.account_index,
            api_private_keys={c.api_key_index: c.api_private_key},
            chain_id=self.profile.chain_id,
        )
        err = signer.check_client()
        if err is not None:
            raise RuntimeError(f"[{self.name}] API key check failed: {err}")
        self.signer = signer
        log.info("[%s] signer ready (account %d)", self.name, c.account_index)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            LighterBookFeed(self.name, self.profile.ws_url, self.market_id,
                            self.book, notify).run(stop),
            name=f"book-{self.key}")]
        if live and self.signer is None:
            # Shadow mode reaches here: the strategy runs but no signer was
            # built. The account stream needs one to authenticate, so
            # starting it would only produce an endless reconnect loop
            # against an error that cannot resolve itself.
            log.info("[%s] no signer — account order stream not started "
                     "(expected in shadow mode)", self.name)
        elif live:
            self.orders_feed = AccountOrdersFeed(
                self.name, self.profile.ws_url, self.market_id,
                self.conf.lighter_creds.account_index, self.signer)
            tasks.append(asyncio.create_task(self.orders_feed.run(stop),
                                             name=f"acct-{self.key}"))
        return tasks

    def ready_to_trade(self) -> bool:
        return self.orders_feed is not None and self.orders_feed.ready.is_set()

    async def warm_http(self) -> None:
        """Keep the order-path HTTPS connections warm (a cold TLS handshake
        adds 10-15ms to the first order after an idle spell).

        Endpoint chosen by measurement, 2026-09-05: `/api/v1/status`,
        `/blockHeight`, `/info` and `/layer2BasicInfo` all answer 403 from
        CloudFront, so the ping this method used to send had NEVER once
        succeeded -- and it said so at debug level, which is the same as not
        saying it. `orderBookDetails` for our own market answers 200 in ~90ms
        for 1.4 KB, which is the lightest thing that actually works.
        """
        try:
            await self._get("/api/v1/orderBookDetails",
                            {"market_id": str(self.market_id)})
            self._warm_fails = 0
        except Exception as e:
            # A keepalive that always fails is a keepalive that does not
            # exist. Silence here is how the previous one survived.
            self._warm_fails = getattr(self, "_warm_fails", 0) + 1
            if self._warm_fails in (5, 50) or self._warm_fails % 500 == 0:
                log.warning("[%s] order-path keepalive has failed %d times "
                            "in a row: %r — the first order after an idle "
                            "spell will pay a cold TLS handshake",
                            self.name, self._warm_fails, e)
        if self.signer is None:
            return
        try:
            sess = self.signer.api_client.rest_client.pool_manager
            async with sess.get(self.profile.api_url + "/api/v1/status",
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
                await r.read()
        except Exception as e:
            log.debug("[%s] signer keepalive failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        f = 10 ** self.price_decimals
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    def px_tick(self, px: float) -> float:
        """One price increment. zkLighter's grid is a fixed decimal count."""
        return round(10.0 ** -self.price_decimals, 10)

    # ------------------------------------------------------------- execution

    def _next_coi(self) -> int:
        self._coi += 1
        return self._coi

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """Market order with avg-price protection; settle via account ws."""
        assert self.signer is not None
        from lighter import SignerClient
        coi = self._next_coi()
        fut = self.orders_feed.watch(coi) if self.orders_feed else None
        base_amount = int(round(qty * 10 ** self.size_decimals))
        price = int(round(limit_px * 10 ** self.price_decimals))
        try:
            _tx, resp, err = await self.signer.create_order(
                market_index=self.market_id,
                client_order_index=coi,
                base_amount=base_amount,
                price=price,
                is_ask=not is_buy,
                order_type=SignerClient.ORDER_TYPE_MARKET,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                reduce_only=reduce_only,
                order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
            )
        except Exception as e:
            if fut is not None:
                self.orders_feed.unwatch(coi)
            msg = f"{type(e).__name__}: {e}"
            if getattr(e, "status", None) == 429 or "(429)" in str(e):
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": msg, "unresolved": False}
        if err is not None or (getattr(resp, "code", 200) or 200) != 200:
            if fut is not None:
                self.orders_feed.unwatch(coi)
            msg = str(err) if err is not None else \
                f"tx rejected code={resp.code} msg={getattr(resp, 'message', None)}"
            if "rate limit" in msg.lower():
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": msg, "unresolved": False}
        if fut is None:
            return {"status": "sent-unconfirmed", "filled_base": 0.0,
                    "avg_px": None, "err": None, "unresolved": True}
        try:
            info = await asyncio.wait_for(fut, timeout=self.settle_timeout)
            return {"status": info["status"], "filled_base": info["filled_base"],
                    "avg_px": info.get("avg_px"), "err": None, "unresolved": False}
        except asyncio.TimeoutError:
            self.orders_feed.unwatch(coi)
            log.warning("[%s] no settle confirmation for coi %d in %.1fs",
                        self.name, coi, self.settle_timeout)
            return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}

    # ----------------------------------------------------------- maker (B3)
    #
    # zkLighter cancels BY THE CLIENT ORDER INDEX we chose ourselves
    # (`cancel_order(market_index, order_index=<the client_order_index used at
    # create>)`, verified against the SDK's own create/modify/cancel example
    # 2026-09-04). Two consequences, both good:
    #   * we can cancel an order whose send never came back, and
    #   * cancelling twice is the same as cancelling once.
    # What the SDK returns is only whether the cancel TRANSACTION was
    # accepted. The order's death arrives later, on the account websocket --
    # so nothing here concludes anything; poll_order() is the only truth.

    async def send_maker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """Post-only limit order. Rests on the book; never crosses."""
        assert self.signer is not None
        from lighter import SignerClient
        coi = self._next_coi()
        out = {"status": "send-failed", "handle": coi, "filled_base": 0.0,
               "avg_px": None, "err": None, "unresolved": False}
        base_amount = int(round(qty * 10 ** self.size_decimals))
        price = int(round(limit_px * 10 ** self.price_decimals))
        try:
            _tx, resp, err = await self.signer.create_order(
                market_index=self.market_id,
                client_order_index=coi,
                base_amount=base_amount,
                price=price,
                is_ask=not is_buy,
                order_type=SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
                reduce_only=reduce_only,
            )
        except Exception as e:
            # The transaction may or may not have landed. The order index is
            # ours and already allocated, so the caller can still cancel it --
            # which is exactly what it must do rather than assume nothing
            # happened.
            msg = f"{type(e).__name__}: {e}"
            if getattr(e, "status", None) == 429 or "(429)" in str(e):
                msg = "RATE_LIMITED: " + msg
            out["status"] = "send-unresolved"
            out["unresolved"] = True
            out["err"] = msg
            return out
        if err is not None or (getattr(resp, "code", 200) or 200) != 200:
            msg = str(err) if err is not None else \
                f"tx rejected code={resp.code} msg={getattr(resp, 'message', None)}"
            if "rate limit" in msg.lower():
                msg = "RATE_LIMITED: " + msg
            out["err"] = msg
            return out
        # Accepted for sequencing. Whether it rested (or was killed as
        # canceled-post-only) shows up on the account stream.
        out["status"] = "sent"
        return out

    async def poll_order(self, handle) -> dict:
        """Latest account-stream frame for this order.

        There is deliberately no REST fallback here. Lighter's REST account
        state lags its websocket settlements -- the engine already carries a
        five-second grace guard for exactly that (RECONCILE_GRACE_SEC) -- so a
        second, differently-lagging source of truth would create the very
        disagreement this codebase spent B4 removing. When the stream is
        silent we say `unknown` and the caller escalates to a position
        reconcile, which reads the chain and is authoritative.
        """
        out = {"status": "unknown", "filled_base": None, "avg_px": None,
               "terminal": False, "err": None}
        if handle is None or self.orders_feed is None:
            return out
        info = self.orders_feed.latest(int(handle))
        if info is None:
            if not self.orders_feed.ready.is_set():
                out["err"] = "account stream down"
            return out
        out["status"] = info["status"]
        out["filled_base"] = info["filled_base"]
        out["avg_px"] = info.get("avg_px")
        out["terminal"] = bool(info.get("terminal"))
        return out

    async def cancel_order(self, handle) -> dict:
        """Ask the sequencer to cancel one order. Idempotent by construction:
        the index is ours. Says nothing about the order's fate."""
        if handle is None or self.signer is None:
            return {"status": "rejected", "err": "no handle"}
        try:
            _tx, resp, err = await self.signer.cancel_order(
                market_index=self.market_id, order_index=int(handle))
        except Exception as e:
            # Cancels are idempotent, so "did it land?" is answered by
            # retrying rather than by guessing.
            return {"status": "unresolved", "err": f"{type(e).__name__}: {e}"}
        if err is not None or (getattr(resp, "code", 200) or 200) != 200:
            msg = str(err) if err is not None else \
                f"tx rejected code={resp.code} msg={getattr(resp, 'message', None)}"
            low = msg.lower()
            if "not found" in low or "already" in low or "filled" in low:
                return {"status": "gone", "err": msg}
            return {"status": "rejected", "err": msg}
        return {"status": "accepted", "err": None}

    async def _auth_get(self, path: str, params: dict):
        auth, err = self.signer.create_auth_token_with_expiry()
        if err is not None:
            raise RuntimeError(f"auth token: {err}")
        async with self.session.get(
                self.profile.api_url + path, params=params,
                headers={"authorization": auth},
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    async def cancel_open_orders(self) -> int:
        """Cancel every resting order this account has in THIS market.

        Called once at live startup. A previous process that died with a
        quote on the book leaves an order nobody is hedging -- the same naked
        exposure as a lost cancel, with no one watching at all. Read over
        REST rather than from the stream: at startup we have not traded yet,
        so REST lag is not a hazard, and "what is actually on the book" must
        not depend on a subscription snapshot having arrived.
        """
        if self.signer is None:
            return 0
        c = self.conf.lighter_creds
        data = await self._auth_get("/api/v1/accountActiveOrders",
                                    {"account_index": str(c.account_index),
                                     "market_id": str(self.market_id)})
        cois = []
        for o in data.get("orders") or []:
            try:
                cois.append(int(o["client_order_index"]))
            except (KeyError, TypeError, ValueError):
                log.error("[%s] resting order without a usable "
                          "client_order_index: %s", self.name, str(o)[:200])
                raise RuntimeError(
                    f"[{self.name}] a resting order cannot be addressed for "
                    f"cancellation — refusing to trade on top of it")
        if not cois:
            return 0
        log.critical("[%s] %d resting order(s) found at startup — cancelling "
                     "before trading / 启动时发现挂单，先撤单再交易",
                     self.name, len(cois))
        for coi in cois:
            res = await self.cancel_order(coi)
            if res["status"] in ("rejected", "unresolved"):
                raise RuntimeError(
                    f"[{self.name}] startup cancel of order {coi} did not "
                    f"confirm ({res['err']}) — refusing to trade on top of "
                    f"orders we do not control")
        return len(cois)

    # -------------------------------------------------------------- accounts

    async def _account(self) -> Optional[dict]:
        c = self.conf.lighter_creds
        if c is None or c.account_index is None:
            return None
        data = await self._get("/api/v1/account",
                               params={"by": "index",
                                       "value": str(c.account_index)})
        for acct in data.get("accounts") or []:
            return acct
        return None

    async def fetch_equity(self):
        acct = await self._account()
        if acct is None:
            return None
        return (float(acct.get("total_asset_value") or 0.0),
                float(acct.get("available_balance") or 0.0))

    async def fetch_position(self) -> float:
        acct = await self._account()
        if acct is None:
            raise RuntimeError(f"[{self.name}] account not found")
        for p in acct.get("positions") or []:
            if int(p.get("market_id", -1)) == self.market_id:
                return float(p.get("sign") or 1.0) * float(p.get("position") or 0.0)
        return 0.0

    async def close(self) -> None:
        if self.signer is not None:
            try:
                await self.signer.api_client.close()
            except Exception:
                pass
