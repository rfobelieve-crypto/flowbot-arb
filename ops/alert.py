# -*- coding: utf-8 -*-
"""HMM 看護的告警投遞層 —— Discord 主、arb 自己的 Telegram 備，送不出去會變成一盞紅燈。

===========================================================================
為什麼 arb 有自己的一份（2026-09-15，HMM 整條搬進 arb）
===========================================================================
看護原本住在 flow_system，理由是告警管線 `research/ops/notify.py` 住在那裡，
而 arb 的硬規則是**永不 import、永不讀 flow_system**（CLAUDE.md §1）。
使用者決定 HMM 的東西全部搬到 arb、從這邊開 session，所以投遞層必須是
arb 自己的一份，憑證也是 arb 自己的：

    ARB_DISCORD_WEBHOOK_URL   主管道（使用者 2026-09-15 選「Discord 照舊」）
    ARB_TG_BOT_TOKEN          備援，引擎的控制通道本來就在用這組
    ARB_TG_CHAT_ID

讀取順序：os.environ -> `arb/.env` -> `arb/engine/.env`。
**不讀 flow_system 的 .env**，就算裡面有同一個 webhook —— 那一步正是隔離規則要擋的。

算術從 flow_system 的 notify.py 逐行搬過來，三個設計決定照舊（那邊有事故經過）：

1. **設定一律 env -> .env 回退。** 排程啟動的 .bat 不會載 .env；
   只讀 os.environ 的舊版因此靜默 8 天沒送出任何告警（flow_system 2026-09-13）。
2. **「沒有設定任何管道」不是合法狀態。** 回 `configured=False` 並讓旗標 not-ok。
3. **投遞結果寫成旗標** `results/hmm_alert_last.json`。flow_system 的新鮮度看板
   從外面讀它（讀的方向是 flow_system -> arb，沒反），所以「告警送不出去」
   不會只有告警自己知道。

Discord webhook 是**頻道層的憑證**，只放 .env（`.env*` 已被 .gitignore 擋），
不進對話、不進 argv、不進 log。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ARB = os.path.dirname(HERE)
FLAG = os.path.join(ARB, "results", "hmm_alert_last.json")
ENV_FILES = (os.path.join(ARB, ".env"), os.path.join(ARB, "engine", ".env"))

DISCORD_LIMIT = 1900            # 實際上限 2000，留邊給前後綴
RETRIES = 3


def _dotenv(path: str) -> dict:
    """KEY=VALUE 解析，不需要 python-dotenv。"""
    out = {}
    if not os.path.exists(path):
        return out
    # errors="replace"：arb/engine/.env 的中文註解是記事本存的 cp950，不是 UTF-8。
    # 嚴格解碼會拋 UnicodeDecodeError（不是 OSError），整支看護就在讀設定時死掉 ——
    # 而金鑰那幾行是 ASCII，換掉壞字元不影響它們（2026-09-15 搬家時實測到）。
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except (OSError, ValueError):
        pass
    return out


_ENV_CACHE = None


def cfg(key: str, default: str = "") -> str:
    """env 優先，然後 arb/.env，然後 arb/engine/.env。"""
    global _ENV_CACHE
    v = os.environ.get(key)
    if v:
        return v
    if _ENV_CACHE is None:
        _ENV_CACHE = {}
        for p in reversed(ENV_FILES):           # 前面的檔案優先 -> 後讀覆蓋
            _ENV_CACHE.update(_dotenv(p))
    return _ENV_CACHE.get(key, default)


def _post_discord(url: str, text: str) -> tuple:
    body = json.dumps({"content": text[:DISCORD_LIMIT],
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "flowbot-arb-hmm/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            # webhook 成功是 204 No Content
            return (200 <= r.status < 300), "HTTP %d" % r.status
    except urllib.error.HTTPError as e:
        return False, "HTTP %d %s" % (e.code, (e.reason or "")[:60])
    except Exception as e:                              # noqa: BLE001
        return False, type(e).__name__ + ": " + str(e)[:80]


def _post_telegram(token: str, chat: str, text: str) -> tuple:
    body = json.dumps({"chat_id": chat, "text": text[:4000],
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token,
        data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return (200 <= r.status < 300), "HTTP %d" % r.status
    except urllib.error.HTTPError as e:
        return False, "HTTP %d" % e.code             # 不印 URL：裡面有 token
    except Exception as e:                              # noqa: BLE001
        return False, type(e).__name__


def channels() -> list:
    """有設定的管道。空 list 代表**沒有任何人收得到告警**。"""
    out = []
    if cfg("ARB_DISCORD_WEBHOOK_URL"):
        out.append("discord")
    if cfg("ARB_TG_BOT_TOKEN") and cfg("ARB_TG_CHAT_ID"):
        out.append("telegram")
    return out


def send(text: str, source: str = "hmm_watch", write_flag: bool = True) -> dict:
    """Discord 先送；Discord 沒設定或送不出去才走 Telegram。任一成功即算送達。"""
    res = {"asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "source": source, "tried": {}, "delivered": False,
           "configured": bool(channels())}

    url = cfg("ARB_DISCORD_WEBHOOK_URL")
    if url:
        for i in range(RETRIES):
            ok, why = _post_discord(url, text)
            res["tried"]["discord"] = why
            if ok:
                res["delivered"] = True
                break
            if i < RETRIES - 1:
                time.sleep(3 * (i + 1))
    else:
        # 主管道沒設定要被看見，就算備援送達了 —— 否則使用者以為 Discord 會響。
        res["tried"]["discord"] = "未設定 ARB_DISCORD_WEBHOOK_URL"

    tok, chat = cfg("ARB_TG_BOT_TOKEN"), cfg("ARB_TG_CHAT_ID")
    if tok and chat and not res["delivered"]:
        for i in range(RETRIES):
            ok, why = _post_telegram(tok, chat, text)
            res["tried"]["telegram"] = why
            if ok:
                res["delivered"] = True
                break
            if i < RETRIES - 1:
                time.sleep(3 * (i + 1))

    if write_flag:
        payload = dict(res)
        # **ok 的語意是「主管道送得出去」。** 備援送達但 Discord 沒設定仍是 not-ok：
        # 使用者選的是 Discord，只在 Telegram 響等於換了頻道而沒人知道。
        payload["ok"] = bool(res["delivered"] and url)
        payload["reason"] = (
            "已送達：" + ",".join(k for k, v in res["tried"].items()
                                  if v.startswith("HTTP 2"))
            if payload["ok"] else
            ("**沒有設定任何告警管道**" if not res["configured"]
             else "**主管道不通**：" + "；".join(
                 "%s=%s" % (k, v) for k, v in res["tried"].items())))
        try:
            os.makedirs(os.path.dirname(FLAG), exist_ok=True)
            with open(FLAG, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return res


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for k in ("ARB_DISCORD_WEBHOOK_URL", "ARB_TG_BOT_TOKEN", "ARB_TG_CHAT_ID"):
        src = ("env" if os.environ.get(k) else
               next((os.path.relpath(p, ARB) for p in ENV_FILES
                     if _dotenv(p).get(k)), "—"))
        print("%-26s %s" % (k, src))            # 只印來源，不印值
    print("channels:", channels() or "**無**")
