# -*- coding: utf-8 -*-
"""候選標的在 HL 上有沒有對沖腿 —— 沒有就沒有這條線。

    python arblib/hedge_leg_check.py

HMM 的構造是「掛單 Lighter、吃單對沖 HL」，所以**同一個資產必須兩邊都掛**。
markout 篩選（sweep_markout.json）只看 Lighter 一側，它排出來的前段很可能是
Lighter 特有的市場 —— 那些在 HL 上不存在，於是那一段名單對 HMM 是空的。

===========================================================================
第一版有兩個 bug，留檔因為它們比結論有用（2026-09-14）
===========================================================================
第一版用**精確字串比對**，於是 ANTHROPIC 報「沒有」。而那是錯的，
因為 `config_ANTH.yaml` 就是 `dex: io` ＋ Lighter 側 `symbol: ANTHROPIC`，
**那個配對此刻正在跑**。兩個原因：

    1. HIP-3 dex 的市場名**帶前綴**：`io:ANTH`，不是 `ANTH`
    2. 兩個場館對同一個資產**叫不同名字**：Lighter 叫 ANTHROPIC、io 叫 ANTH

救它的是那個已知答案（有一個跑著的配對），不是我的警覺 ——
`factor-research.md` 最後一條：自己剛寫的儀器要先在答案已知的資料上跑一次。

===========================================================================
所以這一版多了一道控制：**價格對照**
===========================================================================
名字對得上**不代表是同一個資產**。HL core 有 `AI`、有 `MET`，Lighter 也有，
而那可能是兩個完全不同的東西 —— 那種錯會讓我們「對沖」到一個不相干的標的，
也就是把一個對沖部位變成兩個裸部位。

所以每一個配得上的都印兩邊的價格與偏離。偏離 > 5% 一律標紅：
**那不是溢價，那是配錯資產。**（§0.75 的 BTC 控制配對就是這個角色。）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request

import requests

HL = "https://api.hyperliquid.xyz/info"
LIGHTER = "https://mainnet.zklighter.elliot.ai"
HERE = os.path.dirname(os.path.abspath(__file__))
# 2026-09-15：原本讀 ../flow_system/research/results/，違反隔離規則（CLAUDE.md §1）。
# 改讀複製進 arb 的那一份（research/hmm_gate0/README.md）。
MARKOUT = os.path.normpath(os.path.join(
    os.path.dirname(HERE), "research", "hmm_gate0", "results",
    "sweep_markout.json"))
MISMATCH_PCT = 5.0
# 價格反查別名的容忍度。3% 是「同一個資產在兩個場館」的合理上界,
# 而不流動市場的真實錯價可以更大 —— 所以命中全部印出來,不自動選。
ALIAS_PCT = 3.0


def post(body):
    req = urllib.request.Request(
        HL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def bare(name: str) -> str:
    """'io:ANTH' -> 'ANTH'（HIP-3 的市場名帶 dex 前綴）。"""
    return name.split(":", 1)[1] if ":" in name else name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="")
    ap.add_argument("--fee-bps", type=float, default=4.90)
    a = ap.parse_args(argv)

    # ---- 候選 ----
    if a.coins:
        cands = [(c.strip(), None, None, None) for c in a.coins.split(",")]
    else:
        d = json.load(io.open(MARKOUT, encoding="utf-8"))
        cands = []
        for x in d:
            m1 = x.get("mo_no_1.0")
            if not isinstance(m1, (int, float)) or x.get("n", 0) < 50:
                continue
            if m1 - a.fee_bps <= 0:
                continue
            cands.append((x["coin"], x["n"], x.get("usd"), m1 - a.fee_bps))
        cands.sort(key=lambda r: -(r[3] or 0))
        if not cands:
            raise RuntimeError("候選名單是空的 —— 空輸出在這裡不是合法狀態")

    # ---- Lighter 的價格表（配錯資產的控制組）----
    lp = {}
    try:
        j = requests.get(LIGHTER + "/api/v1/orderBookDetails",
                         timeout=30).json()
        for m in j.get("order_book_details", []):
            try:
                lp[m["symbol"]] = float(m.get("last_trade_price") or 0)
            except (KeyError, TypeError, ValueError):
                continue
    except Exception as e:                                    # noqa: BLE001
        raise RuntimeError("Lighter 價格表讀不到（%r）—— 少了它就沒有"
                           "配錯資產的控制組，不要在這種狀態下解讀結果" % e)

    # ---- HL：core ＋ 每個 HIP-3 dex。索引用去前綴的名字 ----
    # bare name -> [(where, maxLeverage, full name), ...]
    # **收集全部，不是第一個。** setdefault 版本會取「第一個找到的場館」，
    # 而同一個資產可以在多個 HL 場館上市且價格差很多：實測 Lighter 的
    # ANTHROPIC 2130 對上 io:ANTH 2134（0.19%）與 vntl:ANTHROPIC 1619（24%）。
    # 取第一個會把一個完全可用的配對報成「配錯資產」。
    idx = {}
    core = post({"type": "meta"})
    for u in core.get("universe", []):
        idx.setdefault(bare(u["name"]), []).append(
            ("core", u.get("maxLeverage"), u["name"]))
    n_dex = 0
    for d0 in post({"type": "perpDexs"}):
        if not isinstance(d0, dict) or not d0.get("name"):
            continue
        try:
            m = post({"type": "meta", "dex": d0["name"]})
        except Exception:                                     # noqa: BLE001
            continue
        n_dex += 1
        for u in m.get("universe", []):
            idx.setdefault(bare(u["name"]), []).append(
                (d0["name"], u.get("maxLeverage"), u["name"]))
    print("HL：core %d 個 ＋ %d 個 HIP-3 dex，去前綴後共 %d 個可對沖名字"
          % (len(core.get("universe", [])), n_dex, len(idx)))

    # ---- HL 的中價（拿來跟 Lighter 對照）----
    # **每個 dex 要分開問**：`allMids` 不帶 dex 參數時只回 core，於是所有
    # HIP-3 的候選價格欄都是空的 —— 而那正是最需要這道控制的地方（core 的
    # AI 已經被抓到偏離 54%）。一個對「只有 core 有值」視而不見的控制組，
    # 跟沒有控制組是一樣的。
    mids = dict(post({"type": "allMids"}))
    _all_dex = {w for v in idx.values() for (w, _l, _f) in v}
    for _dx in sorted(_all_dex - {"core"}):
        try:
            mids.update(post({"type": "allMids", "dex": _dx}) or {})
        except Exception:                                     # noqa: BLE001
            print("  allMids(dex=%s) 讀不到 —— 那幾個標的沒有價格對照" % _dx)

    print()
    print("%-11s %6s %10s %8s   %-22s %9s %9s %7s"
          % ("coin", "n", "Lighter$", "淨bps", "HL 對沖腿", "HL價", "LT價", "偏離%"))
    print("-" * 96)
    ok = miss = bad = 0
    for coin, n, usd, net in cands:
        hits = list(idx.get(coin) or [])
        ltp = lp.get(coin)

        # **價格只能確認名字比對，不能取代它。**（2026-09-14 實測）
        # 第一版靠價格反查別名,確實找到了 io:OAI / io:ANTH —— 但同時把
        # AI 配到 STX、GRAM 配到 XRP、ANTHROPIC 配到 cash:ETH。
        # 而最糟的是 AI 因此報 0.14%「沒問題」,把一個真的同名不同資產
        # 蓋掉了。**一個會把紅燈變綠的修法比原來的漏洞更危險。**
        # 所以價格相近的名字只當**給人看的提示**,不進判定。
        alias_hint = []
        if ltp and ltp > 0:
            have = {h[2] for h in hits}
            for _lst in idx.values():
                for h in _lst:
                    if h[2] in have:
                        continue
                    v = mids.get(h[2])
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        continue
                    if v > 0 and abs(v - ltp) / ltp * 100.0 <= ALIAS_PCT:
                        alias_hint.append(h[2])

        def _dev(h):
            v = mids.get(h[2]) or mids.get(coin)
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None, None
            return v, (abs(v - ltp) / ltp * 100.0
                       if ltp and ltp > 0 else None)

        # 同一個名字可能在多個場館 -> 選**價格對得上**的那個，不是第一個
        best = None
        for h in (hits or []):
            v, dv = _dev(h)
            if best is None or (dv is not None
                                and (best[2] is None or dv < best[2])):
                best = (h, v, dv)
        hit = best[0] if best else None
        if not hit:
            miss += 1
            print("%-11s %6s %10s %8s   %s"
                  % (coin, n or "—", ("%.0f" % usd) if usd else "—",
                     ("%+.1f" % net) if net is not None else "—",
                     "**沒有名字命中**" + (
                         "｜價格相近的（**未驗證,要人看**）：%s"
                         % " ".join(alias_hint[:5]) if alias_hint else "")))
            continue
        where, lev, full = hit
        hlp, dev = best[1], best[2]
        flag = ""
        if len(hits) > 1:
            flag = "  （%d 處命中：%s）" % (
                len(hits), " ".join(h[2] for h in hits[:4]))
        if dev is not None and dev > MISMATCH_PCT:
            flag = ("  <- **需人工確認**：同名不同資產？還是真的錯價？"
                    + ("｜價格相近的別名候選：%s" % " ".join(alias_hint[:4])
                       if alias_hint else ""))
            bad += 1
        else:
            ok += 1
        mmf = (1.0 / lev) / 2.0 * 100 if lev else float("nan")
        print("%-11s %6s %10s %8s   %-22s %9s %9s %7s%s"
              % (coin, n or "—", ("%.0f" % usd) if usd else "—",
                 ("%+.1f" % net) if net is not None else "—",
                 "%s lev %sx mmf %.1f%%" % (where, lev, mmf),
                 ("%.4g" % hlp) if hlp else "—",
                 ("%.4g" % ltp) if ltp else "—",
                 ("%.2f" % dev) if dev is not None else "—", flag))
    print()
    print("  可用 %d 個｜名字對不上 %d 個｜**價格對不上（配錯資產）%d 個**"
          % (ok, miss, bad))
    if bad:
        print("  偏離大的那些要人看過再用：可能同名不同資產（把對沖做成兩個裸部位），"
              "也可能是不流動市場上的真實錯價。兩者的下一步完全相反。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
