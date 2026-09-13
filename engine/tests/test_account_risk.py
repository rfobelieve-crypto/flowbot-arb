"""B6: the account-level ceiling, and the launch-time sum of per-process ones.

Why these tests exist
---------------------------------------------------------------------------
Every risk switch in this engine is computed from `self.venues` -- this
process's two legs. One process trades one symbol and holds at most one
resting quote, so N markets means N processes sharing one account, and N
processes each passing every check can still overload it.

Each test below pairs a "it binds" case with a "it does not bind" case,
because a guard that can only be seen failing has not been shown to measure
anything (mistake.md 2026-09-03: a guard must be seen green once and red
once). The default configuration has both account switches at 0, so the
first test pins that B6 changed nothing for anyone who does not set them.

Run:  python -m pytest tests/test_account_risk.py -q
"""
import asyncio
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.account import (AccountSnapshot, hl_snapshot,  # noqa: E402
                                 lighter_snapshot)
from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from tools import account_budget as ab  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


# --------------------------------------------------------------- scaffolding

class StubVenue:
    def __init__(self, key, label, cap=10_000.0):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps, self.maker_fee_bps = cap, 0.0, 0.0
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash = 0.0, 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.exposure = None
        self.book = OrderBook()

    def ready_to_trade(self):
        return True

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])


def make_engine(risk: str = "", legs: str = "") -> Engine:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("thresholds:\n  midline_bps: 5.0\n  upper_bps: 4.0\n"
            "  lower_bps: 3.0\nexecution:\n  premium_persist_sec: 0.0\n"
            + legs + (("risk:\n" + risk) if risk else ""))
    f.close()
    cfg = load_config(f.name, NO_ENV, symbol="SNDK", hedge_venue="lighter-rh")
    eng = Engine(cfg)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def snap(gross, free=10_000.0, *, mine=0.0, age=0.0, aid="lighter:mainnet:1"):
    return AccountSnapshot(account_id=aid, ts=time.time() - age,
                           gross_usd=gross, free_usd=free,
                           equity_usd=free, markets=3, mine_usd=mine)


# ------------------------------------------------------------- A: headroom

def test_default_config_leaves_headroom_untouched():
    """B6 off by default: headroom is exactly the pre-B6 per-process value."""
    eng = make_engine()
    assert eng.cfg.max_account_gross_usd == 0.0
    assert eng.cfg.min_account_free_usd == 0.0
    eng.entropy.position = 1.0
    eng.hedge.position = -1.0
    # Both venues have exposure=None, which would fail closed IF a cap were
    # set. With none set it must be ignored entirely.
    assert eng._headroom(eng.entropy, eng.hedge, 100.0) == pytest.approx(
        min(10_000.0 - 100.0, 10_000.0 - 100.0))


def test_account_gross_cap_shrinks_headroom():
    eng = make_engine("  max_account_gross_usd: 2000\n")
    eng.entropy.exposure = snap(1_500.0, mine=200.0)
    eng.hedge.exposure = snap(100.0, aid="hl:0xabc:core")
    # entropy's pool has $500 left; the per-process caps would allow $10,000.
    assert eng._headroom(eng.entropy, eng.hedge, 100.0) == pytest.approx(500.0)


def test_account_cap_not_binding_when_pool_is_empty():
    """The other half of the previous test: same cap, empty account, no bind."""
    eng = make_engine("  max_account_gross_usd: 2000\n")
    eng.entropy.exposure = snap(0.0)
    eng.hedge.exposure = snap(0.0, aid="hl:0xabc:core")
    assert eng._headroom(eng.entropy, eng.hedge, 100.0) == pytest.approx(
        2_000.0)   # the account cap, not the $10,000 per-process cap


def test_unknown_account_fails_closed():
    """exposure=None with a cap set must block, not wave through."""
    eng = make_engine("  max_account_gross_usd: 2000\n")
    eng.entropy.exposure = None
    eng.hedge.exposure = snap(0.0, aid="hl:0xabc:core")
    assert eng._headroom(eng.entropy, eng.hedge, 100.0) == 0.0


def test_stale_account_snapshot_fails_closed():
    eng = make_engine("  max_account_gross_usd: 2000\n")
    old = eng.ACCOUNT_STALE_MULT * eng.cfg.reconcile_sec + 1.0
    eng.entropy.exposure = snap(0.0, age=old)
    eng.hedge.exposure = snap(0.0, aid="hl:0xabc:core")
    assert eng._headroom(eng.entropy, eng.hedge, 100.0) == 0.0
    # ... and fresh again the moment it refreshes
    eng.entropy.exposure = snap(0.0, age=0.0)
    assert eng._headroom(eng.entropy, eng.hedge, 100.0) > 0.0


def test_free_collateral_floor_blocks_opening():
    eng = make_engine("  min_account_free_usd: 50\n")
    eng.entropy.exposure = snap(0.0, free=49.0)
    eng.hedge.exposure = snap(0.0, free=10_000.0, aid="hl:0xabc:core")
    assert eng._headroom(eng.entropy, eng.hedge, 100.0) == 0.0
    eng.entropy.exposure = snap(0.0, free=51.0)
    assert eng._headroom(eng.entropy, eng.hedge, 100.0) > 0.0


# ------------------------------------------------------------------ A: halt

def _run_hedge(eng):
    asyncio.get_event_loop().run_until_complete(eng._maybe_hedge())


def test_account_gross_over_cap_halts_and_names_the_other_share():
    eng = make_engine("  max_account_gross_usd: 1000\n")
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(100.0, 100.1)
    # Our own legs are flat, so every pre-B6 check passes: _gross_usd()==0,
    # net==0. The account is over anyway -- that is the whole point.
    eng.entropy.exposure = snap(5_000.0, mine=0.0)
    eng.hedge.exposure = snap(0.0, aid="hl:0xabc:core")
    _run_hedge(eng)
    assert eng.halted


def test_same_state_does_not_halt_without_the_cap():
    """Reverse proof: only the new switch makes the previous test fail."""
    eng = make_engine()
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(100.0, 100.1)
    eng.entropy.exposure = snap(5_000.0)
    eng.hedge.exposure = snap(0.0, aid="hl:0xabc:core")
    _run_hedge(eng)
    assert not eng.halted


def test_account_under_cap_does_not_halt():
    eng = make_engine("  max_account_gross_usd: 1000\n")
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(100.0, 100.1)
    eng.entropy.exposure = snap(999.0)
    eng.hedge.exposure = snap(0.0, aid="hl:0xabc:core")
    _run_hedge(eng)
    assert not eng.halted


# ---------------------------------------------------------------- A: config

def test_account_cap_below_a_legs_own_cap_is_refused():
    """Units: the account ceiling is compared against ONE venue's gross, so
    what must fit inside it is that leg's max_position_usd -- not
    max_gross_usd, which sums two legs sitting on two separate collateral
    pools."""
    with pytest.raises(ConfigError) as e:
        make_engine("  max_account_gross_usd: 100\n",
                    legs="entropy:\n  max_position_usd: 500\n")
    assert "below a leg" in str(e.value)
    # Two legs summing ABOVE the ceiling is legal: they are two accounts.
    eng = make_engine("  max_gross_usd: 800\n  max_account_gross_usd: 500\n",
                      legs="entropy:\n  max_position_usd: 400\n"
                           "hedge:\n  max_position_usd: 400\n")
    assert eng.cfg.max_account_gross_usd == 500.0


def test_live_requires_both_account_switches():
    keys = {k for k, _ in Engine.REQUIRED_RISK}
    assert "max_account_gross_usd" in keys
    assert "min_account_free_usd" in keys


# ------------------------------------------------------------- P: snapshots

def test_hl_snapshot_uses_the_venues_own_aggregate():
    st = {"marginSummary": {"accountValue": "1000", "totalNtlPos": "2500"},
          "withdrawable": "400",
          "assetPositions": [
              {"position": {"coin": "GMX", "szi": "2", "positionValue": "900"}},
              {"position": {"coin": "ETH", "szi": "-1", "positionValue": "1600"}},
              {"position": {"coin": "BTC", "szi": "0", "positionValue": "0"}}]}
    s = hl_snapshot(st, account_id="hl:0xa:core", coin="GMX")
    assert s.gross_usd == 2500.0      # not re-derived from the list
    assert s.free_usd == 400.0
    assert s.markets == 2             # the zero-size leg is not a market
    assert s.mine_usd == 900.0
    assert s.others_usd == 1600.0


def test_lighter_snapshot_does_not_net_longs_against_shorts():
    """position_value is SIGNED. Summing it signed would report a book that
    is long one market and short another as nearly flat -- the opposite of
    what a gross cap is for."""
    acct = {"available_balance": "18.89", "total_asset_value": "18.89",
            "positions": [
                {"market_id": 1, "position": "5", "position_value": "1200"},
                {"market_id": 2, "position": "-3", "position_value": "-1100"},
                {"market_id": 3, "position": "0", "position_value": "0"}]}
    s = lighter_snapshot(acct, account_id="mainnet:743078", market_id=1)
    assert s.gross_usd == pytest.approx(2300.0)   # NOT 100.0
    assert s.markets == 2
    assert s.mine_usd == pytest.approx(1200.0)
    assert s.free_usd == pytest.approx(18.89)


# ------------------------------------------------- C: the launch-time sum

WD = """
$Members = [ordered]@{
  'A'    = @('--symbol A ', 'run_A.bat')
  'B'    = @('--symbol B ', 'run_B.bat')
  'scanner' = @('tools\\scanner.py', 'run_scanner.bat')
}
"""

BAT = ("@echo off\npython main.py %s--symbol %s --hedge lighter "
       "--config %s --no-dashboard >> logs\\x.log 2>&1\n")


def build_tree(tmp, jobs, wd=WD):
    """jobs: name -> (live, config_name, yaml_text)"""
    eng = os.path.join(tmp, "engine")
    os.makedirs(os.path.join(eng, "tools"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "ops"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "results"), exist_ok=True)
    with open(os.path.join(tmp, "ops", "arb_watchdog.ps1"), "w",
              encoding="utf-8") as fh:
        fh.write(wd)
    with open(os.path.join(eng, "run_scanner.bat"), "w",
              encoding="utf-8") as fh:
        fh.write("@echo off\npython tools\\scanner.py\n")
    for name, (live, cfgname, text) in jobs.items():
        with open(os.path.join(eng, "run_%s.bat" % name), "w",
                  encoding="utf-8") as fh:
            fh.write(BAT % ("" if live else "--record-only ", name, cfgname))
        with open(os.path.join(eng, cfgname), "w", encoding="utf-8") as fh:
            fh.write(text)
    ab.ENGINE = eng
    ab.ARB = tmp
    ab.WATCHDOG = os.path.join(tmp, "ops", "arb_watchdog.ps1")
    ab.FLAG = os.path.join(tmp, "results", "account_budget.json")
    return eng


def cfg_text(leg_cap, acct_cap, free=50):
    """`leg_cap` is the PER-LEG max_position_usd, which is what B3 sums.

    The account ceiling is checked against one account's own gross, and the
    two legs settle on two accounts that do not share collateral -- so the
    quantity that may be summed against it is the per-venue cap, never
    max_gross_usd (the sum of both legs). max_gross_usd is set to 2x here
    only to keep the config internally consistent.
    """
    return ("entropy:\n  max_position_usd: {c}\n"
            "hedge:\n  max_position_usd: {c}\n"
            "risk:\n  max_gross_usd: {g}\n"
            "  max_account_gross_usd: {a}\n"
            "  min_account_free_usd: {f}\n"
            .format(c=leg_cap, g=2 * leg_cap, a=acct_cap, f=free))


def test_registry_parse_refuses_an_empty_table(tmp_path):
    build_tree(str(tmp_path), {}, wd="# the table's shape changed\n")
    with pytest.raises(RuntimeError):
        ab.registered_launchers()


def test_all_record_only_is_green(tmp_path):
    build_tree(str(tmp_path), {
        "A": (False, "config_A.yaml", cfg_text(1000, 2000)),
        "B": (False, "config_B.yaml", cfg_text(1000, 2000))})
    assert ab.main([]) == 0


def test_two_live_processes_over_the_account_cap_is_red(tmp_path):
    """The whole point: each process honours $1,000, the pool ceiling is
    $1,500, and two of them together are $2,000."""
    build_tree(str(tmp_path), {
        "A": (True, "config_A.yaml", cfg_text(1000, 1500)),
        "B": (True, "config_B.yaml", cfg_text(1000, 1500))})
    assert ab.main([]) == 1


def test_two_live_processes_within_the_account_cap_is_green(tmp_path):
    build_tree(str(tmp_path), {
        "A": (True, "config_A.yaml", cfg_text(1000, 2500)),
        "B": (True, "config_B.yaml", cfg_text(1000, 2500))})
    assert ab.main([]) == 0


def test_disagreeing_account_caps_is_red(tmp_path):
    build_tree(str(tmp_path), {
        "A": (True, "config_A.yaml", cfg_text(100, 5000)),
        "B": (True, "config_B.yaml", cfg_text(100, 9000))})
    assert ab.main([]) == 1


def test_missing_account_switch_is_red(tmp_path):
    build_tree(str(tmp_path), {
        "A": (True, "config_A.yaml", "risk:\n  max_gross_usd: 100\n")})
    assert ab.main([]) == 1


def test_unregistered_live_launcher_is_red(tmp_path):
    eng = build_tree(str(tmp_path), {
        "A": (False, "config_A.yaml", cfg_text(100, 5000))})
    with open(os.path.join(eng, "run_GMX.bat"), "w", encoding="utf-8") as fh:
        fh.write(BAT % ("", "GMX", "config_A.yaml"))
    assert ab.main([]) == 1


def test_flag_is_written_and_carries_the_verdict(tmp_path):
    import json
    build_tree(str(tmp_path), {
        "A": (True, "config_A.yaml", cfg_text(1000, 1500)),
        "B": (True, "config_B.yaml", cfg_text(1000, 1500))})
    ab.main([])
    with open(ab.FLAG, encoding="utf-8") as fh:
        d = json.load(fh)
    assert d["ok"] is False and d["live"] == 2 and "B3" in d["reason"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
