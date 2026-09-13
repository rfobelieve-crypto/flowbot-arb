"""Every attribute engine.py reads off a venue must exist on BOTH venues.

Why this exists (2026-09-13)
---------------------------------------------------------------------------
The first `--shadow` rehearsal of B3's maker path died on its first
evaluation with

    AttributeError: 'LighterVenue' object has no attribute 'maker_fee_bps'

`maker_fee_bps` lived on `VenueConf` and on neither venue class, so
`mode: maker` was broken on *both* legs -- the whole maker path had been
written, reviewed and unit-tested at the `plan_maker` / `maker.py` level and
never once driven through `_scan_maker` with real venue objects.

Nothing caught it because the engine tests use a StubVenue that defines
whatever the test needs. That is the same shape as flow_system's facade-skip
bug, which recurred three times before someone wrote an AST test instead of
trying to remember (mistake.md 2026-06-17: "不能靠記得改 facade，靠 test
強制"). This is that test for this boundary.

It is deliberately dumb: it does not know what any attribute means, only that
the engine reads it and therefore both implementations must have it. A venue
that legitimately cannot provide one must be added to EXEMPT *with a reason*,
which makes the exception visible instead of silent.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import (HLCreds, LighterCreds,  # noqa: E402
                                LighterProfile, VenueConf)
from entropy_arb.venue_hl import HLVenue  # noqa: E402
from entropy_arb.venue_lighter import LighterVenue  # noqa: E402

ENGINE_PY = os.path.join(os.path.dirname(__file__), "..", "entropy_arb",
                         "engine.py")

# Identifiers in engine.py that hold a venue. Keeping this list explicit (and
# short) is what makes the scan meaningful: widen it and the test starts
# demanding attributes of things that are not venues.
VENUE_NAMES = {"v", "buy", "sell", "maker_v", "taker_v", "buy_v", "sell_v",
               "venue", "other"}

# Attributes the engine sets ON a venue rather than reading from it, plus the
# two methods only one kind has. Each needs a reason.
EXEMPT = {
    # engine writes these back onto the venue as bookkeeping
    "position", "cash", "volume_usd", "equity", "free", "start_equity",
    "last_traded_ts", "exposure",
    # HL-only: nonce sharing between two legs on one HL account
    "share_nonces_with",
    # HL-only: the signer object; the engine guards it with `kind == "hl"`
    "account",
    # HL-only, and read only inside a branch that now checks BOTH legs'
    # `kind == "hl"` first. The order used to check only the hedge, which
    # would have raised on an entropy=lighter + hedge=tradexyz config; fixed
    # 2026-09-13 when this test surfaced them. `kind` itself is on both.
    "_query_address", "include_core_equity",
}


def venue_attr_reads() -> set:
    with open(ENGINE_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        val = node.value
        # v.attr  /  self.entropy.attr  /  self.hedge.attr
        if isinstance(val, ast.Name) and val.id in VENUE_NAMES:
            out.add(node.attr)
        elif (isinstance(val, ast.Attribute)
              and val.attr in ("entropy", "hedge")
              and isinstance(val.value, ast.Name) and val.value.id == "self"):
            out.add(node.attr)
    return out - EXEMPT


def _hl() -> HLVenue:
    conf = VenueConf(key="entropy", kind="hl", label="HL", symbol="GMX",
                     fee_bps=4.5, maker_fee_bps=1.5, cap_usd=100.0,
                     orders_per_min=30, hl_dex="",
                     hl_creds=HLCreds(private_key=None, account_address=None))
    return HLVenue(conf, "https://x", "wss://x", None, 10.0)


def _lighter() -> LighterVenue:
    conf = VenueConf(key="hedge", kind="lighter", label="LIGHTER", symbol="GMX",
                     fee_bps=2.8, maker_fee_bps=0.4, cap_usd=100.0,
                     orders_per_min=60,
                     lighter_profile=LighterProfile("mainnet", "https://x",
                                                    "wss://x", 304),
                     lighter_creds=LighterCreds(account_index=1,
                                                api_key_index=0,
                                                api_private_key=None),
                     lighter_venue="lighter")
    return LighterVenue(conf, None, 10.0)


def test_the_scan_finds_something():
    """A scan that matches nothing would pass forever (2026-09-12: a guard
    whose own reach is untested is not a guard)."""
    reads = venue_attr_reads()
    assert len(reads) > 15, reads
    assert "maker_fee_bps" in reads      # the attribute that started this
    assert "book" in reads and "cap_usd" in reads


@pytest.mark.parametrize("make", [_hl, _lighter], ids=["hl", "lighter"])
def test_venue_has_every_attribute_the_engine_reads(make):
    v = make()
    missing = sorted(a for a in venue_attr_reads() if not hasattr(v, a))
    assert not missing, (
        "%s is missing %s — engine.py reads these off a venue. Add them to "
        "the venue class, or to EXEMPT with a reason."
        % (type(v).__name__, missing))


def test_maker_fee_comes_from_the_config_not_the_taker_fee():
    """Silently falling back to the taker fee would make a losing quote look
    profitable -- VenueConf's own comment says the maker fee defaults to the
    taker fee and never to zero, so the venue must carry the real value."""
    assert _hl().maker_fee_bps == 1.5
    assert _lighter().maker_fee_bps == 0.4


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
