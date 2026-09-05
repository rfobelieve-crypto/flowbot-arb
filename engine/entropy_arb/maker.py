"""Resting post-only order state (B3, 2026-09-04).

The taker path never needed this file. An IOC order is alive for one round
trip and its outcome arrives in the same response. A **maker** order is alive
for seconds, and for every one of those seconds the exchange -- not this
process -- owns the truth about it.

Each rule below exists because Hummingbot's XEMM executor breaks it
(`docs/PEER_INFRA.md` S3, source read 2026-09-04), and because S7 of that
document makes all three mandatory:

1. **Local state is cleared by exchange TRUTH, never by our own ACTION.**
   `request_cancel()` moves the order to `cancelling`; it does not forget it.
   XEMM assigns `maker_order = None` on the line after `cancel(...)`, so an
   order that fills while the cancel is in flight reaches a handler that no
   longer recognises it -- and the hedge leg is never sent. The naked fill
   leaves no trace. Here, only `apply()` -- fed exclusively by exchange
   reports -- can make an order terminal.

2. **Fills are consumed incrementally.** `unhedged` is what has filled but
   not yet been hedged, and it is meaningful long before the order completes.
   XEMM v2 reacts only to OrderCompletedEvent (zero fill-event references in
   the whole file), so half a fill is half a naked position nothing looks at.

3. **An unconfirmed cancel is PESSIMISTIC.** A cancel that misses its own
   budget sends the order to `unknown`, which means "assume it may have
   filled", not "assume it is gone". `unknown` is not terminal: the owner
   keeps trying, exactly like `_self_rescue` never permanently gives up.

The class holds no I/O and no clock of its own -- callers pass `now` -- so
every rule above is testable without a venue or a network.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("maker")

# ---------------------------------------------------------------- lifecycle
NEW = "new"                # send issued; the exchange has not confirmed it
RESTING = "resting"        # exchange says it is on the book
CANCELLING = "cancelling"  # cancel issued, NOT confirmed -- still OUR order
DONE = "done"              # exchange-confirmed terminal (filled or canceled)
UNKNOWN = "unknown"        # cancel budget blew: treat as possibly filled

# Status strings that mean "off the book for good". Lighter enumerates its
# whole set in the Order model (status enum, verified against the SDK
# 2026-09-04) and every cancel reason there is prefixed "canceled-";
# Hyperliquid's orderStatus says "open" while resting and one of these once
# it is not.
#
# Compared CASE-INSENSITIVELY (2026-09-05). Today HL sends lower/camelCase
# and Lighter sends lowercase, so an exact match happens to work -- but the
# failure mode if that ever changes is the worst one available here: an
# unrecognised status is (correctly) not terminal, so the order sits in
# `unknown` forever, and an order in `unknown` blocks every future quote.
# A venue changing "filled" to "FILLED" would stop the engine dead, quietly.
# perp-dex-tools carries per-venue status maps for exactly this reason and
# handles both CANCELED and CANCELLED spellings (exchanges/extended.py:654,
# exchanges/grvt.py:172-174).
TERMINAL_STATUSES = frozenset(x.lower() for x in {
    "filled", "canceled", "cancelled", "rejected", "expired",
    "marginCanceled", "vaultWithdrawalCanceled", "openInterestCapCanceled",
    "selfTradeCanceled", "reduceOnlyCanceled", "siblingFilledCanceled",
    "delistedCanceled", "liquidatedCanceled", "scheduledCancel",
})
OPEN_STATUSES = frozenset({"open", "resting", "in-progress", "pending",
                           "new", "partially_filled", "partially-filled"})


# A cancel reason is not just a label. Lighter enumerates ten of them and
# Hyperliquid nine, and they split into two groups that deserve completely
# different reactions:
#
#   market  — the book moved, we would have crossed, the slice was too big.
#             Routine. Quote again.
#   account — margin, balance, position-not-allowed, reduce-only-would-have-
#             increased, liquidated, delisted. **The venue is telling us
#             something about the ACCOUNT**, and quoting again just collects
#             the same rejection forever while the fill rate quietly reads
#             zero.
#
# The taker path already separates these (`"margin" in status -> pause the
# venue`, engine.py:724). The maker path did not: every terminal status was
# counted as one routine cancel. Found 2026-09-05 while reading
# perp-dex-tools, whose OrderInfo carries a `cancel_reason` field we had no
# equivalent for.
ACCOUNT_CANCEL_MARKERS = ("margin", "balance", "liquidat", "delisted",
                          "position-not-allowed", "reduce-only",
                          "reduceonly", "insufficient")


def is_account_cancel(status: str) -> bool:
    """True when the venue cancelled us for a reason about the ACCOUNT."""
    s = _norm(status)
    return any(m in s for m in ACCOUNT_CANCEL_MARKERS)


def _norm(status: str) -> str:
    return (status or "").strip().lower()


def is_terminal_status(status: str) -> bool:
    """True when the exchange says this order can never fill again.

    An unrecognised string is NOT terminal. Retiring an order on a status we
    do not understand is the optimistic assumption this file exists to
    forbid -- but see the note above TERMINAL_STATUSES for why the matching
    is case-insensitive rather than exact.
    """
    s = _norm(status)
    if not s or s in OPEN_STATUSES:
        return False
    return (s in TERMINAL_STATUSES or s.startswith("canceled")
            or s.startswith("cancelled"))


@dataclass
class MakerOrder:
    """One resting post-only order: a SHADOW of exchange state, never a
    prediction of it."""

    venue_key: str
    is_buy: bool
    qty: float                    # base size originally posted
    px: float                     # post-only limit price
    sent_ts: float
    handle: object = None         # venue-local id (HL cloid / Lighter coi)
    state: str = NEW
    filled_base: float = 0.0      # exchange-reported, monotonic
    applied_base: float = 0.0     # of the above, booked into venue.position
    hedged_base: float = 0.0      # of the above, dispatched to the other leg
    avg_px: Optional[float] = None
    status: str = ""              # last status string the exchange gave us
    cancel_ts: Optional[float] = None   # when the FIRST cancel went out
    cancel_attempts: int = 0
    note: str = ""                # why it is in `unknown`, for log + status
    seen: list = field(default_factory=list, repr=False)
    # measurement bookkeeping for LIVE_50U_SPEC M2/M3/M4 (fill rate, adverse
    # selection, two-leg latency). Written by the engine, never read by logic.
    stats: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------ accessors

    @property
    def unapplied(self) -> float:
        """Filled but not yet booked into the venue's position. Separate
        from `unhedged` because the two advance at different times: a fill is
        a POSITION the instant the exchange reports it, but it may be too
        small to hedge until later fills accumulate past the other venue's
        minimum order size. Merging them would either mis-state the position
        or fire a hedge the venue would reject."""
        return max(self.filled_base - self.applied_base, 0.0)

    @property
    def unhedged(self) -> float:
        """Filled but not yet acted on by the other leg. Must #2: this is the
        number the engine hedges, and it is live from the first partial
        fill."""
        return max(self.filled_base - self.hedged_base, 0.0)

    @property
    def residual(self) -> float:
        """Size we still believe is on the book."""
        return max(self.qty - self.filled_base, 0.0)

    @property
    def is_terminal(self) -> bool:
        return self.state == DONE

    @property
    def needs_attention(self) -> bool:
        """True while this order can still surprise us -- anything that is
        not an exchange-confirmed end state. `unknown` counts: that is the
        entire point of it."""
        return self.state != DONE

    @property
    def fill_px(self) -> float:
        """Price to book fills at. A resting order fills AT ITS OWN price --
        the taker crosses to us -- so the limit price is exact, not an
        estimate, whenever the venue reports no average."""
        return self.avg_px if self.avg_px else self.px

    # -------------------------------------------------------- state changes

    def on_handle(self, handle) -> None:
        """Record the venue-local id. Venues allocate it BEFORE the network
        call and return it even when the send fails, because a send that
        timed out may still have placed an order we now have to cancel."""
        self.handle = handle

    def apply(self, status: str, filled_base: Optional[float] = None,
              avg_px: Optional[float] = None) -> float:
        """Ingest one exchange report -- the ONLY way state moves forward.

        Returns the newly observed fill (>= 0). Fills are monotonic: a report
        showing less than we already know is a stale frame arriving out of
        order, and it is ignored rather than believed. Believing it would
        make an already-hedged fill look unhedged and buy the hedge twice.
        """
        gained = 0.0
        if filled_base is not None:
            f = max(float(filled_base), 0.0)
            if f > self.filled_base + 1e-15:
                gained = f - self.filled_base
                self.filled_base = f
            elif f < self.filled_base - 1e-12:
                log.debug("[%s] stale fill frame ignored: %.8g < %.8g",
                          self.venue_key, f, self.filled_base)
        if avg_px:
            self.avg_px = float(avg_px)
        if status:
            self.status = status
            self.seen.append(status)
        if is_terminal_status(status):
            # Exchange truth, and the only path to DONE. Reachable from
            # cancelling and from unknown alike: an order we had lost track
            # of is resolved the moment the exchange says what happened.
            self.state = DONE
        elif _norm(status) in OPEN_STATUSES and self.state == NEW:
            self.state = RESTING
        return gained

    def mark_applied(self, qty: float) -> None:
        self.applied_base = min(self.filled_base,
                                self.applied_base + max(qty, 0.0))

    def mark_hedged(self, qty: float) -> None:
        self.hedged_base = min(self.filled_base,
                               self.hedged_base + max(qty, 0.0))

    def request_cancel(self, now: float) -> None:
        """Must #1: a cancel records our INTENT, not our knowledge. Nothing
        is cleared here -- not the handle, not the fills, not the order."""
        if self.state == DONE:
            return
        if self.cancel_ts is None:
            self.cancel_ts = now
        self.cancel_attempts += 1
        if self.state != UNKNOWN:
            self.state = CANCELLING

    def cancel_overdue(self, now: float, budget_sec: float) -> bool:
        """Must #3: the cancel has its OWN budget, separate from
        staleness_sec and from the quote's own resting lifetime."""
        return (self.cancel_ts is not None
                and now - self.cancel_ts > budget_sec)

    def to_unknown(self, reason: str) -> None:
        """Must #3, pessimistic branch: unconfirmed means POSSIBLY FILLED.

        Not terminal. The owner keeps cancelling and keeps polling; a later
        exchange report still resolves it through apply().
        """
        if self.state == DONE:
            return
        self.state = UNKNOWN
        self.note = reason

    def describe(self) -> str:
        return (f"{self.venue_key} {'BUY' if self.is_buy else 'SELL'} "
                f"{self.qty:.6g}@{self.px:.6g} [{self.state}"
                + (f"/{self.status}" if self.status else "")
                + f"] filled {self.filled_base:.6g} "
                f"hedged {self.hedged_base:.6g}"
                + (f" ({self.note})" if self.note else ""))
