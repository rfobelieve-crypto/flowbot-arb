# -*- coding: utf-8 -*-
"""把 Railway 上掃描器寫的 CSV 拉回本機（本機發起，出站 only）。

    python tools/scan_pull.py                  # 拉一次
    python tools/scan_pull.py --status         # 只看狀態不拉

環境變數：SCAN_URL（https://…up.railway.app）、SCAN_TOKEN

===========================================================================
三個設計決定，每一個都擋一個具體的病
===========================================================================
**一、byte offset 續傳。** 每日 CSV 約 100 MB 而且是 append-only，所以只拉
新增的那幾百 KB。整檔重抓是 100 MB x 144 次/天。

**二、寫成 `_rw` 後綴，永不碰本機掃描器寫的檔。**
    遠端 scan_v5_20260913.csv  ->  本機 scan_v5_20260913_rw.csv
消費者用 `scan_*.csv` 的 glob，所以兩邊都撿得到。而分開命名有第二個理由，
它比「避免覆蓋」重要：**Railway 的列跟本機的列腿差不一樣**（§1.39 量到本機
是 53 秒，而那個數字的主成分是 196 次序列呼叫 x RTT，換一台機器就變）。
把來源寫進檔名，未來要分開算的時候分得開 —— 不然它就是又一個
「同一個容器裡放了兩種語意」。

**三、拉取斷掉必須看得見。** 旗標的判準是**本機的位元組有沒有在長**，
不是「遠端有沒有回 200」。Railway 活著但拉取斷了，本機資料會靜靜地停在
昨天，而十個消費者一個都不會報錯（mistake.md 2026-08-29：計數類的下游對
這種病的反應不是壞掉，是安靜地給出一個看起來合理的錯誤數字）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT_DIR = os.path.join(REPO, "engine", "logs", "scan")
STATE = os.path.join(OUT_DIR, ".pull_state.json")
FLAG = os.path.join(REPO, "results", "scan_pull_last.json")
TIMEOUT = 60
CHUNK = 1 << 20


def local_name(remote: str) -> str:
    """遠端 scan_v5_X.csv -> 本機 scan_v5_X_rw.csv（見檔頭第二點）。"""
    base, ext = os.path.splitext(remote)
    return base + "_rw" + ext


def load_state() -> dict:
    try:
        return json.load(io.open(STATE, encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return {}


def save_state(st: dict) -> None:
    tmp = STATE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, STATE)


def write_flag(ok: bool, reason: str, added: int, files: int) -> None:
    os.makedirs(os.path.dirname(FLAG), exist_ok=True)
    tmp = FLAG + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"ok": bool(ok), "reason": reason[:300],
                   "asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "ts": time.time(), "bytes_added": added,
                   "files": files}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, FLAG)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--url", default=os.environ.get("SCAN_URL", ""))
    ap.add_argument("--token", default=os.environ.get("SCAN_TOKEN", ""))
    args = ap.parse_args(argv)
    if not args.url:
        print("沒有 SCAN_URL —— 設環境變數或用 --url")
        return 2
    url = args.url.rstrip("/")
    h = {"X-Scan-Token": args.token} if args.token else {}
    os.makedirs(OUT_DIR, exist_ok=True)
    st = load_state()

    try:
        hz = requests.get(url + "/health", timeout=TIMEOUT).json()
        print("遠端：掃描器 %s｜重啟 %s 次｜%s 個檔｜最新 %s 秒前"
              % ("活著" if hz.get("scanner_alive") else "**死了**",
                 hz.get("restarts"), hz.get("files"),
                 hz.get("newest_age_sec")))
        files = requests.get(url + "/files", headers=h,
                             timeout=TIMEOUT).json()
    except Exception as e:                                   # noqa: BLE001
        msg = "遠端讀不到：%r" % e
        print(msg)
        write_flag(False, msg, 0, 0)
        return 1

    if args.status:
        for f in files:
            got = st.get(f["name"], 0)
            print("  %-28s 遠端 %11s  本機 %11s  差 %s"
                  % (f["name"], format(f["size"], ","), format(got, ","),
                     format(f["size"] - got, ",")))
        return 0

    added = 0
    problems = []
    for f in files:
        name, size = f["name"], f["size"]
        off = int(st.get(name, 0))
        if off > size:
            # 遠端比本機小 = 遠端輪替過或被兩天的保留策略刪了再重建。
            # **不要從 0 重抓然後 append** —— 那會把整份重複寫進本機檔。
            # 改成換一個本機檔名重新開始，並大聲說出來。
            problems.append("%s 遠端變小（%s < %s），換檔重來"
                            % (name, format(size, ","), format(off, ",")))
            n = 1
            while os.path.exists(os.path.join(
                    OUT_DIR, local_name(name).replace("_rw", "_rw%d" % n))):
                n += 1
            st.pop(name, None)
            name_local = local_name(name).replace("_rw", "_rw%d" % n)
            off = 0
        else:
            name_local = local_name(name)
        if off == size:
            continue
        try:
            r = requests.get(url + "/file", params={"name": name},
                             headers=dict(h, Range="bytes=%d-" % off),
                             timeout=TIMEOUT, stream=True)
            if r.status_code == 416:
                problems.append("%s 回 416（offset 超過遠端大小）" % name)
                continue
            r.raise_for_status()
            p = os.path.join(OUT_DIR, name_local)
            got = 0
            with open(p, "ab") as fh:
                for chunk in r.iter_content(CHUNK):
                    if chunk:
                        fh.write(chunk)
                        got += len(chunk)
            st[name] = off + got
            added += got
            print("  %-28s +%s bytes -> %s"
                  % (name, format(got, ","), name_local))
        except Exception as e:                               # noqa: BLE001
            problems.append("%s 拉取失敗：%r" % (name, e))

    save_state(st)
    ok = not problems and (added > 0 or not files)
    reason = ("；".join(problems) if problems else
              "拉了 %s bytes、%d 個檔" % (format(added, ","), len(files)))
    if not problems and added == 0:
        # 遠端沒有新資料。掃描器每 600 秒才寫一輪，所以這在兩輪之間是正常的
        # —— 但連續好幾次都 0 就不正常，而那個判斷交給 freshness 的 age。
        reason = "沒有新位元組（遠端最新 %s 秒前）" % hz.get("newest_age_sec")
        ok = True
    print("\n%s" % reason)
    write_flag(ok, reason, added, len(files))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
