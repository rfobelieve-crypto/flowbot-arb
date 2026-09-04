"""Configuration: strategy from a YAML file, credentials from .env, market
selection (symbol + hedge venue) from the command line.

The split is deliberate: config.yaml IS the strategy (thresholds, sizing,
risk) and is safe to share/commit as an example; .env holds only secrets;
which markets to trade is stated explicitly on every start (--symbol,
--hedge). Every YAML key is validated against the schema below, so a typo
is an error rather than a setting that silently does nothing.

Threshold model (fixed numbers the user derives from recorded minute data):

    premium_bps = (entropy_price / hedge_price - 1) * 10_000

    SELL entropy / BUY hedge  fires when the executable premium
        (entropy bid over hedge ask) >= midline_bps + upper_bps
    BUY entropy / SELL hedge  fires when the executable premium
        (entropy ask under hedge bid) <= midline_bps - lower_bps

    Both hurdles are net of both venues' taker fees, so a full round trip
    nets >= (upper_bps + lower_bps) after fees by construction.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used

HEDGE_VENUES = ("lighter", "lighter-rh", "tradexyz")


def _lighter_creds(venue: str):
    """Credentials for ONE Lighter deployment.

    mainnet and the Robinhood chain are separate accounts with separate API
    keys (upstream .env.example states this twice). Reading the same
    LIGHTER_* vars for both was a latent bug: harmless while only one leg was
    Lighter, fatal for NVDA_LL where BOTH legs are (flow_system TODO 1.08 §7,
    found by the B1 audit 2026-09-04).

    `LIGHTER_RH_*` overrides for the robinhood chain and falls back to the
    plain names, so every pre-existing config keeps loading identically.
    """
    pre = "LIGHTER_RH_" if venue == "lighter-rh" else "LIGHTER_"
    idx = _env_i(pre + "ACCOUNT_INDEX")
    kid = _env_i(pre + "API_KEY_INDEX")
    key = _env_s(pre + "API_PRIVATE_KEY")
    if venue == "lighter-rh" and not (idx or kid or key):
        idx, kid, key = (_env_i("LIGHTER_ACCOUNT_INDEX"),
                         _env_i("LIGHTER_API_KEY_INDEX"),
                         _env_s("LIGHTER_API_PRIVATE_KEY"))
    return LighterCreds(idx, kid, key)


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class VenueConf:
    key: str                  # "entropy" | "hedge"
    kind: str                 # "hl" | "lighter"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str
    fee_bps: float            # TAKER fee, bps
    # B3: the maker fee is a different number and the whole reason the maker
    # path exists (COST_INVENTORY: 13.5 bps taker hurdle vs 4.5 bps maker).
    # It may be NEGATIVE where the venue pays a rebate. It defaults to the
    # taker fee, never to zero: assuming a fee schedule you have not measured
    # is how a losing pair looks profitable on paper.
    maker_fee_bps: float
    cap_usd: float
    orders_per_min: int
    # hl
    hl_dex: str = ""
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None
    lighter_venue: str = ""   # "lighter" | "lighter-rh" (funding poller key)


@dataclass
class Config:
    symbol: str
    hedge_venue: str
    entropy: VenueConf
    hedge: VenueConf
    # thresholds (the whole signal)
    midline_bps: float
    upper_bps: float
    lower_bps: float
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    # B3 (2026-09-04) maker path. `mode` defaults to taker, so every config
    # written before B3 loads byte-for-byte identically.
    mode: str
    maker_venue: str
    # How long one post-only quote may rest before it is cancelled. This is
    # the numerator of M2's fill rate (LIVE_50U_SPEC S2).
    maker_timeout_sec: float
    # PEER_INFRA S7 must #3: the cancel gets its OWN budget, NOT staleness_sec
    # and not the quote's lifetime. A cancel unconfirmed past this is treated
    # as POSSIBLY FILLED, never as cancelled.
    cancel_timeout_sec: float
    maker_poll_sec: float
    # Cancel a resting quote once the edge it was posted for decays below
    # this (bps, net of both fees, measured against the CURRENT hedge book).
    # The XEMM lesson inverted: they cancel when the edge gets absurdly good
    # because that means they are the stale quote; we also cancel when it
    # goes bad, because a quote we can no longer hedge profitably is an
    # adverse-selection machine. 0 = cancel as soon as it would lose money.
    maker_min_edge_bps: float
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    net_tolerance_base: float
    # B4/G1 (2026-09-04): the two legs are meant to cancel. This is the
    # largest |sum of positions| the engine may carry before it stops
    # trading outright. Without it, an imbalance can grow silently one
    # failed hedge at a time -- the shape that cost the entropy-arb
    # author's peers $1.1M. 0 = disabled (not recommended once live).
    # Declared without a default so it cannot be forgotten at a call site;
    # load_config always supplies it (default 3x net_tolerance_base).
    max_net_base: float
    # B4 (2026-09-04): the session mark-to-market floor. The engine halts
    # when session PnL drops below -this. Constant cost: session_pnl() is a
    # sum over two venues, no I/O. 0 = disabled (not recommended once live).
    max_daily_loss_usd: float
    # B4 (2026-09-04): absolute ceiling on total gross exposure across both
    # venues. cap_usd is PER VENUE, so a config typo that scales size by 10x
    # passes every per-venue check. This is the backstop for that.
    max_gross_usd: float
    # B4: books stale this many evaluations in a row -> halt. "Stale forever"
    # currently looks identical to "quiet market", which is a silent-failure
    # shape (the venue_down path only covers position-fetch failures, not a
    # dead book feed while REST still answers).
    max_consecutive_stale: int
    # B4: how many reduce-only hedges are still allowed AFTER a halt, to
    # flatten an imbalance that already exists. 0 = freeze on halt (leaves
    # naked exposure); the default lets the engine unwind what it can.
    halt_flatten_attempts: int
    # B4 (2026-09-04): ceiling on the top-of-book premium the engine will act
    # on. An edge far outside the measured band means the book is wrong (a
    # halted or delisted underlying, a bad tick that persists, an oracle
    # break) -- and acting on a wrong book is exactly how you end up on the
    # losing side of it. premium_persist_sec filters a one-tick phantom; this
    # filters a book that is persistently wrong. 0 = disabled.
    max_edge_bps: float
    # Volatility circuit breaker (2026-09-04): the largest peak-to-trough
    # move, in bps, that either venue's mid may make inside vol_window_sec
    # before the engine stops opening NEW exposure for vol_cooldown_sec.
    # Unlike every other switch here this one is a PAUSE, not a HALT --
    # volatility fixes itself, and a breaker that needs a human restart after
    # every news spike gets switched off. Hedging, flattening, self-rescue
    # and reconcile are never paused. 0 = disabled.
    vol_window_sec: float
    vol_max_move_bps: float
    vol_cooldown_sec: float
    # Liquidation / foreign-trade detection (2026-09-04). A position that
    # moved while we sent nothing is not ours to manage: it is a liquidation,
    # an ADL, a manual trade on the same account, or a second bot. All four
    # mean the engine's model of the position is wrong in a way no amount of
    # reconciling fixes, so it halts (and still self-rescues). True unless
    # you deliberately share the account, which this project forbids anyway.
    unexplained_position_halt: bool
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # B5 control channel. Credentials come from .env like every other
    # secret; they are never logged.
    control_enabled: bool
    control_command_file: str
    control_poll_sec: float
    control_notify_critical: bool
    tg_bot_token: Optional[str]
    tg_chat_id: Optional[str]
    # recorder
    recorder_enabled: bool
    recorder_csv: str
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    maker_csv: str
    dashboard: bool
    log_file: str
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL

    @property
    def creds_complete(self) -> bool:
        for v in (self.entropy, self.hedge):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
        return True


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    "thresholds": {
        "midline_bps": float,
        "upper_bps": float,
        "lower_bps": float,
    },
    "entropy": {
        "dex": str,
        "venue": str,          # local patch 2026-09-04: hl | lighter | lighter-rh
        "symbol": str,         # alias on the leg-A venue (same idea as hedge.symbol)
        "taker_fee_bps": float,
        "maker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "hedge": {
        "symbol": None,   # optional ticker alias on the hedge venue (local patch 2026-08-30)
        "taker_fee_bps": float,
        "maker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "risk": {
        "max_net_base": float,      # B4/G1: hard cap on |leg A + leg B|
        "max_daily_loss_usd": float,  # B4: session MTM floor, halts on breach
        "max_gross_usd": float,       # B4: absolute sum |pos x mid| ceiling
        "max_consecutive_stale": int,  # B4: stale books N times in a row -> halt
        "halt_flatten_attempts": int,  # B4: reduce-only hedges allowed AFTER halt
        "max_edge_bps": float,        # B4: refuse an edge too good to be true
        "vol_window_sec": float,      # volatility breaker: measurement window
        "vol_max_move_bps": float,    # ... peak-to-trough that trips it
        "vol_cooldown_sec": float,    # ... how long the pause lasts
        "unexplained_position_halt": bool,  # halt on a move we did not cause
    },
    "execution": {
        "mode": str,                  # B3: taker | maker
        "maker_venue": str,           # B3: which leg rests (entropy | hedge)
        "maker_timeout_sec": float,   # B3: how long a quote may rest
        "cancel_timeout_sec": float,  # B3: the cancel's OWN budget
        "maker_poll_sec": float,      # B3: how often a resting order is read
        "maker_min_edge_bps": float,  # B3: cancel a quote whose edge decayed
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
    },
    # B5 control channel (2026-09-04): four verbs, out of band. It never
    # places an order, never changes a threshold, and never lifts a HALT.
    "control": {
        "enabled": bool,
        "command_file": str,
        "poll_sec": float,
        "notify_critical": bool,
    },
    "recorder": {
        "enabled": bool,
        "csv": str,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "maker_csv": str,
        "dashboard": bool,
        "file": str,
    },
}


class ConfigError(ValueError):
    pass


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else None


# -------------------------------------------------------------------- loading

def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                symbol: str, hedge_venue: str) -> Config:
    load_dotenv(env_file)
    try:
        with open(config_file, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / 未找到配置文件，请先复制 "
            f"config.example.yaml 为 config.yaml 并修改")
    _validate(raw, _SCHEMA)

    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required, e.g. --symbol SNDK / "
                          "必须用 --symbol 指定交易品种")
    if hedge_venue not in HEDGE_VENUES:
        raise ConfigError(
            f"--hedge must be one of {list(HEDGE_VENUES)}, got "
            f"{hedge_venue!r} / --hedge 必须是 {list(HEDGE_VENUES)} 之一")

    thr = raw.get("thresholds") or {}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / 必须填写，请用采集的分钟"
                              f"数据计算后填入")
    upper, lower = float(thr["upper_bps"]), float(thr["lower_bps"])
    if upper <= 0 or lower <= 0:
        raise ConfigError("thresholds.upper_bps and lower_bps must be > 0 "
                          "(the round trip nets upper+lower bps after fees)")

    # B4/G1: default is 3x the net tolerance -- tight enough that a single
    # stuck leg trips it, loose enough that normal settle lag does not.
    max_net_base = float(_get(raw, "risk", "max_net_base",
                              3.0 * float(_get(raw, "execution",
                                               "net_tolerance_base", 0.001))))
    if max_net_base < 0:
        raise ConfigError("risk.max_net_base must be >= 0 (0 disables)")
    max_daily_loss_usd = float(_get(raw, "risk", "max_daily_loss_usd", 0.0))
    if max_daily_loss_usd < 0:
        raise ConfigError("risk.max_daily_loss_usd must be >= 0 (0 disables)")
    max_gross_usd = float(_get(raw, "risk", "max_gross_usd", 0.0))
    if max_gross_usd < 0:
        raise ConfigError("risk.max_gross_usd must be >= 0 (0 disables)")
    max_consecutive_stale = int(_get(raw, "risk", "max_consecutive_stale", 0))
    if max_consecutive_stale < 0:
        raise ConfigError("risk.max_consecutive_stale must be >= 0 (0 disables)")
    halt_flatten_attempts = int(_get(raw, "risk", "halt_flatten_attempts", 3))
    if halt_flatten_attempts < 0:
        raise ConfigError("risk.halt_flatten_attempts must be >= 0")
    max_edge_bps = float(_get(raw, "risk", "max_edge_bps", 0.0))
    if max_edge_bps < 0:
        raise ConfigError("risk.max_edge_bps must be >= 0 (0 disables)")

    mode = str(_get(raw, "execution", "mode", "taker") or "taker").lower()
    if mode not in ("taker", "maker"):
        raise ConfigError(f"execution.mode must be taker|maker, got {mode!r}")
    maker_venue = str(_get(raw, "execution", "maker_venue", "entropy")
                      or "entropy").lower()
    if maker_venue not in ("entropy", "hedge"):
        raise ConfigError("execution.maker_venue must be entropy|hedge, got "
                          f"{maker_venue!r}")
    maker_timeout_sec = float(_get(raw, "execution", "maker_timeout_sec", 5.0))
    cancel_timeout_sec = float(_get(raw, "execution", "cancel_timeout_sec", 3.0))
    maker_poll_sec = float(_get(raw, "execution", "maker_poll_sec", 0.25))
    if mode == "maker":
        for name, v in (("maker_timeout_sec", maker_timeout_sec),
                        ("cancel_timeout_sec", cancel_timeout_sec),
                        ("maker_poll_sec", maker_poll_sec)):
            if v <= 0:
                raise ConfigError(f"execution.{name} must be > 0 in maker mode "
                                  f"(a zero budget is not a budget)")

    # Maker executions get their own CSV: the columns that matter for a
    # resting quote (rest time, fill vs cancel, cancel latency) have no
    # meaning for an IOC pair, and LIVE_50U_SPEC M2/M3/M4 read them directly.
    trades_csv = _get(raw, "logging", "trades_csv", "logs/trades.csv")
    maker_csv = _get(raw, "logging", "maker_csv", None) or os.path.join(
        os.path.dirname(trades_csv), "maker.csv")

    vol_window_sec = float(_get(raw, "risk", "vol_window_sec", 30.0))
    vol_max_move_bps = float(_get(raw, "risk", "vol_max_move_bps", 0.0))
    vol_cooldown_sec = float(_get(raw, "risk", "vol_cooldown_sec", 60.0))
    unexplained_position_halt = bool(_get(raw, "risk",
                                          "unexplained_position_halt", True))
    if vol_max_move_bps < 0:
        raise ConfigError("risk.vol_max_move_bps must be >= 0 (0 disables)")
    if vol_max_move_bps and vol_window_sec <= 0:
        raise ConfigError("risk.vol_window_sec must be > 0 when the "
                          "volatility breaker is armed")
    if vol_max_move_bps and vol_cooldown_sec <= 0:
        raise ConfigError("risk.vol_cooldown_sec must be > 0 when the "
                          "volatility breaker is armed (a pause of zero "
                          "seconds is not a pause)")

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / 必须在 (0, 1] 之间")

    entropy_dex = _get(raw, "entropy", "dex", "io")
    if hedge_venue == "tradexyz" and entropy_dex == "xyz":
        raise ConfigError("entropy.dex 'xyz' with hedge_venue 'tradexyz' is "
                          "the same market on both legs / 两条腿是同一个市场")

    entropy_hl_creds = HLCreds(_env_s("HL_PRIVATE_KEY"),
                               _env_s("HL_ACCOUNT_ADDRESS"))
    # dex "" = Hyperliquid core mainnet as leg A (crypto pairs; local patch
    # 2026-08-30 for the §0.75 recording family). Label it honestly.
    entropy = VenueConf(
        key="entropy", kind="hl", label="ENTROPY" if entropy_dex else "HL",
        symbol=symbol,
        fee_bps=float(_get(raw, "entropy", "taker_fee_bps", 0.0)),
        maker_fee_bps=float(_get(raw, "entropy", "maker_fee_bps",
                                 _get(raw, "entropy", "taker_fee_bps", 0.0))),
        cap_usd=float(_get(raw, "entropy", "max_position_usd", 1000.0)),
        orders_per_min=int(_get(raw, "entropy", "max_orders_per_min", 120)),
        hl_dex=entropy_dex,
        hl_creds=entropy_hl_creds,
    )

    # Local patch 2026-09-04 (flow_system TODO 1.02): leg A may itself be a
    # Lighter chain, so a pair whose BOTH legs sit on a structurally zero-fee
    # schedule (lighter <-> lighter-rh) can be recorded with the same engine.
    # `entropy.venue: lighter|lighter-rh`; default "hl" keeps every existing
    # config byte-for-byte the same.
    entropy_venue = str(_get(raw, "entropy", "venue", "hl") or "hl")
    if entropy_venue in ("lighter", "lighter-rh"):
        if entropy_venue == hedge_venue:
            raise ConfigError("entropy.venue equals --hedge: both legs are the "
                              "same market / 两条腿是同一个市场")
        entropy = VenueConf(
            key="entropy", kind="lighter",
            label="LIGHTER" if entropy_venue == "lighter" else "RH",
            symbol=str(_get(raw, "entropy", "symbol", None) or symbol),
            fee_bps=float(_get(raw, "entropy", "taker_fee_bps", 0.0)),
            maker_fee_bps=float(_get(raw, "entropy", "maker_fee_bps",
                                     _get(raw, "entropy", "taker_fee_bps", 0.0))),
            cap_usd=float(_get(raw, "entropy", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "entropy", "max_orders_per_min", 30)),
            lighter_profile=LIGHTER_PROFILES[entropy_venue],
            lighter_creds=_lighter_creds(entropy_venue),
            lighter_venue=entropy_venue,
        )
    elif entropy_venue != "hl":
        raise ConfigError(f"entropy.venue must be hl|lighter|lighter-rh, got {entropy_venue!r}")

    if hedge_venue == "tradexyz":
        hedge = VenueConf(
            key="hedge", kind="hl", label="XYZ",
            symbol=symbol,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 1.0)),
            maker_fee_bps=float(_get(raw, "hedge", "maker_fee_bps",
                                     _get(raw, "hedge", "taker_fee_bps", 1.0))),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 120)),
            hl_dex="xyz",
            hl_creds=HLCreds(
                _env_s("HL_PRIVATE_KEY_XYZ") or _env_s("HL_PRIVATE_KEY"),
                _env_s("HL_ACCOUNT_ADDRESS_XYZ") or _env_s("HL_ACCOUNT_ADDRESS")),
        )
    else:
        # hedge.symbol: optional alias when the hedge venue lists the same
        # instrument under a different ticker (Entropy io:OAI == Lighter
        # OPENAI, io:ANTH == ANTHROPIC). Local patch 2026-08-30.
        hedge = VenueConf(
            key="hedge", kind="lighter",
            label="LIGHTER" if hedge_venue == "lighter" else "RH",
            symbol=str(_get(raw, "hedge", "symbol", None) or symbol),
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 0.0)),
            maker_fee_bps=float(_get(raw, "hedge", "maker_fee_bps",
                                     _get(raw, "hedge", "taker_fee_bps", 0.0))),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 30)),
            lighter_profile=LIGHTER_PROFILES[hedge_venue],
            lighter_creds=_lighter_creds(hedge_venue),
            lighter_venue=hedge_venue,
        )

    return Config(
        max_net_base=max_net_base,
        max_daily_loss_usd=max_daily_loss_usd,
        max_gross_usd=max_gross_usd,
        max_consecutive_stale=max_consecutive_stale,
        halt_flatten_attempts=halt_flatten_attempts,
        max_edge_bps=max_edge_bps,
        vol_window_sec=vol_window_sec,
        vol_max_move_bps=vol_max_move_bps,
        vol_cooldown_sec=vol_cooldown_sec,
        unexplained_position_halt=unexplained_position_halt,
        symbol=symbol,
        hedge_venue=hedge_venue,
        entropy=entropy,
        hedge=hedge,
        midline_bps=float(thr["midline_bps"]),
        upper_bps=upper,
        lower_bps=lower,
        take_fraction=take_fraction,
        max_order_notional=float(_get(raw, "sizing", "max_order_notional_usd", 500.0)),
        min_order_notional=float(_get(raw, "sizing", "min_order_notional_usd", 10.0)),
        inventory_scale_bps=float(_get(raw, "inventory", "scale_bps", 10.0)),
        inventory_floor_frac=float(_get(raw, "inventory", "floor_frac", 0.5)),
        mode=mode,
        maker_venue=maker_venue,
        maker_timeout_sec=maker_timeout_sec,
        cancel_timeout_sec=cancel_timeout_sec,
        maker_poll_sec=maker_poll_sec,
        maker_min_edge_bps=float(_get(raw, "execution", "maker_min_edge_bps", 0.0)),
        premium_persist_sec=float(_get(raw, "execution", "premium_persist_sec", 0.3)),
        cooldown_sec=float(_get(raw, "execution", "cooldown_sec", 0.0)),
        settle_timeout_sec=float(_get(raw, "execution", "settle_timeout_sec", 5.0)),
        leg_slippage_bps=float(_get(raw, "execution", "leg_slippage_bps", 50.0)),
        hedge_slippage_bps=float(_get(raw, "execution", "hedge_slippage_bps", 20.0)),
        net_tolerance_base=float(_get(raw, "execution", "net_tolerance_base", 0.001)),
        max_consecutive_errors=int(_get(raw, "execution", "max_consecutive_errors", 3)),
        rate_limit_pause_sec=float(_get(raw, "execution", "rate_limit_pause_sec", 10.0)),
        staleness_sec=float(_get(raw, "execution", "staleness_sec", 10.0)),
        reconcile_sec=float(_get(raw, "execution", "reconcile_sec", 15.0)),
        venue_probe_sec=float(_get(raw, "execution", "venue_probe_sec", 30.0)),
        http_keepalive_sec=float(_get(raw, "execution", "http_keepalive_sec", 10.0)),
        control_enabled=bool(_get(raw, "control", "enabled", True)),
        control_command_file=str(_get(raw, "control", "command_file",
                                      "control.cmd")),
        control_poll_sec=float(_get(raw, "control", "poll_sec", 2.0)),
        control_notify_critical=bool(_get(raw, "control", "notify_critical",
                                          True)),
        tg_bot_token=_env_s("ARB_TG_BOT_TOKEN"),
        tg_chat_id=_env_s("ARB_TG_CHAT_ID"),
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_csv=_get(raw, "recorder", "csv", "logs/minutes.csv"),
        log_level=str(_get(raw, "logging", "level", "INFO")).upper(),
        status_interval_sec=float(_get(raw, "logging", "status_interval_sec", 30.0)),
        trades_csv=trades_csv,
        maker_csv=maker_csv,
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_get(raw, "logging", "file", "logs/engine.log"),
    )
