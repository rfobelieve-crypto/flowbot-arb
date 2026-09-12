#!/usr/bin/env python3
"""Check the credentials in .env WITHOUT printing any of them.

Fill one field, run this, look for a green line. It answers three questions
a wrong paste otherwise only answers at 3am on a live engine:

  1. Is the value even the right SHAPE? An Ethereum private key is 64 hex
     characters; an address is 40. They look equally plausible in a config
     file and one of them cannot sign.
  2. Does the account actually EXIST on the chain we are about to point it
     at? Lighter mainnet and the Robinhood chain are separate deployments
     with separate accounts, and config.py silently falls back from
     LIGHTER_RH_* to LIGHTER_* when the former is missing.
  3. Is the HL agent wallet still AUTHORISED, and until when? Agents expire.
     A signature that stops working is not a message the engine can explain.

Secrets are never printed -- only lengths, shapes and public identifiers.

Run:  python tools/check_env.py            (engine/.env)
      python tools/check_env.py --root     (also the repo-root .env, M1 keys)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
ROOT = ENGINE.parent

OK, WARN, BAD, SKIP = "  OK  ", " WARN ", " BAD  ", " ---- "
HEX = re.compile(r"^(0x)?[0-9a-fA-F]+$")


def load(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v and not v.startswith("<"):
            out[k.strip()] = v
    return out


def say(status: str, name: str, msg: str) -> None:
    print(f"[{status}] {name:26s} {msg}")


def hexlen(v: str) -> int:
    return len(v[2:]) if v.lower().startswith("0x") else len(v)


def check_hl(env: dict) -> None:
    key, addr = env.get("HL_PRIVATE_KEY"), env.get("HL_ACCOUNT_ADDRESS")
    if not key:
        say(SKIP, "HL_PRIVATE_KEY", "未填 — Entropy(io) 那條腿簽不了單")
    elif not HEX.match(key):
        say(BAD, "HL_PRIVATE_KEY", "不是 hex 字串")
    elif hexlen(key) == 40:
        say(BAD, "HL_PRIVATE_KEY",
            "40 hex = 這是一個「地址」，不是私鑰。API 頁面上 Generate 之後"
            "顯示的那串 64 hex 才是私鑰（只顯示一次）")
    elif hexlen(key) != 64:
        say(BAD, "HL_PRIVATE_KEY", f"{hexlen(key)} hex，以太坊私鑰要 64")
    else:
        say(OK, "HL_PRIVATE_KEY", "64 hex，形狀正確")
    if not addr:
        say(SKIP, "HL_ACCOUNT_ADDRESS", "未填")
    elif hexlen(addr) != 40 or not addr.lower().startswith("0x"):
        say(BAD, "HL_ACCOUNT_ADDRESS", "應為 0x + 40 hex")
    else:
        say(OK, "HL_ACCOUNT_ADDRESS", addr)
    if key and addr and key.lower().lstrip("0x") == addr.lower().lstrip("0x"):
        say(BAD, "HL_PRIVATE_KEY", "★ 跟 HL_ACCOUNT_ADDRESS 是同一個值")

    if not addr or hexlen(addr) != 40:
        return
    agent = None
    try:
        from eth_account import Account
        agent = Account.from_key(key).address if key and hexlen(key) == 64 else None
    except ImportError:
        say(SKIP, "agent 位址推導", "eth_account 未安裝（pip install -r "
                                    "requirements-live.txt 之後才驗得了）")
    except Exception as e:
        say(BAD, "agent 位址推導", f"私鑰無法載入: {type(e).__name__}")
    try:
        ags = hl_info({"type": "extraAgents", "user": addr.lower()}) or []
    except Exception as e:
        say(WARN, "HL extraAgents", f"查詢失敗 {type(e).__name__}")
        return
    if not ags:
        say(BAD, "HL agent 授權", "這個地址底下沒有任何已授權的 agent wallet")
        return
    import datetime as dt
    for a in ags:
        until = dt.datetime.utcfromtimestamp(int(a.get("validUntil", 0)) / 1000)
        left = (until - dt.datetime.utcnow()).days
        mine = agent and str(a.get("address", "")).lower() == agent.lower()
        status = OK if (mine or agent is None) else SKIP
        say(status, "HL agent", f"{a.get('name')!r} {a.get('address')} "
                                f"到期 {until:%Y-%m-%d %H:%M}Z（剩 {left} 天）"
                                + ("  ← 就是 .env 這把" if mine else ""))
    if agent and not any(str(a.get("address", "")).lower() == agent.lower()
                         for a in ags):
        say(BAD, "HL agent 授權",
            "★ .env 這把 key 推導出來的位址不在已授權清單裡 —— 授權交易沒簽，"
            "或簽的是別把")


def hl_info(payload: dict):
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


LIGHTER_HOSTS = {
    "mainnet": "https://mainnet.zklighter.elliot.ai",
    "robinhood": "https://api.rh.lighter.xyz",
}
# chain_id 與 entropy_arb/config.py 的 LIGHTER_PROFILES 同值（刻意重寫一次：
# check_env 要能在 SDK 與 engine 都還沒裝好時獨立跑）。對不上就是這裡要改。
LIGHTER_CHAIN_IDS = {"mainnet": 304, "robinhood": 466324}


def check_lighter(env: dict, prefix: str, want: str, needed_by: str) -> None:
    idx = env.get(prefix + "ACCOUNT_INDEX")
    kid = env.get(prefix + "API_KEY_INDEX")
    key = env.get(prefix + "API_PRIVATE_KEY")
    label = prefix.rstrip("_")
    if not any((idx, kid, key)):
        extra = ("（config.py 會回退去讀 LIGHTER_* —— 那是拿 mainnet 憑證打 "
                 "RH 的 host）" if prefix.endswith("RH_") else "")
        say(SKIP, label, f"三格全空 — {needed_by} 跑不了{extra}")
        return
    for name, v in ((prefix + "ACCOUNT_INDEX", idx), (prefix + "API_KEY_INDEX", kid)):
        if not v:
            say(BAD, name, "未填")
        elif not v.lstrip("-").isdigit():
            say(BAD, name, "不是數字（佔位符沒換掉？）")
        else:
            say(OK, name, v)
    if not key:
        say(BAD, prefix + "API_PRIVATE_KEY", "未填")
    elif not HEX.match(key) or hexlen(key) != 80:
        say(BAD, prefix + "API_PRIVATE_KEY", f"{hexlen(key)} hex，Lighter 要 80")
    else:
        say(OK, prefix + "API_PRIVATE_KEY", "80 hex，形狀正確")
    if not idx or not idx.isdigit():
        return
    # "查不到" and "查不成" are different answers and must not be merged.
    # Reporting a failed request as a negative result is the optimistic
    # assumption this project bans everywhere else; it belongs banned here
    # too, because the conclusion it invites is "wrong chain, go fix it".
    seen = {}
    for name, base in LIGHTER_HOSTS.items():
        req = urllib.request.Request(
            f"{base}/api/v1/account?by=index&value={idx}",
            headers={"User-Agent": "entropy-arb/check_env",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
            accts = d.get("accounts") or []
            if accts:
                seen[name] = ("found", accts[0].get("collateral"))
            else:
                seen[name] = ("absent", None)
        except urllib.error.HTTPError as e:
            # 400 from the chain that does not have this account is a real
            # "absent"; anything else is us failing to ask.
            seen[name] = ("absent", None) if e.code == 400 else ("error", f"HTTP {e.code}")
        except Exception as e:                                  # noqa: BLE001
            seen[name] = ("error", type(e).__name__)
    status, detail = seen.get(want, ("error", "not queried"))
    other = [n for n, (st, _) in seen.items() if st == "found" and n != want]
    if status == "found":
        say(OK, label + " 帳號", f"index {idx} 存在於 {want}（抵押 {detail}）")
    elif status == "error":
        say(WARN, label + " 帳號",
            f"無法查證（{detail}）— 這不是「帳號不存在」，是查詢沒成功。稍後重跑")
    elif other:
        say(BAD, label + " 帳號",
            f"index {idx} 不在 {want}，卻存在於 {other[0]} ★ 這一組放錯前綴了")
    else:
        say(BAD, label + " 帳號", f"index {idx} 在 {want} 上不存在")

    # ── 簽章能力（2026-09-12 補）────────────────────────────────────────
    # 上面幾關驗的是**形狀**與**帳戶存在**，不是「這把金鑰簽不簽得動」。
    # 2026-09-12 它們全報 OK，而真的送單時 SignerClient 回
    # 「private key does not match the one on Lighter ... on api key 4」。
    # 一個不能下單的設定看起來是綠的 —— 這個 repo 反覆出現的同一個形狀。
    # 所以這一關直接呼叫 SDK 自己的 check_client()，不自己判斷。
    if status != "found" or not key or not kid:
        return
    try:
        from lighter import SignerClient
    except ImportError:
        say(WARN, label + " 簽章",
            "SDK 沒裝（pip install -r requirements-live.txt）—— "
            "**簽章能力未驗，上面的 OK 不代表下得了單**")
        return
    base = LIGHTER_HOSTS[want]
    chain = LIGHTER_CHAIN_IDS[want]
    try:
        signer = SignerClient(url=base, account_index=int(idx),
                              api_private_keys={int(kid): key},
                              chain_id=chain)
        err = signer.check_client()
    except Exception as e:                                      # noqa: BLE001
        say(BAD, label + " 簽章", f"建不起 signer：{type(e).__name__}: {e}"[:150])
        return
    if err is None:
        say(OK, label + " 簽章", f"api key {kid} 能簽（chain_id {chain}）")
    else:
        msg = str(err).replace(key, "<私鑰已遮蔽>")
        say(BAD, label + " 簽章",
            f"api key {kid} **簽不動** —— {msg[:120]}"
            " ★ 去 Lighter 重新產生 API key，或改對 API_KEY_INDEX")


def check_tg(env: dict) -> None:
    tok, chat = env.get("ARB_TG_BOT_TOKEN"), env.get("ARB_TG_CHAT_ID")
    if not tok and not chat:
        say(SKIP, "ARB_TG_*", "未填 — 控制通道只走落地命令檔 control.cmd（可接受）")
        return
    say(OK if tok and ":" in tok else BAD, "ARB_TG_BOT_TOKEN",
        "形狀像 token" if tok and ":" in tok else "應為 <數字>:<字串>")
    say(OK if chat and chat.lstrip("-").isdigit() else BAD, "ARB_TG_CHAT_ID",
        chat or "未填")


def check_m1(env: dict) -> None:
    say(OK if env.get("HL_ACCOUNT_ADDRESS") else BAD, "HL_ACCOUNT_ADDRESS",
        "HL 的 userFills 是公開的，有地址就讀得到")
    say(OK if env.get("LIGHTER_ACCOUNT_INDEX") else BAD, "LIGHTER_ACCOUNT_INDEX",
        "公開索引")
    for venue, names in (
            ("OKX", ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")),
            ("Bitget", ("BITGET_API_KEY", "BITGET_API_SECRET",
                        "BITGET_API_PASSPHRASE")),
            ("Binance", ("BINANCE_API_KEY", "BINANCE_API_SECRET"))):
        have = [n for n in names if env.get(n)]
        if not have:
            say(SKIP, venue, "未填 — fee_receipts.py 會跳過這一所，不會壞")
        elif len(have) < len(names):
            say(BAD, venue, f"只填了 {len(have)}/{len(names)} 格")
        else:
            say(OK, venue, "三格齊全（記得是唯讀權限＋不勾提幣）")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", action="store_true",
                    help="也檢查 repo 根目錄的 .env（M1 回執用的唯讀 key）")
    args = ap.parse_args()

    env = load(ENGINE / ".env")
    print(f"\n=== 引擎憑證  {ENGINE / '.env'} ===")
    if not env:
        print("  （檔案不存在或全空）")
    print("\n-- [1] Hyperliquid / Entropy io  （NBIS 需要）--")
    check_hl(env)
    print("\n-- [2] Lighter mainnet  （NBIS 與 NVDA_LL 都需要）--")
    check_lighter(env, "LIGHTER_", "mainnet", "兩個配對")
    print("\n-- [3] Lighter Robinhood 鏈  （NVDA_LL 需要）--")
    check_lighter(env, "LIGHTER_RH_", "robinhood", "NVDA_LL")
    print("\n-- [4] 控制通道（選配）--")
    check_tg(env)

    if args.root:
        renv = load(ROOT / ".env")
        print(f"\n=== M1 成交回執  {ROOT / '.env'} ===")
        check_m1(renv)
    print()


if __name__ == "__main__":
    sys.exit(main())
