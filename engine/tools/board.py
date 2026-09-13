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
    name = os.path.basename(d.rstrip("\\/"))
    sj = os.path.join(d, "status.json")
    if not os.path.exists(csv_path):
        # HMM (2026-09-13): an engine may run with `recorder.enabled: false`,
        # and before this branch existed such an engine had NO ROW AT ALL --
        # a live strategy invisible on the board that is supposed to watch it.
        # Absence of minute data is not absence of an engine; liveness then
        # comes from status.json's own mtime.
        if not os.path.exists(sj):
            return None
        age = time.time() - os.stat(sj).st_mtime
        out = {"pair": name, "age_sec": age, "alive": age < STALE_SEC,
               "minutes": 0, "last": None, "premium_now": None,
               "premium_med": None, "premium_p5": None, "premium_p95": None,
               "sell_med": None, "buy_med": None, "status": None}
        try:
            out["status"] = json.load(io.open(sj, encoding="utf-8"))
        except Exception:                                       # noqa: BLE001
            pass
        return out
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
        # EVERY guard, not just the first (2026-09-13). With B6 a pair can
        # carry several at once, and showing guards[0] hides the account-level
        # ones behind whatever happened to be appended earlier -- the failure
        # mode being that an engine blocked on margin looks like a quiet
        # market. Reds first so the worst one is leftmost.
        amber = [g for g in guards if g.get("level") != "red"]
        alert = " ".join(
            [f'<span class=bad>{g["what"]}</span>' for g in red]
            + [f'<span class=warn>{g["what"]}</span>' for g in amber])
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

    # ------------------------------------------------------------ HMM block
    # The premium table above answers "is there a band" -- that is the
    # RECORDING family's question. A maker engine's question is different and
    # none of its numbers appear up there: the four things the 9th override
    # says are the only judgement (X4 M2/M3/M4/M5) are all in status.json and
    # were, until today, written every 30 seconds and read by nothing.
    def maker_row(p):
        st = (p["status"] or {}).get("public") or {}
        pv = (p["status"] or {}).get("private") or {}
        c = st.get("counts") or {}
        rested, fills = c.get("quotes_rested", 0), c.get("quote_fills", 0)
        fr = st.get("fill_rate_pct")
        # M2 判準：>=30% 可用、<10% 這條路關掉。0 筆報價時不著色 ——
        # 「還沒開始」不是「不及格」（mistake.md 2026-09-03：ok 的語意是
        # 連得上且設定對，不是有資料）。
        fr_cls = ("" if not rested else
                  "ok" if (fr or 0) >= 30 else
                  "bad" if (fr or 0) < 10 else "warn")
        pos = " ".join(f"{k} {fmt(v, 4)}"
                       for k, v in (pv.get("positions") or {}).items())
        # metrics.snapshot() is {key: {p50,p95,p99,max,n}} keyed by
        # arb/hedge/quote/cancel. The first version of this row read
        # lat["hedge_p50"], which does not exist -- the column would have been
        # blank forever, and a column that can never populate is the same
        # disease as a guard that can never fire (mistake.md 2026-09-03).
        lat = st.get("latency_ms") or {}
        hedge = lat.get("hedge") or {}
        cancel = lat.get("cancel") or {}
        # M4 判準：中位 <2s、p95 <10s。
        m4_cls = ("" if not hedge else
                  "ok" if (hedge.get("p50", 0) < 2000
                           and hedge.get("p95", 0) < 10000) else "bad")
        # M3 判準：平均漂移 > −1 bps 代表沒有被系統性挑走。
        # usd 加權那個才是「這段時間實際被挑走多少」。0 筆已結算時不著色 ——
        # 括號裡的 (n+pending) 就是為了分辨「還沒到期」與「真的沒有成交」。
        mk3 = st.get("markout") or {}
        u3 = mk3.get("usd_bps")
        m3_cls = ("" if not mk3.get("n") else
                  "ok" if (u3 or 0) > -1.0 else "bad")
        # M5：行情過期。FLAT 時引擎只暫停並計次，第 N 次才真的 HALT ——
        # 所以「還沒 HALT」不等於「沒事」，爬升本身就是訊號。
        # 0 不著色（沒開始 != 不及格）；到達上限前一格就紅，因為那時還來得及
        # 調 staleness_sec，HALT 之後就只剩人工重啟。
        ep = c.get("stale_episodes", 0)
        ep_lim = c.get("stale_episode_limit") or 0
        m5_cls = ("" if not ep else
                  "bad" if (ep_lim and ep >= ep_lim - 1) else "warn")
        return (f"<tr><td><b>{p['pair']}</b></td>"
                f"<td class=n>{rested}</td><td class=n>{fills}</td>"
                f"<td class=n>{c.get('quotes_cancelled', 0)}</td>"
                f"<td class='n {fr_cls}'>{fmt(fr, 1)}%</td>"
                f"<td class=n>{c.get('post_only_rejects', 0)}</td>"
                f"<td class=n>{c.get('unresolved_cancels', 0)}</td>"
                f"<td class='n {m4_cls}'>{fmt(hedge.get('p50'), 0)}"
                f" / {fmt(hedge.get('p95'), 0)}</td>"
                f"<td class=n>{fmt(cancel.get('p50'), 0)}</td>"
                f"<td class='n {m3_cls}'>{fmt(mk3.get('usd_bps'), 2)}"
                f"<span class=sub> ({mk3.get('n', 0)}"
                f"+{mk3.get('pending', 0)}p)</span></td>"
                f"<td class='n {m5_cls}'>{ep}/{ep_lim or chr(8734)}"
                f"<span class=sub> streak {c.get('stale_streak', 0)}</span></td>"
                f"<td class=n>{pos or '—'}</td>"
                f"<td class=n>{fmt(pv.get('net_base'), 4)}</td>"
                f"<td class=n>{fmt(pv.get('session_mtm_usd'), 4)}</td></tr>")

    mk = [p for p in pairs
          if ((p["status"] or {}).get("public") or {}).get("mode") == "maker"]
    maker_block = "" if not mk else f"""
<h1 style="margin-top:18px">HMM <span class=sub>— 掛單腿的執行面（X4 M2/M4）</span></h1>
<table>
<tr><th>pair</th><th class=n>掛出</th><th class=n>成交</th><th class=n>撤單</th>
    <th class=n>M2 成交率</th><th class=n>po 拒絕</th><th class=n>撤單未確認</th>
    <th class=n>M4 對沖 p50/p95 ms</th><th class=n>撤單 p50 ms</th>
    <th class=n>M3 60s 漂移 bps</th>
    <th class=n>M5 過期 ep</th>
    <th class=n>部位</th><th class=n>淨</th><th class=n>session MTM $</th></tr>
{"".join(maker_row(p) for p in mk)}
</table>
<div class=note><b>撤單未確認</b>不是雜訊：那是 <code>unknown</code> 狀態，
 意思是「可能已經成交」而不是「已經沒了」，而一張卡在 unknown 的單會擋住
 下一次報價（<code>maker.py</code> 規則三）。<b>M2</b> 的判準是
 &ge;30% 可用、&lt;10% 這條路關掉 —— 0 筆報價時不著色，因為「還沒開始」
 不是「不及格」。<b>M3</b> 的判準是 usd 加權漂移 &gt; −1 bps（沒有被系統性
 挑走），括號裡是「已結算 + 還在等到期」—— 那兩個數字才分得出
 「還沒到 60 秒」與「真的沒有成交」。</div>
"""

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
{maker_block}
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
