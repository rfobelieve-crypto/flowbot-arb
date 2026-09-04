"""Hyperliquid HIP-3 dex venue adapter (Entropy = dex "io", trade.xyz = "xyz").

Market metadata, account state and order posting use Hyperliquid's public
/info and /exchange REST endpoints via plain aiohttp; the book comes from the
OFFICIAL websocket (see feeds.HLBookFeed). Trading lazily imports the
official `hyperliquid-python-sdk` signing helpers + eth_account —
--record-only data collection needs neither.

IOC limit orders settle synchronously in the /exchange response; unknown
outcomes (timeout/5xx) fall back to orderStatus-by-cloid polling inside
send_taker(), so the engine sees the same unified result shape as the Lighter
venue: {status, filled_base, avg_px, err, unresolved}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Optional

import aiohttp

from .book import OrderBook
from .config import VenueConf
from .feeds import HLBookFeed

log = logging.getLogger("hl")

INFO_TIMEOUT = 10.0


class NonceAllocator:
    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        self._last = max(self._last + 1, int(time.time() * 1000))
        return self._last


class HLAccount:
    def __init__(self, private_key: str, account_address: Optional[str],
                 api_url: str) -> None:
        from eth_account import Account
        self.wallet = Account.from_key(private_key)
        self.query_address = (account_address or self.wallet.address).lower()
        self.is_mainnet = api_url == "https://api.hyperliquid.xyz"
        self.nonces = NonceAllocator()

    def describe(self) -> str:
        s = f"signer={self.wallet.address} account={self.query_address}"
        if self.wallet.address.lower() != self.query_address:
            s += " (agent mode)"
        return s


class HLVenue:
    kind = "hl"

    def __init__(self, conf: VenueConf, api_url: str, ws_url: str,
                 session: aiohttp.ClientSession, settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.api_url = api_url
        self.ws_url = ws_url
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        # Whether this leg's equity should include the CORE (dex "") bucket.
        # HIP-3 clearinghouses are funded separately -- USDC sitting in core
        # or spot cannot margin an io position until it is transferred in
        # (confirmed 2026-09-04; engine/README: "Fund the dex-specific
        # clearinghouses you trade"). So a HIP-3 leg reports only its own
        # bucket: counting money it cannot use would overstate the equity on
        # the dashboard. A leg that IS core (dex "") still counts core.
        # Also cleared when two venues share one HL account (count once).
        self.include_core_equity = not conf.hl_dex
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.account: Optional[HLAccount] = None
        self.coin = ""
        self.asset_id = -1
        self.size_decimals = 0
        self.min_base = 0.0
        self.min_quote = 10.0
        self._cloid = int(time.time() * 1000)
        self._signing = None      # lazy hyperliquid-sdk signing module

    async def _info(self, payload: dict):
        async with self.session.post(
                self.api_url + "/info", json=payload,
                timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    async def load_market(self) -> None:
        dexs = await self._info({"type": "perpDexs"})
        names = [(d or {}).get("name", "") for d in dexs]
        if self.conf.hl_dex not in names:
            raise RuntimeError(f"[{self.name}] dex '{self.conf.hl_dex}' not "
                               f"found on Hyperliquid (available: "
                               f"{[n for n in names if n][:20]}...)")
        dex_index = names.index(self.conf.hl_dex)
        meta = await self._info({"type": "meta", "dex": self.conf.hl_dex})
        want = f"{self.conf.hl_dex}:{self.conf.symbol}"
        for idx, a in enumerate(meta["universe"]):
            if a["name"] not in (want, self.conf.symbol):
                continue
            if a.get("isDelisted"):
                raise RuntimeError(f"[{self.name}] {a['name']} is delisted")
            self.coin = a["name"]
            # core mainnet (dex "") uses plain universe indices; HIP-3 dexs
            # use the 110000 + block scheme. Local patch 2026-08-30.
            self.asset_id = (idx if not self.conf.hl_dex
                             else 110000 + (dex_index - 1) * 10000 + idx)
            self.size_decimals = int(a["szDecimals"])
            self.min_base = 10 ** -self.size_decimals
            log.info("[%s] %s asset_id=%d szDecimals=%d maxLev=%sx %s",
                     self.name, self.coin, self.asset_id, self.size_decimals,
                     a.get("maxLeverage"),
                     "isolated-only" if a.get("onlyIsolated") else "")
            return
        raise RuntimeError(f"[{self.name}] {want} not found")

    def init_signer(self) -> None:
        c = self.conf.hl_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            from hyperliquid.utils import signing as hl_signing
        except ImportError as e:
            raise RuntimeError(
                "live trading on Hyperliquid needs the official SDK — "
                "pip install -r requirements-live.txt "
                "(hyperliquid-python-sdk)") from e
        self._signing = hl_signing
        self.account = HLAccount(c.private_key, c.account_address, self.api_url)
        log.info("[%s] %s", self.name, self.account.describe())

    def share_nonces_with(self, other: "HLVenue") -> None:
        """One signer address must use one nonce sequence."""
        if (self.account and other.account and
                self.account.wallet.address == other.account.wallet.address):
            other.account.nonces = self.account.nonces
            log.info("[%s]/[%s] same signer — shared nonce allocator",
                     self.name, other.name)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        return [asyncio.create_task(
            HLBookFeed(self.name, self.ws_url, self.coin, self.book,
                       notify).run(stop),
            name=f"book-{self.key}")]

    def ready_to_trade(self) -> bool:
        return self.account is not None

    async def warm_http(self) -> None:
        """Order-path keepalive ping (driven by the engine's keepalive loop)."""
        try:
            await self._info({"type": "exchangeStatus"})
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def _px_decimals(self, px: float) -> int:
        max_dec = max(0, 6 - self.size_decimals)
        sig_dec = 4 - math.floor(math.log10(px))
        return max(0, min(max_dec, sig_dec))

    def px_round(self, px: float, round_up: bool) -> float:
        if px <= 0:
            return px
        f = 10.0 ** self._px_decimals(px)
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    def px_tick(self, px: float) -> float:
        """One price increment at this price level. Hyperliquid's grid is
        significant-figure based, so the tick depends on where you are."""
        if px <= 0:
            return 0.0
        return round(10.0 ** -self._px_decimals(px), 10)

    # ------------------------------------------------------------- execution

    def _next_cloid(self):
        from hyperliquid.utils.types import Cloid
        self._cloid += 1
        return Cloid.from_int(self._cloid)

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        assert self.account is not None and self.asset_id >= 0
        s = self._signing
        cloid = self._next_cloid()
        order_req = {"coin": self.coin, "is_buy": is_buy, "sz": round(qty, 8),
                     "limit_px": limit_px,
                     "order_type": {"limit": {"tif": "Ioc"}},
                     "reduce_only": reduce_only, "cloid": cloid}
        try:
            wire = s.order_request_to_order_wire(order_req, self.asset_id)
            action = s.order_wires_to_order_action([wire])
            nonce = self.account.nonces.next()
            sig = s.sign_l1_action(self.account.wallet, action, None, nonce,
                                   None, self.account.is_mainnet)
            payload = {"action": action, "nonce": nonce, "signature": sig,
                       "vaultAddress": None, "expiresAfter": None}
        except Exception as e:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": f"signing failed: {e!r}", "unresolved": False}

        body, err, unresolved = await self._post_exchange(payload)
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": err, "unresolved": False}
        if not unresolved:
            res = self._parse(body)
            if not res.get("unresolved"):
                return res
        # unknown outcome: poll orderStatus by cloid until the deadline
        deadline = time.time() + self.settle_timeout
        while time.time() < deadline:
            try:
                st = await self._info({"type": "orderStatus",
                                       "user": self.account.query_address,
                                       "oid": cloid.to_raw()})
            except Exception:
                st = None
            if st and st.get("status") == "order":
                o = st.get("order") or {}
                status = str(o.get("status", ""))
                inner = o.get("order") or {}
                try:
                    filled = max(float(inner.get("origSz") or 0)
                                 - float(inner.get("sz") or 0), 0.0)
                except (TypeError, ValueError):
                    filled = 0.0
                if status != "open":
                    return {"status": status, "filled_base": filled,
                            "avg_px": None, "err": None, "unresolved": False}
            await asyncio.sleep(0.5)
        return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": True}

    async def _post_exchange(self, payload: dict):
        try:
            async with self.session.post(
                    self.api_url + "/exchange", json=payload,
                    timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT)) as r:
                text = await r.text()
                if r.status == 429:
                    return None, f"RATE_LIMITED: HTTP 429 {text[:150]}", False
                if 400 <= r.status < 500:
                    return None, f"HTTP {r.status}: {text[:250]}", False
                if r.status >= 500:
                    return None, None, True
                return json.loads(text), None, False
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError):
            return None, None, True

    @staticmethod
    def _parse(body: dict) -> dict:
        def fail(msg: str) -> dict:
            low = msg.lower()
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": msg, "unresolved": False}
        if body.get("status") == "err":
            return fail(str(body.get("response")))
        if body.get("status") != "ok":
            return fail(f"unexpected response: {str(body)[:200]}")
        try:
            st = body["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return fail(f"malformed response: {str(body)[:200]}")
        if "filled" in st:
            f = st["filled"]
            return {"status": "filled",
                    "filled_base": float(f.get("totalSz") or 0.0),
                    "avg_px": float(f["avgPx"]) if f.get("avgPx") else None,
                    "err": None, "unresolved": False}
        if "error" in st:
            msg = str(st["error"])
            if "could not immediately match" in msg.lower():
                return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                        "err": None, "unresolved": False}
            return fail(msg)
        if "resting" in st:
            return {"status": "resting?", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        return fail(f"unknown status: {str(st)[:150]}")

    # --------------------------------------------------------- maker (B3)
    #
    # Everything below exists for the resting half of execution. Three
    # properties matter and are worth stating once:
    #
    # * The cloid is allocated BEFORE the network call and returned in EVERY
    #   result, success or not. A send that timed out may well have placed an
    #   order, and an order we cannot name is an order we cannot cancel.
    # * Nothing here concludes anything about an order's fate. cancel_order()
    #   reports whether the exchange ACCEPTED the request; whether the order
    #   died, and with how much filled, comes only from poll_order().
    # * An unrecognised or unreachable answer is "unknown", never "gone".

    def _signed_payload(self, action: dict) -> dict:
        """Wrap one L1 action in this account's next nonce and signature."""
        s = self._signing
        nonce = self.account.nonces.next()
        sig = s.sign_l1_action(self.account.wallet, action, None, nonce,
                               None, self.account.is_mainnet)
        return {"action": action, "nonce": nonce, "signature": sig,
                "vaultAddress": None, "expiresAfter": None}

    async def send_maker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """Post-only (ALO) limit order. Rests on the book; never crosses."""
        assert self.account is not None and self.asset_id >= 0
        s = self._signing
        cloid = self._next_cloid()
        out = {"status": "send-failed", "handle": cloid, "filled_base": 0.0,
               "avg_px": None, "err": None, "unresolved": False}
        order_req = {"coin": self.coin, "is_buy": is_buy, "sz": round(qty, 8),
                     "limit_px": limit_px,
                     "order_type": {"limit": {"tif": "Alo"}},
                     "reduce_only": reduce_only, "cloid": cloid}
        try:
            wire = s.order_request_to_order_wire(order_req, self.asset_id)
            payload = self._signed_payload(s.order_wires_to_order_action([wire]))
        except Exception as e:
            out["err"] = f"signing failed: {e!r}"
            return out
        body, err, unresolved = await self._post_exchange(payload)
        if err is not None:
            out["err"] = err
            return out
        if unresolved:
            # Timeout / 5xx. The order may be resting right now. Say so; the
            # caller must treat it as live and cancel it, not forget it.
            out["status"] = "send-unresolved"
            out["unresolved"] = True
            return out
        res = self._parse_maker(body)
        res["handle"] = cloid
        return res

    @staticmethod
    def _parse_maker(body: dict) -> dict:
        base = {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": False}
        try:
            st = body["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            if body.get("status") == "err":
                base["err"] = str(body.get("response"))
            else:
                base["err"] = f"malformed response: {str(body)[:200]}"
            return base
        if "resting" in st:
            base["status"] = "resting"
            return base
        if "filled" in st:
            f = st["filled"]
            base["status"] = "filled"
            base["filled_base"] = float(f.get("totalSz") or 0.0)
            base["avg_px"] = float(f["avgPx"]) if f.get("avgPx") else None
            return base
        if "error" in st:
            msg = str(st["error"])
            low = msg.lower()
            if "post only" in low or "post-only" in low:
                # Would have crossed. Not a failure: the book moved between
                # planning and sending, which is the normal cost of resting.
                base["status"] = "post-only-reject"
                return base
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            base["err"] = msg
            return base
        base["err"] = f"unknown status: {str(st)[:150]}"
        return base

    async def poll_order(self, handle) -> dict:
        """Current exchange truth for one order. `terminal` means the order
        can never fill again; `unknown` means we learned nothing this time."""
        out = {"status": "unknown", "filled_base": None, "avg_px": None,
               "terminal": False, "err": None}
        if handle is None or self.account is None:
            return out
        try:
            st = await self._info({"type": "orderStatus",
                                   "user": self.account.query_address,
                                   "oid": handle.to_raw()})
        except Exception as e:
            out["err"] = repr(e)
            return out
        if not st or st.get("status") != "order":
            # "unknownOid" lands here. It can mean never-placed OR aged out
            # of the cache, and we cannot tell which, so we conclude nothing.
            out["err"] = str(st.get("status")) if isinstance(st, dict) else None
            return out
        o = st.get("order") or {}
        inner = o.get("order") or {}
        try:
            out["filled_base"] = max(float(inner.get("origSz") or 0)
                                     - float(inner.get("sz") or 0), 0.0)
        except (TypeError, ValueError):
            out["filled_base"] = None
        out["status"] = str(o.get("status") or "unknown")
        out["terminal"] = out["status"] != "open"
        return out

    async def cancel_order(self, handle) -> dict:
        """Ask the exchange to remove one order. Reports only whether the
        REQUEST was accepted -- the order's fate comes from poll_order()."""
        if handle is None or self.account is None:
            return {"status": "rejected", "err": "no handle"}
        action = {"type": "cancelByCloid",
                  "cancels": [{"asset": self.asset_id,
                               "cloid": handle.to_raw()}]}
        try:
            payload = self._signed_payload(action)
        except Exception as e:
            return {"status": "rejected", "err": f"signing failed: {e!r}"}
        body, err, unresolved = await self._post_exchange(payload)
        if err is not None:
            return {"status": "rejected", "err": err}
        if unresolved:
            return {"status": "unresolved", "err": None}
        try:
            st = body["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return {"status": "unresolved",
                    "err": f"malformed cancel response: {str(body)[:200]}"}
        if st == "success":
            return {"status": "accepted", "err": None}
        msg = str(st.get("error") if isinstance(st, dict) else st)
        low = msg.lower()
        if "never placed" in low or "already canceled" in low or "filled" in low:
            # The order is off the book -- but this does NOT say whether it
            # filled or was canceled, so it is "gone", not "canceled".
            return {"status": "gone", "err": msg}
        return {"status": "rejected", "err": msg}

    async def cancel_open_orders(self) -> int:
        """Cancel every resting order this account has in THIS market.

        Called once at live startup. A previous process that died with a
        quote on the book leaves an order nobody is hedging -- the same naked
        exposure as a lost cancel, only with no one watching at all.
        """
        if self.account is None:
            return 0
        try:
            req = {"type": "openOrders", "user": self.account.query_address}
            if self.conf.hl_dex:
                req["dex"] = self.conf.hl_dex
            orders = await self._info(req)
        except Exception as e:
            # B3 audit S6.4 (2026-09-04): this used to log a warning and
            # return 0, which reads as "no resting orders" -- the one
            # conclusion the failure does not support. The Lighter side
            # already refused to start here; the asymmetry existed only
            # because `openOrders` with a `dex` parameter was unverified on
            # HIP-3. Both sides now refuse: you may not trade beside orders
            # you cannot enumerate.
            raise RuntimeError(
                f"[{self.name}] cannot list open orders ({e!r}) — refusing to "
                f"trade without knowing what is already resting / 无法列出挂单，"
                f"拒绝在不掌握的挂单旁边交易")
        if orders is None:
            raise RuntimeError(
                f"[{self.name}] openOrders returned nothing for dex "
                f"{self.conf.hl_dex!r} — cannot tell 'no orders' from "
                f"'unsupported query'")
        oids = [int(o["oid"]) for o in (orders or [])
                if o.get("coin") == self.coin and o.get("oid") is not None]
        if not oids:
            return 0
        log.critical("[%s] %d resting order(s) found at startup — cancelling "
                     "before trading / 启动时发现挂单，先撤单再交易", self.name,
                     len(oids))
        action = {"type": "cancel",
                  "cancels": [{"a": self.asset_id, "o": o} for o in oids]}
        try:
            payload = self._signed_payload(action)
        except Exception as e:
            raise RuntimeError(f"[{self.name}] cannot sign startup cancel: {e!r}")
        body, err, unresolved = await self._post_exchange(payload)
        if err is not None or unresolved:
            raise RuntimeError(
                f"[{self.name}] startup cancel of {len(oids)} resting order(s) "
                f"did not confirm ({err or 'unresolved'}) — refusing to trade "
                f"on top of orders we do not control")
        return len(oids)

    # -------------------------------------------------------------- accounts

    def _query_address(self):
        if self.account is not None:
            return self.account.query_address
        c = self.conf.hl_creds
        return c.account_address.lower() if c and c.account_address else None

    async def fetch_equity(self):
        """Unified account equity via the portfolio endpoint — the same
        Portfolio Value the HL UI shows. Falls back to summing clearinghouse
        buckets if the endpoint shape changes. When both venues share one HL
        account (include_core_equity cleared on the hedge), that venue reports
        only its dex bucket to avoid double-counting."""
        addr = self._query_address()
        if addr is None:
            return None
        if self.include_core_equity:
            try:
                p = await self._info({"type": "portfolio", "user": addr})
                for period, d in p:
                    if period == "day":
                        hist = d.get("accountValueHistory") or []
                        if hist:
                            return float(hist[-1][1]), None
            except Exception as e:
                log.debug("[%s] portfolio fetch failed, falling back: %r",
                          self.name, e)
        dexs = [self.conf.hl_dex] + ([""] if self.include_core_equity else [])
        eq = fr = 0.0
        for dex in dexs:
            st = await self._info({"type": "clearinghouseState", "user": addr,
                                   "dex": dex})
            ms = st.get("marginSummary") or {}
            eq += float(ms.get("accountValue") or 0.0)
            fr += float(st.get("withdrawable") or 0.0)
        return eq, fr

    async def fetch_position(self) -> float:
        addr = self._query_address()
        assert addr is not None
        st = await self._info({"type": "clearinghouseState", "user": addr,
                               "dex": self.conf.hl_dex})
        for ap in st.get("assetPositions") or []:
            pos = ap.get("position") or {}
            if pos.get("coin") == self.coin:
                return float(pos.get("szi") or 0.0)
        return 0.0

    async def close(self) -> None:
        pass
