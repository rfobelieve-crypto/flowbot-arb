#!/usr/bin/env python3
"""One page for every pair being recorded. Reads files; starts nothing.

Why this exists rather than a status page per engine: the recorders were
started before `snapshot.py` existed, so they write no status.json, and
restarting ten live processes to get a dashboard is a bad trade -- each
restart costs a gap in the very minute data the gate is counting, and the
30-second window while a .bat loop restarts its child is exactly when the
watchdog can launch a second copy (the duplicate-scanner bug of 2026-09-03).

So this reads what they already write:

  * `logs/<pair>/minutes.csv`  — the last row, plus the recent premium
  * `logs/<pair>/status.json`  — if an engine happens to publish one
  * file mtime                  — the liveness signal the runbook already
                                  uses (`os.stat`, not a directory listing:
                                  PowerShell's mtime lags on long-open files)

Read-only, no buttons, no network. Writes one self-refreshing HTML file.

    python tools/board.py                 # write logs/board.html
    python tools/board.py --open          # ... and open it
    python tools/board.py --hours 6       # premium stats over a shorter window
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import statistics
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
LOGS = os.path.join(ENGINE, "logs")
STALE_SEC = 180          # a recorder writes once a minute; 3 missed = a problem
REFRESH_SEC = 30


def read_pair(d: str, hours: float) -> dict | None:
    csv_path = os.path.join(d, "minutes.csv")
    if not os.path.exists(csv_path):
        return None
    name = os.path.basename(d.rstrip("\\/"))
    age = time.time() - os.stat(csv_path).st_mtime
    cutoff = time.time() - hours * 3600 if hours else 0
    rows, last = [], None
    with io.open(csv_path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                ts = int(r["minute_ts"])
                if ts >= cutoff:
                    rows.append((ts, float(r["premium_close_bps"]),
                                 float(r["sell_edge_max_bps"]),
                                 float(r["buy_edge_max_bps"])))
                last = r
            except (KeyError, TypeError, ValueError):
                continue
    if last is None:
        return None
    prem = [x[1] for x in rows]
    out = {
        "pair": name, "age_sec": age, "alive": age < STALE_SEC,
        "minutes": len(rows), "last": last,
        "premium_now": prem[-1] if prem else None,
        "premium_med": statistics.median(prem) if prem else None,
        "premium_p5": min(prem) if prem else None,
        "premium_p95": max(prem) if prem else None,
        "sell_med": statistics.median(x[2] for x in rows) if rows else None,
        "buy_med": statistics.median(x[3] for x in rows) if rows else None,
        "status": None,
    }
    sj = os.path.join(d, "status.json")
    if os.path.exists(sj):
        try:
            out["status"] = json.load(io.open(sj, encoding="utf-8"))
        except Exception:                                       # noqa: BLE001
            pass
    return out


def fmt(x, n=2, dash="—"):
    return dash if x is None else f"{x:,.{n}f}"


def render(pairs: list, hours: float) -> str:
    def row(p):
        st = (p["status"] or {}).get("public") or {}
        guards = st.get("guards") or []
        red = [g for g in guards if g.get("level") == "red"]
        mode = st.get("running", "record-only")
        badge = ("<span class=bad>DEAD</span>" if not p["alive"]
                 else f"<span class=ok>{mode}</span>")
        alert = (f'<span class=bad>{red[0]["what"]}</span>' if red else
                 (f'<span class=warn>{guards[0]["what"]}</span>' if guards else ""))
        sm, bm = p["sell_med"], p["buy_med"]
        side = ("both" if (sm or 0) > 0 and (bm or 0) > 0 else
                "SELL only" if (sm or 0) > 0 else
                "BUY only" if (bm or 0) > 0 else "neither")
        return (f"<tr><td><b>{p['pair']}</b> {badge} {alert}</td>"
                f"<td class=n>{fmt(p['age_sec'], 0)}s</td>"
                f"<td class=n>{p['minutes']}</td>"
                f"<td class='n big'>{fmt(p['premium_now'])}</td>"
                f"<td class=n>{fmt(p['premium_med'])}</td>"
                f"<td class=n>{fmt(p['premium_p5'])} … {fmt(p['premium_p95'])}</td>"
                f"<td class=n>{fmt(sm)}</td><td class=n>{fmt(bm)}</td>"
                f"<td class='{'ok' if side == 'both' else 'warn'}'>{side}</td></tr>")

    dead = [p for p in pairs if not p["alive"]]
    banner = (f'<div class="g bad">{len(dead)} recorder(s) not writing: '
              f'{", ".join(p["pair"] for p in dead)}</div>' if dead else
              '<div class="g ok">every recorder is writing</div>')
    return f"""<!doctype html><meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_SEC}">
<title>arb board</title>
<style>
 :root{{color-scheme:dark}}
 body{{background:#0d1117;color:#c9d1d9;font:13px/1.55 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:18px}}
 h1{{font-size:15px;margin:0 0 2px}} .sub{{color:#8b949e;margin-bottom:12px}}
 table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}}
 th,td{{padding:6px 9px;text-align:left;border-bottom:1px solid #21262d}}
 th{{color:#8b949e;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.06em}}
 .n{{text-align:right;font-variant-numeric:tabular-nums}} .big{{font-size:15px;font-weight:600}}
 .ok{{color:#3fb950}} .warn{{color:#e3b341}} .bad{{color:#f85149}}
 .g{{padding:7px 10px;border-radius:6px;margin-bottom:12px;background:#21262d}}
 .g.ok{{background:#12261c}} .g.bad{{background:#3d1418}}
 .note{{margin-top:12px;color:#6e7681;font-size:11px}}
</style>
<h1>arb board <span class=sub>— {len(pairs)} pairs · premium stats over
 {"all data" if not hours else f"the last {hours:g}h"}</span></h1>
<div class=sub>{time.strftime("%Y-%m-%d %H:%M:%S")} local · refreshes every {REFRESH_SEC}s</div>
{banner}
<table>
<tr><th>pair</th><th class=n>data age</th><th class=n>minutes</th>
    <th class=n>premium now</th><th class=n>median</th><th class=n>range</th>
    <th class=n>sell edge</th><th class=n>buy edge</th><th>executable</th></tr>
{"".join(row(p) for p in pairs)}
</table>
<div class=note>All figures in bps. "executable" is which direction had a positive
 median edge over the window — a pair that is <b>one-sided</b> can be entered but
 not unwound, which is the shape NBIS took on 09-03
 (<code>docs/NBIS_REGIME_20260903.md</code>). Read-only: this page starts nothing
 and talks to no exchange.</div>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=24.0,
                    help="window for the premium stats (0 = all data)")
    ap.add_argument("--out", default=os.path.join(LOGS, "board.html"))
    ap.add_argument("--open", action="store_true", dest="open_it")
    a = ap.parse_args()

    pairs = []
    # SNDK is the original member and writes to logs/minutes.csv directly,
    # not logs/SNDK/ -- it predates the per-pair directories. It is also the
    # only pair with a completed verdict, so leaving it off the board would
    # hide the one line with an answer.
    root = read_pair(LOGS, a.hours)
    if root:
        root["pair"] = "SNDK (root)"
        pairs.append(root)
    for d in sorted(glob.glob(os.path.join(LOGS, "*"))):
        if os.path.isdir(d):
            p = read_pair(d, a.hours)
            if p:
                pairs.append(p)
    if not pairs:
        print(f"no minutes.csv under {LOGS}")
        return 1
    io.open(a.out, "w", encoding="utf-8", newline="\n").write(render(pairs, a.hours))
    print(f"{len(pairs)} pairs -> {a.out}")
    for p in pairs:
        print(f"  {p['pair']:22s} {'alive' if p['alive'] else 'DEAD ':5s} "
              f"age {p['age_sec']:5.0f}s  prem now {fmt(p['premium_now']):>8s} "
              f"med {fmt(p['premium_med']):>8s}  "
              f"sell {fmt(p['sell_med']):>7s}  buy {fmt(p['buy_med']):>7s}")
    if a.open_it:
        webbrowser.open("file:///" + a.out.replace("\\", "/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
