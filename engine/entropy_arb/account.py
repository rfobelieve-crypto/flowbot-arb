"""Account-wide exposure, read from the venue itself (2026-09-13).

Why this file exists
-------------------------------------------------------------------------
`VenueConf.cap_usd` is per venue. `Config.max_gross_usd` is the backstop one
level up -- its own comment says so: "cap_usd is PER VENUE, so a config typo
that scales size by 10x passes every per-venue check."

**Both of those are per PROCESS.** One engine process trades one symbol and
holds at most one resting quote (`_scan_maker`: `if self._maker_open: return`),
so running five markets means five processes -- and they share one Lighter
account and one Hyperliquid account. Five processes each honouring a $1,000
ceiling can put $5,000 on one account, and no check in this engine sees it:
every guard is computed from `self.venues`, which is this process's two legs.

That is the same shape flow_system has paid for twice (CLAUDE.md, the two
manual blow-ups): a kill switch that cannot tell whose loss it is looking at.
The difference here is that the other party is another copy of us.

The ledger is the exchange
-------------------------------------------------------------------------
There is no shared file, no lock, no IPC. `fetch_position()` on both venues
already downloads the **whole account** and then filters to one market:

    HL        clearinghouseState -> marginSummary.totalNtlPos   (all markets)
    Lighter   /api/v1/account    -> positions[].position_value  (all markets)

So the account-wide number costs **zero extra API calls** -- it is in the
response we already paid for, and it is exchange truth, which is the only
thing this engine is allowed to clear local state with (`maker.py` rule 1).
`fetch_position()` records one of these on the venue as `venue.exposure`;
nothing else in the engine fetches anything new.

Unit of account
-------------------------------------------------------------------------
`account_id` is the thing that shares margin, not the login:

    HL       address + HIP-3 dex  -- HIP-3 clearinghouses are funded
                                    separately (venue_hl.include_core_equity
                                    documents this), so core and `io` are two
                                    accounts for margin purposes
    Lighter  deployment + index   -- mainnet and the Robinhood chain are
                                    different chains and different accounts

Two legs that report the same `account_id` are genuinely one pool; checking
the same cap twice against it is idempotent, so no special case is needed.
The id exists so a breach says *which* pool, and so `tools/account_budget.py`
can group the configs that share one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AccountSnapshot:
    """What the venue says about the WHOLE account, at one instant.

    `mine_usd` is the part of `gross_usd` that belongs to this process's
    symbol. It is not used by any cap -- it is there so a breach can be
    attributed ("$1,800 gross, $240 of it mine") instead of being reported as
    if we caused it. Acting on the other $1,560 is not this process's
    business: that is the `admin_heal` mistake (zeroing state you do not own).
    """

    account_id: str
    ts: float
    gross_usd: float
    free_usd: float
    equity_usd: float
    markets: int
    mine_usd: float

    @property
    def others_usd(self) -> float:
        return max(self.gross_usd - self.mine_usd, 0.0)

    def age_sec(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.ts

    def describe(self) -> str:
        return (f"{self.account_id}: gross ${self.gross_usd:,.2f} "
                f"({self.markets} mkt, mine ${self.mine_usd:,.2f}, "
                f"others ${self.others_usd:,.2f}) | free "
                f"${self.free_usd:,.2f} | equity ${self.equity_usd:,.2f}")


def hl_snapshot(state: dict, *, account_id: str, coin: str) -> AccountSnapshot:
    """Build a snapshot from one `clearinghouseState` response.

    `marginSummary.totalNtlPos` is Hyperliquid's own account-wide notional --
    verified present on a live account 2026-09-13 alongside accountValue,
    totalMarginUsed and totalRawUsd. We do not re-derive it from the position
    list: a venue's own aggregate is the number its own margin engine uses.
    """
    ms = state.get("marginSummary") or {}
    mine = 0.0
    markets = 0
    for ap in state.get("assetPositions") or []:
        p = ap.get("position") or {}
        try:
            if float(p.get("szi") or 0.0) == 0.0:
                continue
        except (TypeError, ValueError):
            continue
        markets += 1
        if p.get("coin") == coin:
            mine += abs(float(p.get("positionValue") or 0.0))
    return AccountSnapshot(
        account_id=account_id,
        ts=time.time(),
        gross_usd=abs(float(ms.get("totalNtlPos") or 0.0)),
        free_usd=float(state.get("withdrawable") or 0.0),
        equity_usd=float(ms.get("accountValue") or 0.0),
        markets=markets,
        mine_usd=mine,
    )


def lighter_snapshot(acct: dict, *, account_id: str,
                     market_id: int) -> AccountSnapshot:
    """Build a snapshot from one `/api/v1/account` response.

    Lighter has no account-wide notional field, so this sums
    `positions[].position_value`. That value is SIGNED (a live account showed
    `-0.000000`), hence `abs` per leg before summing -- summing signed values
    would net a long against a short on a different market and report a hedged
    book as flat, which is the opposite of what a gross cap is for.
    """
    gross = mine = 0.0
    markets = 0
    for p in acct.get("positions") or []:
        try:
            if float(p.get("position") or 0.0) == 0.0:
                continue
            val = abs(float(p.get("position_value") or 0.0))
        except (TypeError, ValueError):
            continue
        markets += 1
        gross += val
        try:
            if int(p.get("market_id", -1)) == market_id:
                mine += val
        except (TypeError, ValueError):
            pass
    return AccountSnapshot(
        account_id=account_id,
        ts=time.time(),
        gross_usd=gross,
        free_usd=float(acct.get("available_balance") or 0.0),
        equity_usd=float(acct.get("total_asset_value") or 0.0),
        markets=markets,
        mine_usd=mine,
    )
