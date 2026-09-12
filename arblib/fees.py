# -*- coding: utf-8 -*-
"""Venue fee table — the single source of truth for the arb line (2026-09-03).

Why this file exists: the whole §0.75 line turned on one number nobody had
written down. The recording family was described as "both legs 0 bps", which
turned out to be Entropy's referral rebate rather than a schedule, and every
capturable figure quoted before today implicitly assumed zero fees. A fee
belongs in one place, with its source and the date it was checked, so an
estimate can never quietly diverge from it.

THE DECISION RULE (used everywhere downstream):

    a trade enters at the band and closes when the premium has come back to
    HALF the band, so it captures band/2; it crosses BOTH legs on the way in
    and BOTH on the way out, so it pays 2 x (fee_a + fee_b).

        net_bps_per_trade = band/2 - 2 * (fee_a_eff + fee_b_eff)
        required_band     = 4 * (fee_a_eff + fee_b_eff)

    fee_eff = taker_bps * (1 - rebate). Rebates are the operator's own
    account terms, not public facts — they are stated as such below.

Verified 2026-09-03 unless marked ASSUMED. Re-check before any live trade:
a promotional zero is exactly the kind of number that expires quietly.
"""
from __future__ import annotations

# venue key -> (taker bps, rebate fraction, verified?, note)
VENUES: dict[str, dict] = {
    "HL": {
        "taker_bps": 4.5, "maker_bps": 1.5, "rebate": 0.0, "verified": True,
        "note": "Hyperliquid docs, base tier 0.045%. Volume tiers need $5M+/14d; "
                "HYPE staking discounts reach 40% only at 500,000 HYPE.",
    },
    # Every HIP-3 builder dex reads deployerFeeScale = 1.0 with growth mode,
    # i.e. the trader pays the HL schedule and the deployer keeps its share.
    # io's effective zero comes from Entropy's referral rebate, which is a
    # promotion, not a schedule — and it is the one number a small live fill
    # still has to confirm.
    "IO": {
        "taker_bps": 4.5, "maker_bps": 1.5, "rebate": 0.25, "verified": True,
        "note": "Operator's own Entropy referral page 2026-09-04 (referred by "
                "yourquantguy, TIER 1): Benefit 25% = the fee discount on "
                "one's OWN trades. The '100%' quoted from the upstream README "
                "was the REFERRER's share of referees' fees (income), never a "
                "trading discount -- two different numbers with one name. "
                "Rebate 60% on that page is what the operator would earn from "
                "people he refers; it does not touch his own cost.",
    },
    "xyz": {"taker_bps": 4.5, "maker_bps": 1.5, "rebate": 0.0, "verified": True,
            "note": "deployerFeeScale 1.0 + growth mode -> HL schedule."},
    "para": {"taker_bps": 4.5, "maker_bps": 1.5, "rebate": 0.0, "verified": True,
             "note": "deployerFeeScale 1.0 + growth mode -> HL schedule."},
    "mkts": {"taker_bps": 4.5, "maker_bps": 1.5, "rebate": 0.0, "verified": True,
             "note": "deployerFeeScale 1.0 + growth mode -> HL schedule."},
    "hyna": {"taker_bps": 4.5, "maker_bps": 1.5, "rebate": 0.0, "verified": True,
             "note": "deployerFeeScale 1.0 + growth mode -> HL schedule."},
    # ------------------------------------------------------------------ Lighter
    # 2026-09-12 CORRECTION. This entry said 0.0 / 0.0 "verified" from
    # 2026-09-03 until today. IT WAS WRONG, and the docstring at the top of
    # this file predicted exactly this failure ("a promotional zero is exactly
    # the kind of number that expires quietly"). It was not a promotion that
    # expired -- the account-tier layer was never checked at all.
    #
    # Lighter prices the ACCOUNT, not the market:
    #
    #   Standard   0 / 0 bps      cancel latency 300ms IMPOSED BY THE VENUE
    #   Premium    0.40 / 2.80    no added latency on cancels or post-only
    #              (that is the zero-staked-LIT row; staking 500k LIT takes it
    #               to 0.28 / 1.96)
    #   Plus       0.5 bps flat both sides, 300ms taker / 200ms cancel
    #
    # OUR ACCOUNT (index 743078) IS **PREMIUM**. Four independent sources agree:
    # the PREMIUM badge on app.lighter.xyz/portfolio; the order panel on BTC
    # with account data loaded; the same panel on LIT; and the Premium
    # zero-stake row in docs.lighter.xyz/trading/trading-fees, which matches
    # 0.028% / 0.004% to the digit.
    #
    # Two traps that each fooled a reading of this earlier today:
    #   (a) /api/v1/orderBooks reports taker_fee == maker_fee == "0.0000" for
    #       all 245 markets. That is the PROTOCOL-level market parameter, not
    #       what the account pays. Do not read venue cost off that field.
    #   (b) the order panel shows "0% | 0%" until the account data loads, so a
    #       rate-limited or logged-out page looks like a zero-fee venue.
    #
    # The tier is switchable (changeAccountTier; once per 24h, only with no
    # open orders or positions). Switching to Standard makes these numbers 0/0
    # and buys a 300ms cancel latency instead -- see TODO 1.31 for the open
    # question of what that latency is worth in bps. Until that is measured
    # and a switch is actually made, the line below is what we pay.
    "lighter": {"taker_bps": 2.80, "maker_bps": 0.40, "rebate": 0.0,
                "verified": True,
                "note": "PREMIUM tier, zero staked LIT (docs 2026-09-12 + "
                        "PREMIUM badge on our portfolio page + order panel on "
                        "BTC and LIT). Standard tier would be 0/0 but adds a "
                        "venue-imposed 300ms cancel latency. NOT a per-market "
                        "fee -- /api/v1/orderBooks' 0.0000 is the protocol "
                        "parameter, not the account's cost."},
    "lighter-rh": {"taker_bps": 2.80, "maker_bps": 0.40, "rebate": 0.0,
                   "verified": False,
                   "note": "ASSUMED = same schedule as mainnet Lighter. The "
                           "tier is per ACCOUNT and we hold no Robinhood-side "
                           "account, so this has never been checked -- the "
                           "Premium numbers are used because erring high is "
                           "the safe direction for a gate. Quotes in USDG, so "
                           "part of any premium is the stablecoin basis."},
    # ---------------------------------------------------------- Lighter, STANDARD
    # 2026-09-13, RECEIPT-VERIFIED from the public trade tape: Lighter encodes a
    # zero fee as an ABSENT field, not as 0. Across 53,784 regular fills the
    # accounts partition almost perfectly -- takers: 905 always carry the field,
    # 638 never do, only 2 mixed; makers: 268 / 96 / 0 mixed -- and no fill in
    # the "carries the field" group is ever 0.000 (0.50 Plus and the 1.96-2.80
    # Premium ladder are all present). So absent == Standard == 0/0, and it is
    # not theoretical: **74.9% of taker notional and 11.4% of maker notional
    # pays nothing.**
    #
    # The tier is an ACCOUNT property, not a market one, and it is exclusive:
    # Standard buys 0/0 at the price of a 300 ms venue-imposed cancel latency
    # and ~60 req/min. That makes it unusable for market making and irrelevant
    # to anything that rebalances hourly. Which tier applies is therefore a
    # STRATEGY decision -- see cost_model's --lighter-tier.
    "lighter-std": {"taker_bps": 0.0, "maker_bps": 0.0, "rebate": 0.0,
                    "verified": True,
                    "note": "Lighter STANDARD tier: 0 maker / 0 taker, "
                            "receipt-verified from the tape (74.9% of taker "
                            "notional pays nothing). Costs 300ms cancel "
                            "latency + ~60 req/min -- fine for slow "
                            "strategies, impossible for market making."},
    "lighter-rh-std": {"taker_bps": 0.0, "maker_bps": 0.0, "rebate": 0.0,
                       "verified": False,
                       "note": "ASSUMED same tier structure as mainnet "
                               "Lighter. We hold no Robinhood-chain account, "
                               "so this has never been checked."},
    "bitget": {
        "taker_bps": 6.0, "maker_bps": 2.0, "rebate": 0.50, "verified": True,
        "note": "Bitget publishes takerFeeRate per contract (0.0006 = 6 bps). "
                "Rebate 50% stated by the operator 2026-09-03.",
    },
    "binance": {
        "taker_bps": 5.0, "maker_bps": 2.0, "rebate": 0.0, "verified": False,
        "note": "Binance USDT-M standard tier 0.05%/0.02% (public schedule). "
                "No rebate stated by the operator. Lists equity perps as "
                "contractType TRADIFI_PERPETUAL (2026-09-04).",
    },
    "okx": {
        "taker_bps": 5.0, "maker_bps": 2.0, "rebate": 0.45, "verified": True,
        "note": "0.05%/0.02% is OKX's published standard-tier perp schedule. "
                "Rebate 45% CONFIRMED by the operator 2026-09-04 (account "
                "terms, not a promotion).",
    },
}

DEFAULT = {"taker_bps": 4.5, "maker_bps": 1.5, "rebate": 0.0, "verified": False,
           "note": "unknown venue - charged at the HL schedule to stay pessimistic"}


def fee_bps(venue: str, maker: bool = False, rebate: bool = True) -> float:
    """Effective cost of ONE crossing on this venue, in bps.

    rebate=False prices the SCHEDULE only. Every rebate here is account
    terms rather than a public fact (IO's 100% is explicitly UNCONFIRMED),
    so any figure a verdict rests on has to be showable both ways —
    otherwise a promotion that expires quietly takes the conclusion with
    it (2026-09-03: the §0.75 family's "both legs 0 bps" was exactly this).
    """
    v = VENUES.get(venue, DEFAULT)
    base = v["maker_bps"] if maker else v["taker_bps"]
    return base * (1.0 - (v["rebate"] if rebate else 0.0))


# Execution mode. 2026-09-03: the author of the recorder we run published his
# own account of this trade (edgeX, 60 days) and the single biggest difference
# from our model is that he rests orders instead of crossing -- "挂单便宜但不保证
# 成交，吃单一定成交但会滑点", with three documented patterns (rest A then cross
# B; rest A then try to rest B; rest both and cross whichever side is left).
# Crossing four times is the PESSIMISTIC bound, not the only way to trade, and
# on Bitget it is the difference between 6 bps and 2 bps per crossing.
MODES = {
    "taker_taker": "四次吃單（最保守；我們原本的假設）",
    "maker_taker": "一腿掛單、一腿吃單（作者的方式 A）",
    "maker_maker": "兩腿都掛單（作者的方式 C；不保證成交，未成交就切吃單）",
}


def round_trip_bps(leg_a: str, leg_b: str, mode: str = "taker_taker",
                   rebate: bool = True) -> float:
    """Both legs, in and out, under one execution mode."""
    if mode == "maker_maker":
        per_leg = (fee_bps(leg_a, True, rebate)
                   + fee_bps(leg_b, True, rebate))
    elif mode == "maker_taker":
        # rest on the cheaper-to-rest venue, cross the other
        per_leg = min(fee_bps(leg_a, True, rebate) + fee_bps(leg_b, False, rebate),
                      fee_bps(leg_a, False, rebate) + fee_bps(leg_b, True, rebate))
    else:
        per_leg = fee_bps(leg_a, False, rebate) + fee_bps(leg_b, False, rebate)
    return 2.0 * per_leg


def required_band_bps(leg_a: str, leg_b: str, mode: str = "taker_taker",
                      rebate: bool = True) -> float:
    """The band this pair needs before a trade breaks even."""
    return 2.0 * round_trip_bps(leg_a, leg_b, mode, rebate)


def net_per_trade_bps(band_bps: float, leg_a: str, leg_b: str,
                      mode: str = "taker_taker", rebate: bool = True) -> float:
    """What one round trip keeps: half the band, minus four crossings."""
    return band_bps / 2.0 - round_trip_bps(leg_a, leg_b, mode, rebate)


def unverified(leg_a: str, leg_b: str) -> list[str]:
    """Legs whose fee is assumed rather than confirmed — the board must say so."""
    return [v for v in (leg_a, leg_b)
            if not VENUES.get(v, DEFAULT).get("verified", False)]


def table() -> list[dict]:
    """Public-safe fee table for the site (rates are percentages, not dollars)."""
    return [{"venue": k, "taker_bps": v["taker_bps"],
             "rebate_pct": round(v["rebate"] * 100),
             "effective_bps": round(fee_bps(k), 2),
             "maker_bps": v["maker_bps"],
             "effective_maker_bps": round(fee_bps(k, True), 2),
             "verified": v["verified"], "note": v["note"]}
            for k, v in VENUES.items()]


if __name__ == "__main__":  # quick reference: what each combination needs
    import itertools
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"{'配對組合':<26}{'單腿費(bps)':>14}{'來回成本':>10}{'需要的帶':>10}")
    for a, b in itertools.combinations(VENUES, 2):
        print(f"{a + ' x ' + b:<26}{fee_bps(a):>6.2f}+{fee_bps(b):<7.2f}"
              f"{round_trip_bps(a, b):>10.2f}{required_band_bps(a, b):>10.2f}")
