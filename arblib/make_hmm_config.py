# -*- coding: utf-8 -*-
"""從**量測**生成一份 HMM 設定，而不是複製一份再改。

    python arblib/make_hmm_config.py OPENAI --dex io --hl-symbol OAI
    python arblib/make_hmm_config.py ANSEM  --dex para
    python arblib/make_hmm_config.py MINIMAX --dex xyz

===========================================================================
為什麼要有這一支（2026-09-14，從兩個真的抓到的 bug 長出來）
===========================================================================
`config_MET.yaml` 是從 `config_HMM_GMX.yaml` 複製的，而複製帶進了兩個錯：

    max_net_base        單位是**基礎單位**。GMX 的 2.0 在 $7.80 上是 $15.60，
                        MET 是 $0.234 -> **$0.47**，每一筆成交都踩線。
                        （而 GMX 那個 2.0 自己也是錯的：1.04 張單,
                          對沖完全失敗時跳不起來。）
    net_tolerance_base  同樣是基礎單位。HL 的 MET 是 szDecimals 0 ——
                        最小交易單位 1 整顆（$0.234），所以殘餘量子是 1 顆,
                        而複製來的 0.01（$0.0023）**永遠滿足不了**。

兩個都不會報錯，它們只是讓風控閘門在一個標的上恆真、在另一個上恆假。

**修法不是「下次小心」，是讓那條路徑不存在**：價格相依的參數一律從
API 查到的價格與步長算出來，而算式與當時的數字一起寫進產生的檔案裡，
所以三個月後讀它的人看得到它是怎麼來的。

===========================================================================
算式（每一條都寫進輸出的註解裡）
===========================================================================
    一張單          clip_base = max_order_notional_usd / price
    殘餘量子        quantum   = max(兩腿的步長)
                    對沖之後兩腿的殘差只能是量子的整數倍
    max_net_base    0.25 x clip_base，且 >= 3 x quantum
                    **必須 < 1 張單**（否則對沖完全失敗時跳不起來，
                    config.py:479 / engine.py:1478），
                    且要高過粉塵（否則正常的殘差就會誤觸）
    net_tolerance   2 x quantum
    min_order       max(場館下限, $10)

不從量測來的、刻意在候選之間**保持一致**的：
    thresholds ±15 bps    G2 直接受它影響，換一個門檻就換一個答案 ——
                          要比較候選，這個必須一樣。
    max_position_usd 60   每腿；max_account_gross_usd 70 = 單一市場紀律
    max_order_notional 15 M2/M3 沒量之前不放大（對沖腿實測吃得下 30-40 倍）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.error          # 明寫：`import urllib.request` 讓它剛好可用,
import urllib.request        # 但那是實作細節,不是保證

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENG = os.path.join(os.path.dirname(HERE), "engine")
HL = "https://api.hyperliquid.xyz/info"
LIGHTER = "https://mainnet.zklighter.elliot.ai"

CLIP_USD = 15.0
MIN_USD = 10.0
# 2026-09-14：15.0 -> 3.0。**這不是調參數,是把兩處不一致的地方對齊。**
#
# `engine._eff_threshold` 回傳 `midline + upper`,而它的 docstring 寫著
# 「Net hurdle (bps, **on top of fees**)」—— 也就是**淨邊際要超過這個數
# 才報價**。而 `hmm_screen` / `hmm_universe` 的 G1 判準是「淨 > 3.0 bps
# 才值得做」。兩者量的是同一件事,卻差五倍：
#
#     篩選說「這個市場淨 3.8 bps,可以做」-> 引擎說「低於 15,不動」
#
# 15 是為 GMX 訂的（那個市場的淨邊際 89 bps）,然後被複製到每一份設定。
# 這跟 max_net_base 從 GMX 複製到 MET 是同一個病：**一個為某個市場量身
# 的數字被當成常數**。AERO 實測淨 3.8 bps、FIL 2.4 —— 兩個都被 15 擋死,
# 而 FIL 一小時只報價 2 次就是這個原因,不是它壞掉。
#
# 對齊到 G1_NET_BPS。要改就兩邊一起改,否則篩選選出來的東西引擎不會做。
BAND_BPS = 3.0
CAP_USD = 60.0


def post(body, tries: int = 5):
    """HL 的 /info，帶退避重試。

    2026-09-14：這支在產 FIL 設定時被 HTTP 429 打掉。**大聲失敗是對的**
    （比安靜回一份殘缺的設定好太多），但 429 是暫時的,而這台機器上有 16 支
    引擎在共用同一個 IP 的預算 —— 撞到它是常態不是例外。沒有退避 = 每次都
    要人重跑一次,而人會在第三次的時候改成「先不管那個欄位」。
    只對 429/5xx 退避；4xx 的其他錯（打錯 body）立刻拋，那種重試沒有意義。
    """
    req = urllib.request.Request(
        HL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    for i in range(tries):
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            if i == tries - 1:
                raise RuntimeError(
                    "HL /info 連續 %d 次 %s —— 這台機器同時有多支引擎在打同一個"
                    "端點,等幾分鐘再跑,不要把讀不到當成『那個欄位沒有』"
                    % (tries, e.code)) from e
            wait = 3.0 * (2 ** i)
            print("  HL %s，%.0f 秒後重試（%d/%d）" % (e.code, wait, i + 1, tries))
            time.sleep(wait)


def lighter_get(path):
    for i in range(5):
        r = requests.get(LIGHTER + path, timeout=30)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        if i < 4:
            time.sleep(15)
    raise RuntimeError("Lighter REST 一直被限流 —— 沒有步長就不要生成設定")


TMPL = """# {sym} —— HMM 候選（{date} 由 arblib/make_hmm_config.py **從量測生成**）
#
# **不要用複製的方式改這份檔。** 下面每一個價格相依的數字都是從當天的
# 價格與步長算出來的；複製到另一個標的上它們全部失去意義,而且不會報錯。
# 換標的請重跑產生器（mistake.md 2026-09-14）。
#
# 生成當下的量測：
#   Lighter {sym}   價格 {lpx:.6g}   步長 {lstep:.6g} 顆   日成交額 ${lvol:,.0f}
#   HL {hfull}      價格 {hpx:.6g}   步長 {hstep:.6g} 顆   maxLeverage {lev}x
#   價格偏離 {dev:.2f}%（>5% 就是同名不同資產,不是溢價）
#   一張單 ${clip:.0f} = {clip_base:.6g} 顆｜殘餘量子 {quantum:.6g} 顆 = ${quantum_usd:.4f}

entropy:                      # 吃單對沖腿
  dex: '{dex}'
  # **這一腿的 symbol 不在這裡,在 `--symbol`。** config.py:591 對 HL 腿寫的是
  # `symbol=symbol`（CLI 的那個）—— 寫在 YAML 裡的 `entropy.symbol` 會被
  # schema 接受、解析成功,然後**安靜地忽略**。所以啟動器用 `--symbol {hsym}`,
  # 而掛單腿的 {sym} 寫在下面的 `hedge.symbol`。
  # （ANTH 一直是這個形狀：--symbol ANTH ＋ hedge.symbol: ANTHROPIC。）
  # 2026-09-14 用 `userFees`（帶 dex 參數）逐池查證：core / io / para / xyz
  # **四個池的費率完全相同** —— cross 0.00045 / add 0.00015。
  # perpDexs 的 `feeRecipient` 是從基礎費裡**分成**,不是外加。
  taker_fee_bps: 4.5
  maker_fee_bps: 1.5
  max_position_usd: {cap:.0f}
  max_orders_per_min: 30

hedge:                        # 掛單腿（execution.maker_venue: hedge）
  symbol: {sym}
  taker_fee_bps: 2.8          # Lighter PREMIUM
  maker_fee_bps: 0.4          # ← 這條腿掛單,這才是我們實際付的
  max_position_usd: {cap:.0f}
  max_orders_per_min: 60

thresholds:
  # **候選之間刻意保持一致。** G2（兩側可做性）直接受它影響 ——
  # 換一個門檻就換一個答案,所以要比較候選，這三行不可以逐標的調。
  midline_bps: 0.0
  upper_bps: {band:.1f}
  lower_bps: {band:.1f}

sizing:
  take_fraction: 0.5
  # 維持 15 而理由不是深度：對沖腿實測吃得下三十到四十倍
  # （GMX bid 中位 $819 / MET $516,自己中價 5 bps 內）。
  # 綁束是 M2（成交率）與 M3（逆選擇）都還沒量 —— 它們才是每張單的真實
  # 成本。放大一個還沒量過的東西不是放大收益。
  max_order_notional_usd: {clip:.0f}
  min_order_notional_usd: {minord:.0f}   # 場館下限 ${vmin:.2f}

inventory:
  scale_bps: 10.0
  floor_frac: 0

execution:
  mode: maker
  maker_venue: hedge
  maker_timeout_sec: 20.0
  cancel_timeout_sec: 3.0
  maker_poll_sec: 0.25
  # **撤單門檻必須低於進場門檻**（upper_bps {band:.1f}）,否則掛上去的下一個
  # tick 就被自己撤掉。取一半：邊際掉到剩一半就不值得繼續佔著那個價位。
  # 2026-09-14 之前這裡寫死 5.0 而帶是 15,看起來沒事；帶改成 3.0 之後
  # 5.0 就會變成「掛上即撤」—— 兩個數字是綁在一起的,不可以只改一個。
  maker_min_edge_bps: {floor:.1f}
  premium_persist_sec: 2.0
  cooldown_sec: 0.0
  # 30 秒 ＋ 5 秒心跳：Lighter 的 feed 不會自己證明存活,而 staleness 比的是
  # alive_ts。實測沒有心跳時 24.6% 的訊框空檔超過 10 秒,GMX 因此十五分鐘
  # 就 HALT（TODO §1.41b）。
  staleness_sec: 30.0
  ws_ping_sec: 5.0
  reconcile_sec: 15.0
  settle_timeout_sec: 10.0
  leg_slippage_bps: 50.0
  hedge_slippage_bps: 20.0
  # 2 x 殘餘量子（{quantum:.6g} 顆）。對沖之後兩腿的殘差只能是量子的整數倍,
  # 所以小於一個量子的容忍值**永遠滿足不了**。
  net_tolerance_base: {tol:.6g}
  max_consecutive_errors: 3
  rate_limit_pause_sec: 10.0
  venue_probe_sec: 30.0
  # 2026-09-14：10.0 -> 50.0。**這條是 WAF 預算的最大單一消耗者。**
  #
  # 它唯一的工作是讓下單路徑的 TLS 連線不要斷,而連線池的
  # `keepalive_timeout` 是 **75 秒**（engine.py:210）。每 10 秒 ping 一次
  # 是需要的 7.5 倍 —— 每支引擎兩腿 6 req/分,七支 shadow 就是 84 req/分,
  # 而九支 record-only 加起來才 18（`_http_keepalive_loop` 只在
  # `not record_only` 時起）。
  #
  # 實測後果：2026-09-14 每十分鐘整,八支引擎在三十秒內全部被擋一次,
  # 每次 60 秒（`API recovered after 60s outage`）—— 10% 的時間不能交易,
  # 而它擋的是**重連**,那時引擎手上有部位。
  #
  # 50 秒留 25 秒餘裕（75 - 50）,足夠吸收一次丟包與重試,
  # 同時把消耗降到原本的 1/5。
  http_keepalive_sec: 50.0

risk:
  # **必須 < 一張單**（{clip_base:.6g} 顆）：它擋的是「對沖完全失敗」,
  # 而那時 net 正好是一張單（engine.py:1478 —— _maybe_hedge 在報價結算後
  # 才查,立即對沖已經試過）。也要高過粉塵（{quantum:.6g} 顆）。
  # 這裡 = 0.25 張單 = {mnb:.6g} 顆 = ${mnb_usd:.2f} = {quanta:.0f} 個量子。
  max_net_base: {mnb:.6g}
  max_daily_loss_usd: 10.0
  max_gross_usd: {gross:.0f}
  # 單一市場紀律：第二個 live 行程會讓 account_budget.py 的 B3 變紅。
  max_account_gross_usd: 70.0
  min_account_free_usd: 25.0
  max_consecutive_stale: 20
  max_stale_episodes: 5
  halt_flatten_attempts: 3
  max_edge_bps: 300.0
  vol_window_sec: 60.0
  vol_max_move_bps: 300.0
  vol_cooldown_sec: 60.0
  unexplained_position_halt: true

control:
  # B5 控制通道。**沒有 Telegram 憑證時它照樣可用**：
  #     echo flat  > logs/{sym}/control.cmd     全平並保持暫停
  #     echo pause > logs/{sym}/control.cmd     停開新倉,對沖照跑
  # 每 2 秒輪詢,讀完即清空(殘留指令不會在重啟後再平一次倉)。
  # 它**不能**解除 HALT —— 解除 HALT 只能靠重啟,那是刻意的。
  #
  # **為什麼一定要逐配對(2026-09-14 翻 live 前查到的)**：預設值是
  # `control.cmd`,相對於 cwd,而這台機器上十幾個引擎的 cwd 全是 arb/engine,
  # 讀取端又是 `r+` 之後 truncate —— `echo flat > control.cmd` 會被**先搶到
  # 的那一個**吃掉,而真正在送單的那個拿到它的機率是 1/N。
  # 那不是一個需要的時候用得上的開關,而「需要的時候用得上」是它唯一的意義。
  command_file: logs/{sym}/control.cmd
  poll_sec: 2.0

logging:
  dashboard: false
  level: INFO
  file: logs/{sym}/engine.log
  trades_csv: logs/{sym}/trades.csv

recorder:
  enabled: true
  csv: logs/{sym}/minutes.csv
"""

BAT = """@echo off
REM {sym} - HMM candidate, generated from measurements ({date}).
REM CLI --symbol is the HL leg ({hsym}); the Lighter leg ({sym})
REM is hedge.symbol in the yaml. config.py ignores entropy.symbol.
REM MODE: LIVE. THIS SENDS REAL ORDERS.
REM
REM Shadow was retired on 2026-09-14 and the reason is structural, not a
REM preference: in shadow, `shadow_decisions` and `quotes_cancelled` are
REM equal line for line and `quotes_rested` is permanently 0, because
REM maker_rested needs an OPEN status back from the venue. So M2 (fill
REM rate) has a zero denominator, M3 has no fills to mark out, and M4 has
REM no real round trip -- the three gates that decide whether this works
REM are STRUCTURALLY unmeasurable there. A ten-hour clean shadow run told
REM us nothing about whether live would even start (it would not: the HL
REM SDK was not installed).
REM
REM STOP ORDER MATTERS:
REM   1. echo flat > logs{bs}{sym}{bs}control.cmd   (while the engine is alive)
REM   2. comment this member out of ops{bs}arb_watchdog.ps1
REM   3. only then kill the processes
REM   4. wait past one watchdog cycle (>5 min) and re-check -- an
REM      immediately-empty process list is NOT evidence
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines.
cd /d {eng}
:loop
python main.py --symbol {hsym} --hedge lighter --config config_{sym}.yaml --no-dashboard >> logs{bs}{sym}{bs}runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
"""


def build(sym, dex, hsym):
    lt = {m["symbol"]: m for m in
          lighter_get("/api/v1/orderBookDetails")["order_book_details"]}
    m = lt.get(sym)
    if not m:
        raise RuntimeError("Lighter 上沒有 %s —— 空結果不是合法狀態" % sym)
    lpx = float(m["last_trade_price"])
    lstep = 10.0 ** (-int(m.get("supported_size_decimals", 0)))
    vmin = max(float(m.get("min_quote_amount") or 0),
               float(m.get("min_base_amount") or 0) * lpx)

    meta = post({"type": "meta", "dex": dex} if dex else {"type": "meta"})
    mids = post({"type": "allMids", "dex": dex} if dex
                else {"type": "allMids"})
    full = ("%s:%s" % (dex, hsym)) if dex else hsym
    u = next((x for x in meta["universe"] if x["name"] == full), None)
    if not u:
        raise RuntimeError("HL 上沒有 %s" % full)
    hstep = 10.0 ** (-int(u["szDecimals"]))
    hpx = float(mids.get(full) or 0)
    dev = abs(hpx - lpx) / lpx * 100.0 if lpx else float("nan")
    if dev > 5.0:
        raise RuntimeError("%s 的兩腿價格差 %.1f%% —— 同名不同資產,"
                           "拿它當對沖腿會把一個對沖部位變成兩個裸部位"
                           % (sym, dev))

    clip_base = CLIP_USD / lpx
    quantum = max(lstep, hstep)
    # **第三個約束（2026-09-14 實盤打出來的，本支原本沒有）。**
    # 上面兩條（< 1 張單、> 3x 量子）不足以讓引擎跑得動，因為它們沒問
    # 「這個失衡**對沖得掉嗎**」。net-delta 對沖是 reduce-only，動的是扛著
    # 失衡的那一腿（engine.py:2040），而任一腿都有自己的最小單。
    # max_net_base 低於那個最小單 = 引擎會在一個它**結構上平不掉**的失衡上
    # HALT，而 HALT 的 self-rescue 被同一個最小單擋住 -> 沒有出路。
    #
    # MET 實測（2026-09-14 12:15-12:53）：舊算式給 0.25 x 68 = 17 顆 = $3.75，
    # 而 Lighter 的最小單是 50 顆 = $11.03 -> 部位累積到 -16.1 就會 HALT，
    # 永遠走不到對沖。改成 58 顆之後，-50.6 時對沖在 3 秒內 50/50 成交。
    hedge_floor = max(vmin / lpx,            # 掛單腿（Lighter）的最小單
                      10.0 / lpx, 10.0 / (hpx or lpx))   # 兩腿的 $10 下限
    mnb = max(0.25 * clip_base, 3.0 * quantum, hedge_floor * 1.15)
    if hedge_floor >= clip_base:
        raise RuntimeError(
            "%s 在 $%.0f 的單量下做不出合法的 max_net_base：最小可對沖 %.6g 顆 "
            "已經 >= 一張單 %.6g 顆。三條約束（>= 可對沖、< 一張單、> 3x 量子）"
            "沒有交集 —— 單量要放大到至少 $%.0f，或換一個標的。"
            % (sym, CLIP_USD, hedge_floor, clip_base, hedge_floor * lpx * 1.4))
    if mnb >= clip_base:
        raise RuntimeError("%s 的殘餘量子 %.6g 顆太粗：3 個量子已經 >= 一張單"
                           "（%.6g 顆）—— 這個標的在 $%.0f 的單量下"
                           "做不出一個有意義的 max_net_base"
                           % (sym, quantum, clip_base, CLIP_USD))
    return dict(sym=sym, dex=dex, hsym=hsym, hfull=full,
                date=time.strftime("%Y-%m-%d"),
                lpx=lpx, hpx=hpx, dev=dev, lstep=lstep, hstep=hstep,
                lev=u["maxLeverage"],
                lvol=float(m.get("daily_quote_token_volume") or 0),
                clip=CLIP_USD, clip_base=clip_base,
                quantum=quantum, quantum_usd=quantum * lpx,
                mnb=round(mnb, 8), mnb_usd=mnb * lpx,
                quanta=mnb / quantum, tol=round(2.0 * quantum, 8),
                band=BAND_BPS, floor=BAND_BPS / 2.0,
                cap=CAP_USD, gross=CAP_USD * 2 + 10,
                minord=max(MIN_USD, vmin), vmin=vmin)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--dex", default="")
    ap.add_argument("--hl-symbol", default="")
    a = ap.parse_args(argv)
    d = build(a.symbol, a.dex, a.hl_symbol or a.symbol)

    cfg = os.path.join(ENG, "config_%s.yaml" % d["sym"])
    io.open(cfg, "w", encoding="utf-8", newline="\n").write(TMPL.format(**d))
    import yaml
    yaml.safe_load(io.open(cfg, encoding="utf-8"))   # 語法自曝

    # **名字要跟模式一致。** 2026-09-14 shadow 退場之後這支產的是會下單的
    # 啟動器,而它原本還叫 `run_recorder_*` —— 一個叫「recorder」的檔案會
    # 送真單,是下一個人最容易誤會的東西。改成 run_hmm_*,跟既有的
    # live 啟動器同一個命名,看門狗的註冊表也只認這個前綴。
    bp = os.path.join(ENG, "run_hmm_%s.bat" % d["sym"])
    b = BAT.format(eng=ENG, sym=d["sym"], hsym=d["hsym"],
                   date=d["date"],
                   bs=chr(92)).replace("\n", "\r\n").encode("ascii")
    io.open(bp, "wb").write(b)
    assert b.count(b"\n") == b.count(b"\r\n") and all(c < 0x80 for c in b)
    os.makedirs(os.path.join(ENG, "logs", d["sym"]), exist_ok=True)

    # 自曝：用引擎自己的解析器確認兩條腿都指到存在的市場。
    # 這一關是因為第一版把 entropy.symbol 寫進 yaml（被忽略）而 .bat 用了
    # Lighter 的名字 -> 會去找不存在的 "io:OPENAI"。
    sys.path.insert(0, ENG)
    from entropy_arb.config import load_config
    c = load_config(cfg, symbol=d["hsym"], hedge_venue="lighter",
                    env_file=os.path.join(ENG, ".env"))
    assert c.entropy.symbol == d["hsym"], (
        "entropy 腿是 %s，應該是 %s" % (c.entropy.symbol, d["hsym"]))
    assert c.hedge.symbol == d["sym"]
    assert ("--symbol %s " % d["hsym"]).encode() in b, "啟動器的旗標不對"

    print("%-8s 價格 %.6g｜量子 %.6g 顆($%.4f)｜一張 %.6g 顆｜"
          "max_net_base %.6g (%.2f 張, %.0f 量子)｜tol %.6g"
          % (d["sym"], d["lpx"], d["quantum"], d["quantum_usd"],
             d["clip_base"], d["mnb"], d["mnb"] / d["clip_base"],
             d["quanta"], d["tol"]))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
