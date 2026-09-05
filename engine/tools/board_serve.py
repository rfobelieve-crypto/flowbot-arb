#!/usr/bin/env python3
"""Serve the board on http://127.0.0.1:8765/ and keep it current.

`board.py` writes an HTML file; a file is awkward to look at (a browser
extension cannot open file:// URLs, and the page only changes when something
regenerates it). This does both jobs in one command:

  * regenerates board.html every --every seconds, in a background thread
  * serves the logs directory read-only, bound to 127.0.0.1

Bound to loopback ON PURPOSE. It serves market data, but the machine it runs
on is the machine with the keys, and `docs/DEPLOY.md` is explicit: nothing
reaches into the engine host from outside. The production path is the
opposite direction -- the engine PUSHES a snapshot out. This is the local
convenience version of the same page, not a step toward exposing it.

    python tools/board_serve.py                 # http://127.0.0.1:8765/
    python tools/board_serve.py --port 9000 --every 30
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import socketserver
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
LOGS = os.path.join(ENGINE, "logs")
sys.path.insert(0, HERE)

import board  # noqa: E402


class Quiet(http.server.SimpleHTTPRequestHandler):
    """No request logging: this page refreshes itself every 30s and the
    console is for the engine, not for 2,880 GET lines a day."""

    def log_message(self, fmt, *args):
        pass


def regenerate_forever(out: str, hours: float, every: float) -> None:
    while True:
        try:
            pairs = []
            root = board.read_pair(LOGS, hours)
            if root:
                root["pair"] = "SNDK (root)"
                pairs.append(root)
            for d in sorted(os.scandir(LOGS), key=lambda e: e.name):
                if d.is_dir():
                    p = board.read_pair(d.path, hours)
                    if p:
                        pairs.append(p)
            if pairs:
                tmp = out + ".tmp"
                with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(board.render(pairs, hours))
                os.replace(tmp, out)
        except Exception as e:                                  # noqa: BLE001
            print(f"  regenerate failed: {e!r}", flush=True)
        time.sleep(every)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--every", type=float, default=15.0)
    a = ap.parse_args()

    out = os.path.join(LOGS, "board.html")
    threading.Thread(target=regenerate_forever, args=(out, a.hours, a.every),
                     daemon=True).start()
    handler = functools.partial(Quiet, directory=LOGS)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", a.port), handler) as httpd:
        print(f"  board   http://127.0.0.1:{a.port}/board.html")
        print(f"  shadow  http://127.0.0.1:{a.port}/NBIS_shadow/status.html")
        print(f"  (regenerating every {a.every:g}s over the last {a.hours:g}h; "
              f"loopback only; Ctrl+C to stop)", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
