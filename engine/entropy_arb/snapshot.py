"""Engine state snapshot (2026-09-05).

Two outputs, one source:

* `status.json` — the machine-readable one. This is exactly the object that
  will later be POSTed to Vercel (`docs/DEPLOY.md`), so the shape is decided
  here and the transport is a separate problem for later.
* `status.html` — a self-contained page that refreshes itself, for looking at
  a run in progress right now. No server, no port, no CORS: the data is
  inlined, so it opens from `file://`. It is a convenience, not the product.

**It computes nothing.** Every number below already exists in the engine
(`LIVE_50U_SPEC` §4: "全部是引擎已經在算的東西，不新增計算"). If a field is
not already a property of the engine, it does not belong here.

**It is a sidecar.** A snapshot that fails is a log line, never an
interruption -- `CLAUDE.md` §2: 旁路壞掉不影響交易.

**One thing this cannot show, and it matters.** Shadow never accumulates a
position, because accumulating one would mean simulating fills. So a shadow
run answers "how often does an opportunity qualify" -- it does NOT answer
"how many trades you would have done". A live engine stops at `cap_usd`
after two clips and waits for the premium to revert; shadow keeps qualifying
on every book update. Read shadow.csv as an opportunity census, never as a
trade count.

**Two files, not one** (`docs/DEPLOY.md` §三): the public half may contain
only percentages, direction and time; dollars and position sizes live in the
private half. Mixing them in one object is how the wrong half eventually
gets rendered in public.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("snapshot")

REFRESH_SEC = 5


def _book(v, staleness: float) -> dict:
    bid, ask = v.book.best_bid(), v.book.best_ask()
    mid = v.book.mid()
    return {
        "venue": v.name,
        "bid": bid, "ask": ask, "mid": mid,
        "spread_bps": ((ask / bid - 1.0) * 1e4) if (bid and ask) else None,
        "age_sec": (time.time() - v.book.alive_ts) if v.book.alive_ts else None,
        "fresh": v.book.is_fresh(staleness),
    }


def build(eng) -> dict:
    """One dict, split into a public half and a private half."""
    cfg = eng.cfg
    now = time.time()
    prem = eng.premium_bps()
    guards = []
    if eng.halted:
        guards.append({"level": "red", "what": "HALTED",
                       "why": "restart required (a restart re-reads real "
                              "positions strictly)"})
    if eng.paused_by_operator:
        guards.append({"level": "amber", "what": "PAUSED",
                       "why": f"by {eng.pause_source or 'operator'}"})
    if eng.flatten_request:
        guards.append({"level": "amber", "what": "FLATTENING",
                       "why": "closing everything, then staying paused"})
    if eng.vol.paused(now):
        guards.append({"level": "amber", "what": "VOL-PAUSE",
                       "why": f"{eng.vol.reason} ({eng.vol.remaining(now):.0f}s left)"})
    for key in eng._venue_down:
        guards.append({"level": "red", "what": "VENUE DOWN", "why": key})
    for o in eng._maker_open.values():
        guards.append({"level": "amber", "what": "RESTING QUOTE",
                       "why": o.describe()})
    if eng.unexplained_events:
        guards.append({"level": "red", "what": "UNEXPLAINED POSITION MOVE",
                       "why": f"x{eng.unexplained_events} — liquidation, ADL, "
                              f"or someone else on this account"})
    if eng._recorder_dead:
        guards.append({"level": "red", "what": "RECORDER DEAD",
                       "why": eng._recorder_dead[:120]})
    stale = [v.name for v in eng.venues.values()
             if not v.book.is_fresh(cfg.staleness_sec)]
    if stale:
        guards.append({"level": "red", "what": "STALE BOOK",
                       "why": ", ".join(stale)})

    fill_rate = (100.0 * eng.maker_fills / eng.maker_rested
                 if eng.maker_rested else None)
    public = {
        "ts": now,
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "pair": f"{cfg.symbol} entropy/{cfg.hedge_venue}",
        "mode": cfg.mode,
        "running": "shadow" if eng.shadow else
                   ("record-only" if eng.record_only else "live"),
        "uptime_sec": now - eng.start_ts,
        "premium_bps": prem,
        "band": {"low": cfg.midline_bps - cfg.lower_bps,
                 "mid": cfg.midline_bps,
                 "high": cfg.midline_bps + cfg.upper_bps},
        "in_band": (prem is not None
                    and cfg.midline_bps - cfg.lower_bps <= prem
                    <= cfg.midline_bps + cfg.upper_bps),
        "fees_bps": {"entropy_taker": cfg.entropy.fee_bps,
                     "entropy_maker": cfg.entropy.maker_fee_bps,
                     "hedge_taker": cfg.hedge.fee_bps,
                     "hedge_maker": cfg.hedge.maker_fee_bps},
        "books": [_book(v, cfg.staleness_sec) for v in eng.venues.values()],
        "guards": guards,
        "counts": {
            "trades": eng.trades, "hedges": eng.hedges,
            "quotes_rested": eng.maker_rested, "quote_fills": eng.maker_fills,
            "quotes_cancelled": eng.maker_cancels,
            "post_only_rejects": eng.maker_rejects,
            "unresolved_cancels": eng.maker_unknown,
            "edges_refused": eng._absurd_skips,
            "vol_trips": eng.vol.trips,
            "shadow_decisions": eng.shadow_decisions,
        },
        "fill_rate_pct": fill_rate,            # M2
        "latency_ms": eng.lat.snapshot(),      # M4 + the cancel round trip
    }
    pnl = eng.session_pnl()
    private = {
        "ts": now,
        "positions": {v.name: v.position for v in eng.venues.values()},
        "net_base": sum(v.position for v in eng.venues.values()),
        "session_mtm_usd": pnl,
        "account_delta_usd": eng.account_delta(),
        "equity_usd": {v.name: v.equity for v in eng.venues.values()},
        "free_usd": {v.name: v.free for v in eng.venues.values()},
        "caps_usd": {v.name: v.cap_usd for v in eng.venues.values()},
        "exp_edge_usd": eng.total_exp_edge,
        "fill_edge_usd": eng.total_fill_edge,
        "recent": list(eng.recent_trades)[-20:],
    }
    return {"public": public, "private": private}


def write(eng, json_path: str, html_path: Optional[str] = None) -> None:
    """Never raises. A snapshot that fails is a log line."""
    try:
        snap = build(eng)
        d = os.path.dirname(json_path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, json_path)     # readers never see a half-written file
        if html_path:
            with open(html_path + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(render_html(snap))
            os.replace(html_path + ".tmp", html_path)
    except Exception:                                           # noqa: BLE001
        log.exception("snapshot write failed (trading is unaffected)")


# --------------------------------------------------------------------- html

def _fmt(x, digits=2, dash="—"):
    return dash if x is None else f"{x:,.{digits}f}"


def render_html(snap: dict) -> str:
    """A page you can open from disk. Deliberately dependency-free."""
    p, q = snap["public"], snap["private"]
    band = p["band"]
    prem = p["premium_bps"]
    prem_cls = ("ok" if p["in_band"] else "hot") if prem is not None else "dim"
    guards = p["guards"] or [{"level": "green", "what": "ALL CLEAR",
                              "why": "no guard is asserting"}]
    grows = "".join(
        f'<div class="g {g["level"]}"><b>{g["what"]}</b><span>{g["why"]}</span></div>'
        for g in guards)
    brows = "".join(
        f'<tr><td>{b["venue"]}</td><td class="n">{_fmt(b["bid"])}</td>'
        f'<td class="n">{_fmt(b["ask"])}</td>'
        f'<td class="n">{_fmt(b["spread_bps"], 2)}</td>'
        f'<td class="n {"" if b["fresh"] else "hot"}">{_fmt(b["age_sec"], 1)}s</td></tr>'
        for b in p["books"])
    lat = p["latency_ms"]
    lrows = "".join(
        f'<tr><td>{k}</td><td class="n">{_fmt(v["p50"], 0)}</td>'
        f'<td class="n">{_fmt(v["p95"], 0)}</td>'
        f'<td class="n">{_fmt(v["p99"], 0)}</td><td class="n">{v["n"]}</td></tr>'
        for k, v in sorted(lat.items())) or \
        '<tr><td colspan="5" class="dim">nothing measured yet</td></tr>'
    c = p["counts"]
    crows = "".join(f'<div class="kv"><span>{k.replace("_", " ")}</span>'
                    f'<b>{v}</b></div>' for k, v in c.items() if v)
    pos = " · ".join(f"{k} {v:+.6g}" for k, v in q["positions"].items())
    return f"""<!doctype html><meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_SEC}">
<title>{p["pair"]} — {p["running"]}</title>
<style>
 :root{{color-scheme:dark}}
 body{{background:#0d1117;color:#c9d1d9;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:18px}}
 h1{{font-size:15px;margin:0 0 2px;font-weight:600}}
 .sub{{color:#8b949e;margin-bottom:14px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}
 .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}}
 .card h2{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#8b949e;margin:0 0 8px;font-weight:600}}
 .big{{font-size:26px;font-weight:600}}
 table{{width:100%;border-collapse:collapse}} td,th{{padding:3px 6px;text-align:left}}
 th{{color:#8b949e;font-weight:500;font-size:11px}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
 .ok{{color:#3fb950}} .hot{{color:#f85149}} .dim{{color:#6e7681}}
 .g{{display:flex;justify-content:space-between;gap:10px;padding:5px 8px;border-radius:5px;margin-bottom:5px;background:#21262d}}
 .g.red{{background:#3d1418;color:#ff7b72}} .g.amber{{background:#3a2c11;color:#e3b341}}
 .g.green{{background:#12261c;color:#3fb950}} .g span{{color:inherit;opacity:.8;text-align:right}}
 .kv{{display:flex;justify-content:space-between;padding:2px 0}} .kv span{{color:#8b949e}}
 .note{{margin-top:14px;color:#6e7681;font-size:11px}}
</style>
<h1>{p["pair"]} <span class="dim">· mode {p["mode"]} · <b>{p["running"]}</b></span></h1>
<div class="sub">{p["time_utc"]} · up {p["uptime_sec"] / 3600:.1f}h · refreshes every {REFRESH_SEC}s</div>
<div class="grid">
 <div class="card"><h2>premium</h2>
  <div class="big {prem_cls}">{_fmt(prem)} bps</div>
  <div class="dim">band {_fmt(band["low"])} … {_fmt(band["high"])} (mid {_fmt(band["mid"])})
   — {"inside" if p["in_band"] else "OUTSIDE"}</div>
  <div class="dim">fees taker {p["fees_bps"]["entropy_taker"]}+{p["fees_bps"]["hedge_taker"]}
   · maker {p["fees_bps"]["entropy_maker"]}+{p["fees_bps"]["hedge_maker"]} bps</div>
 </div>
 <div class="card"><h2>guards</h2>{grows}</div>
 <div class="card"><h2>books</h2><table>
  <tr><th>venue</th><th class="n">bid</th><th class="n">ask</th><th class="n">spread</th><th class="n">age</th></tr>
  {brows}</table></div>
 <div class="card"><h2>position</h2>
  <div class="big">{pos or "—"}</div>
  <div class="dim">net {q["net_base"]:+.6g} · session MTM {_fmt(q["session_mtm_usd"], 4)} USD</div>
 </div>
 <div class="card"><h2>latency p50/p95/p99 (ms)</h2><table>
  <tr><th>action</th><th class="n">p50</th><th class="n">p95</th><th class="n">p99</th><th class="n">n</th></tr>
  {lrows}</table></div>
 <div class="card"><h2>counters</h2>{crows or '<div class="dim">nothing yet</div>'}</div>
</div>
<div class="note">Read-only. This page has no buttons and never talks back to the engine
 — <code>docs/DEPLOY.md</code>. Dollar figures are shown because this file is local;
 the public snapshot carries percentages, direction and time only.</div>
"""
