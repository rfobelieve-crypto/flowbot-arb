"""HLVenue accounting rules that need no network.

The one under test here is which USDC bucket a leg is allowed to call its
own: HIP-3 dexes are funded separately from Hyperliquid core, so a leg that
trades `io` may not count money parked in core toward its equity.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import VenueConf                 # noqa: E402
from entropy_arb.venue_hl import HLVenue                 # noqa: E402


def venue(dex):
    conf = VenueConf(key="entropy", kind="hl", label="X", symbol="NBIS",
                     fee_bps=4.5, maker_fee_bps=1.5, cap_usd=40.0,
                     orders_per_min=120, hl_dex=dex)
    return HLVenue(conf, "https://api.hyperliquid.xyz", "wss://x", None, 5.0)


def test_hip3_leg_counts_only_its_own_clearinghouse():
    """USDC in core cannot margin an io position until it is transferred in,
    so counting it would overstate what this leg can actually trade."""
    assert venue("io").include_core_equity is False
    assert venue("xyz").include_core_equity is False


def test_core_leg_counts_core():
    assert venue("").include_core_equity is True


def test_shared_account_still_counts_core_once():
    a, b = venue(""), venue("xyz")
    assert a.include_core_equity is True
    b.include_core_equity = False          # what _run_inner does on a shared account
    assert [v.include_core_equity for v in (a, b)].count(True) == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:52s} OK")
