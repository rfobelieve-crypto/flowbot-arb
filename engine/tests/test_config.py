"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.dashboard and cfg.log_file


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")


def test_ws_ping_defaults_to_off():
    """The feed keepalive must stay OFF unless a config asks for it.

    `recorder.py` counts a minute as sampled with `is_fresh(staleness_sec)`,
    which reads `book.alive_ts`, which ANY inbound frame refreshes. Turning
    client pings on globally would therefore inflate `samples` for the nine
    recorders -- moving what counts as a sample under a measurement already
    in flight, which is exactly what `book.is_fresh`'s docstring forbids.

    So the default is 0.0 and only config_HMM_GMX.yaml opts in. If someone
    later gives this a non-zero default, this test is what says no.
    """
    cfg = load_config(write_tmp(MINIMAL), symbol="SNDK",
                      hedge_venue="lighter", env_file=NO_ENV)
    assert cfg.entropy.ws_ping_sec == 0.0
    assert cfg.hedge.ws_ping_sec == 0.0


def test_ws_ping_opt_in_is_wired_through():
    """And when a config does ask, it reaches both legs.

    A setting that parses but never reaches the feed is the same disease as
    a guard that cannot fire: it looks configured and does nothing.
    """
    cfg = load_config(write_tmp(MINIMAL + """
execution:
  ws_ping_sec: 5.0
  staleness_sec: 30.0
"""), symbol="SNDK", hedge_venue="lighter", env_file=NO_ENV)
    assert cfg.entropy.ws_ping_sec == 5.0
    assert cfg.hedge.ws_ping_sec == 5.0
    assert cfg.staleness_sec == 30.0


def test_recording_family_configs_have_no_ws_ping():
    """The nine recorders must stay byte-for-byte on the old behaviour.

    Not a style check -- `samples` is the §0.75 family's own denominator.
    """
    import glob
    # **具名清單,不是排除一個檔名。** 這一關要守的是「凍結的那九支不變」,
    # 所以每加一份 HMM 設定就要在這裡加一行 —— 那個摩擦是刻意的:
    # 它逼人回答「這份設定是 HMM 候選,還是我不小心動到了錄製家族」。
    # 2026-09-14 加 MET 時這一關紅了,而那正是它存在的理由。
    hmm = {"config_HMM_GMX.yaml", "config_MET.yaml",
           "config_OPENAI.yaml", "config_ANSEM.yaml",
           "config_MINIMAX.yaml"}
    changed = [os.path.basename(p)
               for p in glob.glob(os.path.join(ROOT, "config_*.yaml"))
               if os.path.basename(p) not in hmm
               and "ws_ping_sec" in io.open(p, encoding="utf-8").read()]
    assert not changed, f"recording configs gained ws_ping_sec: {changed}"
