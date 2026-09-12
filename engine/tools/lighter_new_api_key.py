# -*- coding: utf-8 -*-
"""產生一把 Lighter API 金鑰、註冊上鏈、寫進 .env —— **你自己跑，我看不到任何祕密**。

為什麼需要這支：Lighter 的網頁只顯示**公鑰**，私鑰在建立當下若沒存就再也拿
不回來。而 SDK 的註冊函式簽名是

    change_api_key(eth_private_key, new_pubkey, ..., api_key_index)

—— 它需要**你錢包的 L1 私鑰**。那把鑰匙控制整個錢包與裡面的錢，所以它絕不
能經過對話、log、命令列參數或任何檔案。這支腳本用 getpass 讀它：不回顯、
不進 shell 歷史、只存在於記憶體，用完就沒了。

**這支腳本會做四件事**
  1. 在本機產生金鑰對（`lighter.create_api_key()`）
  2. 用你的錢包私鑰把**公鑰**註冊到指定的 api_key_index
  3. 輪詢公開端點，等鏈上真的出現那把公鑰（**判準是產物不是回傳值**）
  4. 備份 .env，把私鑰與 index 寫進去，最後用 SDK 自己的 check_client() 驗

**它不會印出私鑰，也不會印出你的錢包私鑰。** 只印公鑰與 index。

跑法（在 arb/engine 底下）：
    python tools/lighter_new_api_key.py            # 互動，預設挑一個沒用過的 index
    python tools/lighter_new_api_key.py --index 3  # 指定 index
    python tools/lighter_new_api_key.py --dry      # 只產生金鑰對、不註冊不寫檔
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ENV = HERE.parent / ".env"
API = "https://mainnet.zklighter.elliot.ai"
CHAIN_ID = 304
UA = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 Chrome/126"}


def chain_keys(account_index: int, tries: int = 5):
    """鏈上目前註冊了哪些 api key（公開端點，帶退避重試）。"""
    url = f"{API}/api/v1/apikeys?account_index={account_index}"
    for t in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read())
            return {int(k["api_key_index"]): (k.get("public_key") or "").lower()
                    for k in (d.get("api_keys") or [])}
        except Exception as e:                                  # noqa: BLE001
            if t == tries - 1:
                raise
            time.sleep(8 * (t + 1))


def env_lines():
    return ENV.read_text(encoding="utf-8").splitlines()


def env_get(key: str):
    for ln in env_lines():
        s = ln.strip()
        if s.startswith(key + "="):
            return s.split("=", 1)[1].strip()
    return None


def env_set(pairs: dict):
    """只改指定的 key，其餘一字不動（read-then-update，見 mistake.md 2026-04-19）。"""
    out, seen = [], set()
    for ln in env_lines():
        s = ln.strip()
        hit = next((k for k in pairs if s.startswith(k + "=")), None)
        if hit:
            out.append(f"{hit}={pairs[hit]}")
            seen.add(hit)
        else:
            out.append(ln)
    for k, v in pairs.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")


def main(args):
    acct = int(env_get("LIGHTER_ACCOUNT_INDEX") or 0)
    assert acct, "LIGHTER_ACCOUNT_INDEX 沒填，先補上再跑"
    print(f"帳戶 index {acct}")

    before = chain_keys(acct)
    print("鏈上現有金鑰：" + (", ".join(f"idx{i}={p[:12]}…"
                                  for i, p in sorted(before.items())) or "（無）"))

    idx = args.index
    if idx is None:
        idx = next(i for i in range(1, 200) if i not in before)
        print(f"自動挑了一個沒用過的 index：{idx}")
    else:
        note = "（會覆蓋那個 index 上的舊金鑰）" if idx in before else ""
        print(f"使用指定的 index：{idx} {note}")

    from lighter import create_api_key, SignerClient
    priv, pub, err = create_api_key()
    assert err is None, f"產生金鑰失敗：{err}"
    print()
    print("已在本機產生金鑰對。")
    print(f"  公鑰（可公開）：{pub}")
    print(f"  私鑰：**不印出**（長度 {len(priv)}）")

    if args.dry:
        print()
        print("=== --dry：沒有註冊、沒有寫檔 ===")
        return

    print()
    print("接下來要把**公鑰**註冊上鏈，這需要你錢包的 L1 私鑰。")
    print("  · 輸入時不會回顯、不會進 shell 歷史、不會寫進任何檔案")
    print("  · 它只存在於這個行程的記憶體，用完就沒了")
    print("  · 如果你不想在這裡輸入，按 Ctrl+C 中止，改用網頁 UI")
    eth = getpass.getpass("錢包 L1 私鑰（0x… 64 hex）：").strip()
    if not eth:
        print("沒有輸入，中止。"); return

    # change_api_key 是 **async def**（SDK signer_client.py:470）。
    # 同步呼叫它只會拿到一個 coroutine、一個位元都不送，而輪詢會老實地失敗
    # 80 秒 —— 看起來像「Lighter 很慢」而不是「我們沒送」。
    # close() 也是 async，所以整段包在一個 async 函式裡跑。
    async def _register():
        signer = SignerClient(url=API, account_index=acct,
                              api_private_keys={idx: priv}, chain_id=CHAIN_ID)
        try:
            print("送出註冊交易…")
            resp, err = await signer.change_api_key(
                eth_private_key=eth, new_pubkey=pub, api_key_index=idx)
            return resp, err
        finally:
            try:
                await signer.close()
            except Exception:                                   # noqa: BLE001
                pass

    res, sign_err = asyncio.run(_register())
    del eth                                   # 不留在記憶體裡超過必要
    if sign_err is not None:
        print(f"  ★ 簽章/送出失敗：{str(sign_err)[:200]}")
        print("    .env **沒有被改動**。最常見的原因是 L1 私鑰貼錯。")
        return
    print(f"  交易回應：{str(res)[:160]}")

    # 判準是**產物**：鏈上真的出現那把公鑰，不是交易回了什麼
    print("等鏈上出現（每 8 秒查一次，最多 10 次）…")
    want = pub.lower().removeprefix("0x")
    for i in range(10):
        time.sleep(8)
        now = chain_keys(acct)
        got = (now.get(idx) or "").removeprefix("0x")
        if got == want:
            print(f"  第 {i+1} 次：**已上鏈**，idx{idx} = {got[:16]}…")
            break
        print(f"  第 {i+1} 次：idx{idx} = {got[:16] or '(無)'}… 還沒對上")
    else:
        print("  ★ 鏈上還沒出現。交易可能還在排隊，或註冊沒成功。")
        print("    .env **不會**被改動 —— 稍後手動重跑這支，或改用網頁 UI。")
        return

    bak = ENV.with_suffix(".env.bak-%s" % time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(ENV, bak)
    env_set({"LIGHTER_API_KEY_INDEX": str(idx),
             "LIGHTER_API_PRIVATE_KEY": priv})
    print()
    print(f".env 已更新（備份在 {bak.name}）")

    err2 = SignerClient(url=API, account_index=acct,
                        api_private_keys={idx: priv},
                        chain_id=CHAIN_ID).check_client()
    print("check_client()：" + ("**通過，可以簽單了**" if err2 is None
                               else f"**仍然失敗** —— {str(err2)[:120]}"))
    print()
    print("最後跑一次： python tools/check_env.py")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--index", type=int, default=None,
                   help="要用的 api_key_index（預設自動挑一個沒用過的）")
    p.add_argument("--dry", action="store_true",
                   help="只產生金鑰對，不註冊不寫檔")
    main(p.parse_args())
