"""B8: the two instrument defects in the scanner's pairing rules.

Both were found by reading the scan output rather than the code (flow_system
TODO 1.03), and both inflate the ranking with numbers nobody can trade. The
ranking is what decides which battleground to record next, so a fake pair is
not cosmetic -- it costs a week of recording.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from scanner import CANON, pair_up                        # noqa: E402


def leg(sym):
    return {"kind": "cex", "sym": sym, "vol24": 1e6, "created_at": ""}


def names(pairs):
    return sorted(p["pair"] for p in pairs)


def test_same_venue_is_not_a_pair():
    """Bitget lists XAUT and PAXG. Before B8 both canonicalised to GOLD, so
    the scanner paired the venue with itself and reported GOLD@bitget-bitget
    -- a spread with no second book to hedge on, which config.py itself
    refuses to load."""
    venues = {"bitget": {"XAUT": leg("XAUT"), "PAXG": leg("PAXG")}}
    pairs, _ = pair_up(venues)
    assert pairs == [], f"same-venue pair survived: {names(pairs)}"


def test_gold_issuers_are_separate_assets():
    """XAUT vs PAXG measures the ISSUER basis (two custodians' credit and
    redemption frictions), not a venue basis. Aliasing them to one asset does
    not test that hypothesis, it assumes it."""
    assert CANON["XAUT"] != CANON["PAXG"]
    assert CANON["XAU"] not in (CANON["XAUT"], CANON["PAXG"])
    venues = {"bitget": {"XAUT": leg("XAUT")},
              "okx": {"PAXG": leg("PAXG")}}
    pairs, _ = pair_up(venues)
    # different assets -> no pair at all, rather than a fake GOLD pair
    assert pairs == [], f"cross-issuer pair still merged: {names(pairs)}"


def test_the_same_issuer_across_venues_still_pairs():
    """The fix must not throw away the real pairs."""
    venues = {"bitget": {"XAUT": leg("XAUT")},
              "okx": {"XAUT": leg("XAUT")}}
    pairs, _ = pair_up(venues)
    assert names(pairs) == ["GOLD_XAUT@bitget-okx"]


def test_real_aliases_still_work():
    venues = {"io": {"OAI": leg("io:OAI")},
              "lighter": {"OPENAI": leg("OPENAI")}}
    pairs, _ = pair_up(venues)
    assert names(pairs) == ["OAI@IO-lighter"]


def test_three_venues_give_three_pairs():
    venues = {"io": {"NBIS": leg("io:NBIS")},
              "lighter": {"NBIS": leg("NBIS")},
              "bitget": {"NBIS": leg("NBIS")}}
    pairs, _ = pair_up(venues)
    assert len(pairs) == 3
    assert all(p["leg_a"] != p["leg_b"] for p in pairs)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:52s} OK")
