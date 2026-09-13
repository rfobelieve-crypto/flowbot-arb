# -*- coding: utf-8 -*-
"""Railway 上的掃描器服務：跑掃描器 ＋ 一個唯讀的檔案端點。

    python tools/scan_service.py        # Railway 的 CMD

===========================================================================
為什麼掃描器要離開本機
===========================================================================
Lighter 坐在 CloudFront + WAF 後面，那一層是 **per-IP**，而且掃描器
**一個憑證都不帶** —— 帳戶是不是 Premium 完全不相關。2026-09-13 那個
per-IP 預算用完了：WS 握手開始回 403，`lighter_tape` 約 1.7 小時連不回來，
丟掉不可回填的成交帶。

量出來的腳印：掃描器一支 65 次/分（24 小時不停），十支引擎 reconcile 合計
約 40 次/分。**研究負載是執行負載的十六倍，而它們在搶同一個 IP 預算。**

那個失效模式對研究錄製器可以活，對 HMM 不行：live 引擎的對沖腿行情在封鎖
期間斷線就回不來，而那時它手上有部位。所以掃描器搬走，把整個 per-IP 預算
留給引擎。完整理由與取捨見 `docs/DEPLOY.md` §6。

===========================================================================
兩個設計決定
===========================================================================
**掃描器跑在 subprocess，不是執行緒。** 它掛掉時 HTTP 端點還活著，
`/health` 才報得出「掃描器死了」—— 一個把自己也一起帶走的監控端點，
在最需要它的那一刻不存在。

**檔案端點支援 Range，而且只支援讀。** 每日 CSV 約 100 MB 且是
append-only，所以本機記 byte offset、每輪只拉新增的那幾百 KB。
沒有 Range 的話是 100 MB x 144 次/天。

**這個容器裡沒有任何交易金鑰**（掃描器本來就零憑證）。`SCAN_TOKEN` 只防
「變成開放端點」，它外洩的後果是「別人看得到公開盤口資料」，不是任何
交易能力 —— 那兩件事的等級差三個數量級。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCANNER = os.path.join(REPO, "engine", "tools", "scanner.py")
OUT_DIR = os.path.join(REPO, "engine", "logs", "scan")
TOKEN = os.environ.get("SCAN_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

# 使用者決定（2026-09-13）：Railway 上只留兩天。它的檔案系統是暫存的，
# 而本機才是 durable 的那一份 —— 兩天代表「拉取斷一整天還救得回來」，
# 而不用為了永久保存去買磁碟。
KEEP_DAYS = 2
PRUNE_EVERY_SEC = 3600
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

_state = {"restarts": 0, "started": time.time(),
          "last_exit": None, "pid": None}


# ------------------------------------------------------------------ 掃描器

def _run_scanner() -> None:
    """跑它，死了就重啟。退避是為了不要在 WAF 擋我們時猛敲。"""
    delay = 5.0
    while True:
        p = subprocess.Popen([sys.executable, SCANNER], cwd=REPO)
        _state["pid"] = p.pid
        rc = p.wait()
        _state["pid"] = None
        _state["last_exit"] = rc
        _state["restarts"] += 1
        print("[service] scanner 結束 rc=%s，%.0fs 後重啟" % (rc, delay),
              flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 300.0)


def _prune() -> None:
    """只留最近 KEEP_DAYS 天的每日 CSV。listings.csv 與 universe.json
    不動 —— 它們是累積的而且很小（109 KB / 50 KB）。"""
    while True:
        try:
            cut = time.time() - KEEP_DAYS * 86400
            for f in os.listdir(OUT_DIR):
                if not f.startswith("scan_"):
                    continue
                p = os.path.join(OUT_DIR, f)
                if os.path.getmtime(p) < cut:
                    os.remove(p)
                    print("[service] 刪掉過期的 %s" % f, flush=True)
        except Exception as e:                               # noqa: BLE001
            print("[service] prune 失敗（不影響掃描）: %r" % e, flush=True)
        time.sleep(PRUNE_EVERY_SEC)


# ---------------------------------------------------------------- 檔案端點

def _served(name: str) -> bool:
    """只端出**消費者真的會讀的資料檔**。

    第一次端到端測試時這裡沒有白名單，於是拉取端把整個目錄搬回來 ——
    `runner.log`、`runner_v5b.err`，以及**拉取端自己的狀態檔**。
    那不只是浪費：把 log 端出去等於把一個會無限長大的東西放進同步路徑，
    而狀態檔被同步回來會變成一個自我指涉的謎題。
    白名單而不是黑名單 —— 之後 scanner 多寫一種檔時，預設是「不端出」。
    """
    return name == "listings.csv" or (name.startswith("scan_")
                                      and name.endswith(".csv"))


def _listing() -> list:
    out = []
    try:
        for f in sorted(os.listdir(OUT_DIR)):
            p = os.path.join(OUT_DIR, f)
            if os.path.isfile(p) and _served(f):
                out.append({"name": f, "size": os.path.getsize(p),
                            "mtime": os.path.getmtime(p)})
    except FileNotFoundError:
        pass
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "arb-scan/1"

    def _auth(self) -> bool:
        if not TOKEN:
            return True          # 沒設 token 就不檢查（本機測試用）
        if self.headers.get("X-Scan-Token") == TOKEN:
            return True
        self.send_error(401, "bad token")
        return False

    def _json(self, obj) -> None:
        import json
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):                                        # noqa: N802
        u = urlparse(self.path)
        if u.path == "/health":
            # health 不要 token：Railway 自己要探活。它也不洩漏任何東西 ——
            # 只有「掃描器活著嗎、檔案多大、多久以前寫的」。
            files = _listing()
            newest = max((f["mtime"] for f in files), default=0)
            return self._json({
                "ok": _state["pid"] is not None and files != [],
                "scanner_alive": _state["pid"] is not None,
                "restarts": _state["restarts"],
                "last_exit": _state["last_exit"],
                "uptime_sec": round(time.time() - _state["started"]),
                "files": len(files),
                "newest_age_sec": round(time.time() - newest) if newest else None,
                "keep_days": KEEP_DAYS,
            })
        if not self._auth():
            return
        if u.path == "/files":
            return self._json(_listing())
        if u.path == "/file":
            name = (parse_qs(u.query).get("name") or [""])[0]
            # 路徑穿越：白名單而不是黑名單。`..` 這種東西不該靠「檢查有沒有」
            # 來擋，該靠「只允許長這樣」。
            # 白名單擋兩件事：路徑穿越（`..` 不合 SAFE_NAME），以及
            # 「這個目錄裡任何其他東西」（_served）。/files 已經濾過，
            # 但 /file 是可以直接被叫的，所以它要自己擋。
            if not SAFE_NAME.match(name) or not _served(name):
                return self.send_error(400, "bad name")
            p = os.path.join(OUT_DIR, name)
            if not os.path.isfile(p):
                return self.send_error(404, "no such file")
            size = os.path.getsize(p)
            start = 0
            rng = self.headers.get("Range", "")
            m = re.match(r"bytes=(\d+)-$", rng.strip()) if rng else None
            if m:
                start = int(m.group(1))
                if start > size:
                    # 本機的 offset 比遠端的檔還大 = 遠端輪替過或被清掉了。
                    # 回 416 讓拉取端知道要從頭來，不要安靜地回 0 位元組
                    # （那會被讀成「沒有新資料」）。
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
            n = size - start
            self.send_response(206 if m else 200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(n))
            if m:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, size - 1, size))
            self.end_headers()
            if n <= 0:
                return
            with open(p, "rb") as fh:
                fh.seek(start)
                left = n
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return
        self.send_error(404)

    def log_message(self, *a):                               # noqa: N802
        pass


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    # 2026-09-13：第一次煙霧測試就在本機起了**第二個掃描器**，而本機已經有
    # 一個常駐的 —— 兩支打同一個公開 API，正是 2026-09-03 那個
    # duplicate-scanner bug，也正是我們今天搬家的原因。測端點不需要掃描器，
    # 所以給它一個旗標，而不是靠「記得等它自己逾時」。
    ap.add_argument("--no-scanner", action="store_true",
                    help="只跑檔案端點，不啟動掃描器（測試用）")
    args = ap.parse_args(argv)
    os.makedirs(OUT_DIR, exist_ok=True)
    if not os.path.exists(SCANNER):
        print("找不到 %s" % SCANNER, flush=True)
        return 2
    if args.no_scanner:
        print("[service] --no-scanner：只跑端點", flush=True)
    else:
        threading.Thread(target=_run_scanner, daemon=True).start()
    threading.Thread(target=_prune, daemon=True).start()
    print("[service] 監聽 :%d｜掃描器 %s｜保留 %d 天｜token %s"
          % (PORT, SCANNER, KEEP_DAYS, "有" if TOKEN else "**沒設**"),
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
