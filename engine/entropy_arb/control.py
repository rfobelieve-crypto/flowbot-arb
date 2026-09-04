"""B5 control channel: four verbs, out of band (2026-09-04).

`LIVE_50U_SPEC.md` §6 fixed the shape before any of it was written, and the
reasons are scars rather than preferences:

* **It does not go through the dashboard.** The dashboard is read-only and
  has no buttons, and the control channel has to work on the day the
  dashboard is the thing that broke. So: Telegram, plus a command file you
  can `echo` into over SSH when Telegram is the thing that broke.
* **Destructive verbs verify real external state first.** `/okx-admin/heal`
  was once a GET endpoint; a link preview prefetched it with `confirm=YES`
  and zeroed a live position in the database, orphaning the real one
  (mistake.md 2026-06-07, and it was the second time). The rule written
  there is: anything that changes real exchange state checks the real
  exchange first. `flat` therefore reconciles positions against the venues
  BEFORE it closes anything.
* **Every verb is idempotent.** Sending it twice does what sending it once
  did. The operator is going to be in a hurry.
* **Before and after, it says what it did.** A control channel you cannot
  audit is a control channel you will not trust at the moment you need it.

What this channel deliberately CANNOT do: place an order, change a
threshold, or lift a HALT. A halt is one-way on purpose -- it needs a
restart, because a restart is what re-reads real positions with strict=True.
A message from a phone must not be able to undo a kill switch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Optional

import aiohttp

log = logging.getLogger("control")

VERBS = ("pause", "resume", "flat", "status")
HELP = ("commands: /pause (stop opening, keep hedging) | /resume | "
        "/flat (close everything, stay paused) | /status")


class CriticalRelay(logging.Handler):
    """Buffers CRITICAL log lines for the control channel to push out.

    A logging handler must never do network I/O: it is called from the event
    loop (and from inside except blocks), so a slow send would stall the
    order path. It only appends here; the channel drains it.
    """

    def __init__(self, maxlen: int = 50) -> None:
        super().__init__(level=logging.CRITICAL)
        self.lines: deque = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:                                       # noqa: BLE001
            pass


class TelegramClient:
    def __init__(self, session: aiohttp.ClientSession, token: str,
                 chat_id: str) -> None:
        self.session = session
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)

    async def send(self, text: str) -> bool:
        try:
            async with self.session.post(
                    self.base + "/sendMessage",
                    json={"chat_id": self.chat_id, "text": text[:3900],
                          "disable_notification": False},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                return r.status == 200
        except Exception as e:                                  # noqa: BLE001
            log.warning("telegram send failed: %r", e)
            return False

    async def poll(self, offset: int, timeout: int = 25):
        """Long-poll getUpdates. Returns (messages, next_offset)."""
        try:
            async with self.session.get(
                    self.base + "/getUpdates",
                    params={"offset": str(offset), "timeout": str(timeout),
                            "allowed_updates": json.dumps(["message"])},
                    timeout=aiohttp.ClientTimeout(total=timeout + 15)) as r:
                if r.status != 200:
                    log.warning("telegram getUpdates HTTP %d", r.status)
                    return [], offset
                body = await r.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001
            log.debug("telegram poll failed: %r", e)
            return [], offset
        msgs = []
        for upd in body.get("result") or []:
            offset = max(offset, int(upd.get("update_id", 0)) + 1)
            m = upd.get("message") or {}
            chat = str((m.get("chat") or {}).get("id", ""))
            text = (m.get("text") or "").strip()
            if not text:
                continue
            if chat != self.chat_id:
                # Not our operator. Do not reply: an answer would confirm
                # the bot exists to whoever found it.
                log.warning("ignored control message from chat %s", chat)
                continue
            msgs.append(text)
        return msgs, offset


class ControlChannel:
    """Reads commands from Telegram and from a command file, applies them to
    the engine, and reports what happened."""

    def __init__(self, engine, cfg) -> None:
        self.eng = engine
        self.cfg = cfg
        self.tg: Optional[TelegramClient] = None
        self.relay: Optional[CriticalRelay] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.offset = 0
        self.applied = 0
        self.last_command = ""

    # ------------------------------------------------------------- plumbing

    async def notify(self, text: str) -> None:
        if self.tg is not None:
            await self.tg.send(text)

    def _drain_command_file(self):
        """One verb per line; the file is emptied as it is read.

        This is the path that still works when Telegram is unreachable --
        `echo flat > control.cmd` over SSH. Emptying it is what makes it
        safe to leave lying around: a verb is consumed once, so a stale file
        cannot re-flatten an engine that was restarted.
        """
        path = self.cfg.control_command_file
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r+", encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh.read().splitlines()]
                fh.seek(0)
                fh.truncate()
        except Exception as e:                                  # noqa: BLE001
            log.warning("command file unreadable: %r", e)
            return []
        return [ln for ln in lines if ln]

    # -------------------------------------------------------------- the run

    async def run(self, stop: asyncio.Event) -> None:
        # Its own HTTP session on purpose: a 25-second long poll must not
        # share a connector with the order path.
        self.session = aiohttp.ClientSession()
        try:
            token, chat = self.cfg.tg_bot_token, self.cfg.tg_chat_id
            if token and chat:
                self.tg = TelegramClient(self.session, token, chat)
                await self.notify(
                    f"engine up — {self.cfg.symbol} "
                    f"entropy/{self.cfg.hedge_venue}, mode {self.cfg.mode}. "
                    + HELP)
            else:
                log.warning("control: no ARB_TG_BOT_TOKEN/ARB_TG_CHAT_ID — "
                            "command file only (%s)",
                            self.cfg.control_command_file)
            while not stop.is_set():
                for verb in self._drain_command_file():
                    await self._handle(verb, "file")
                if self.relay is not None:
                    while self.relay.lines:
                        await self.notify("⚠ " + self.relay.lines.popleft())
                if self.tg is None:
                    try:
                        await asyncio.wait_for(stop.wait(),
                                               timeout=self.cfg.control_poll_sec)
                    except asyncio.TimeoutError:
                        pass
                    continue
                msgs, self.offset = await self.tg.poll(self.offset)
                for text in msgs:
                    await self._handle(text, "telegram")
        finally:
            await self.session.close()

    # ------------------------------------------------------------- the verbs

    async def _handle(self, text: str, source: str) -> None:
        verb = text.strip().lstrip("/").split()[0].lower() if text.strip() else ""
        if verb not in VERBS:
            log.info("control: ignoring %r from %s", text[:40], source)
            if verb and self.tg is not None and source == "telegram":
                await self.notify(f"unknown command {verb!r}. " + HELP)
            return
        self.last_command = verb
        self.applied += 1
        # Before: say what arrived, before acting on it. If the action then
        # hangs or crashes, the operator still knows it was received.
        log.critical("CONTROL: %s received via %s", verb, source)
        try:
            reply = await self._apply(verb)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001
            log.exception("control: %s failed", verb)
            reply = f"{verb} FAILED: {e!r} — check the engine log"
        # After: say what it did.
        log.critical("CONTROL: %s -> %s", verb, reply)
        await self.notify(f"{verb}: {reply}")

    async def _apply(self, verb: str) -> str:
        eng = self.eng
        if verb == "status":
            return eng.control_status()
        if verb == "pause":
            if eng.paused_by_operator:
                return "already paused (idempotent). Hedging and reconcile " \
                       "continue; use /resume to lift."
            eng.set_operator_pause(True, "operator")
            return ("paused — opening no new exposure. Hedging, flattening "
                    "and reconcile continue. Positions untouched.")
        if verb == "resume":
            if eng.halted:
                # A halt is one-way BY DESIGN: it needs a restart, because
                # the restart re-reads real positions with strict=True. A
                # message from a phone does not get to undo a kill switch.
                return ("REFUSED: the engine is HALTED, which /resume cannot "
                        "lift. Check both venues, then restart the process — "
                        "a restart re-reads real positions strictly.")
            if eng.flatten_request:
                return "REFUSED: a flatten is still in progress. Wait for it."
            if not eng.paused_by_operator:
                return "not paused (idempotent) — already trading."
            eng.set_operator_pause(False, "operator")
            return "resumed — opening new exposure again."
        if verb == "flat":
            if eng.flatten_request:
                return (f"already flattening (idempotent) — "
                        f"{eng.describe_positions()}")
            eng.request_flatten()
            return (f"flattening: positions are verified against the venues "
                    f"FIRST, then closed reduce-only. The engine stays "
                    f"paused afterwards — /resume to trade again. Now: "
                    f"{eng.describe_positions()}")
        return "unhandled"


def make_relay(fmt: Optional[logging.Formatter] = None) -> CriticalRelay:
    r = CriticalRelay()
    r.setFormatter(fmt or logging.Formatter("%(name)s: %(message)s"))
    return r
