# B3b 逐行審：對沖腿「撤單未確認」的追蹤 ＋ HL 輪詢間隔（2026-09-15）

> 範本照 `B1_ENGINE_AUDIT.md` / `B3_MAKER_AUDIT.md`。CLAUDE.md §2.1：碰錢的新執行路徑
> 要一份審查，並明寫**沒跑過的部分**。

## 結論先講

- **修的是 2026-09-15 15:13–15:34 MON 的 HALT。** 帳最後是平的（net +0.2），
  但引擎停了，而且 HALT 的理由錯了：不是「有人動了帳戶」，是我們自己一張沒追蹤的單。
- **429 的主因不在本 repo**：MON 當天 274 行 HL 429 全部落在 :05–:33，
  也就是 flow_system `FlowBot_HLRecord` 的執行窗（TODO §1.41e，那邊正在把它搬上 Railway）。
  本審查修的是**引擎在 429 之下的行為**，外加一項減負：
  1. **（主修）** 撤單未確認時舊版把 handle 丟掉（違反 CLAUDE.md §2.3）→ 撤單被拒在預算內重送；
     仍未確認就登記成 orphan，對帳迴圈繼續撤／問到交易所給終態；期間它那條腿的部位
     變動歸因給我們（`_we_touched`）、新報價暫停。
  2. **（減負，非根因）** HL 的 `poll_order` 是 REST（`orderStatus` 權重 2、每 IP 1200/分，
     HL 官方文件當天查證），而掛單對沖沿用報價腿的 `maker_poll_sec` 0.05 秒，理論上限 2400/分；
     **實際吃掉多少沒量**（受往返延遲限制）。新旋鈕 `execution.hedge_maker_poll_sec`，
     **預設 0.0 = 舊行為**，只有 `config_MON.yaml` 設 1.0。**注意**：與錄製器搬家同時上線會混淆
     §1.41e 第三條驗證，見那一節。
- 只影響 `hedge_maker_timeout_sec > 0` 的設定（目前只有 MON）。21 個啟動器的設定檔
  載入驗過；266 passed / 2 skipped；7 個破壞注入全紅。
- **MON 尚未重啟**（使用者：「先不要重啟等修好了再說」）。這份改動在真實 HL 上**沒跑過**。

## 1 事故逐行（`engine/logs/MON/runner.log`）

    15:13:27.857  [QUOTE FILL] LIGHTER SELL 661 — hedging
    15:13:58.001  [HEDGE MAKER] 撤單回錯:RATE_LIMITED: HTTP 429 null
    15:14:01.008  [HEDGE MAKER] **撤單沒有確認** handle=...1a0a3922b7b
    15:14:01.692  [QUOTE HEDGE] HL BUY 661/661 filled                 <- 殘量吃單（對的）
    ……（那張 post-only BUY 661 仍在簿上，之後成交）
    15:34:52.970  HALTED: [HL] position moved +661 with no order from us in 1251s
    15:34:52.970  [HL] reconcile: chain +1522 vs local +861 — adopting chain
    15:34:54.774  SELF-RESCUE COMPLETE: net +0.2

429 的分布：MON 當天所有 HL 429 都落在每小時 **:05–:22**，而 flow_system 的
`FlowBot_HLRecord`（`hl_fuel_recorder.py --max-addr 2500`）每小時 :05 開跑到 :33。
**那一半不在本 repo 能修的範圍**（§1 隔離），這裡只把引擎自己那一份降下來，並讓引擎
在限流下仍然不丟單。

## 2 改動逐處

| 位置 | 改了什麼 | 為什麼 |
|---|---|---|
| `config.py` 欄位／schema／載入 | `hedge_maker_poll_sec`，預設 0.0，負值拒絕 | 新旋鈕預設舊行為（§5） |
| `engine.py:_hedge_poll_interval` | >0 用它；否則 `min(maker_poll_sec, 0.25)`（逐位元組等於舊式） | 報價腿與對沖腿的 poll 成本不同 |
| `engine.py:_hedge_try_maker` 兩個輪詢迴圈 | sleep 改用上面那個間隔 | 同上 |
| `engine.py:_hedge_try_maker` 撤單段 | 第一次撤單之後，**未被接受**（rejected／unresolved／炸掉）就每 `max(poll, 0.5)` 秒重送，直到終態或 `cancel_timeout_sec` 預算用完；**已接受就不重送** | 事故那一次只送一次，剩餘預算都在問一張沒撤的單 |
| `engine.py:_hedge_try_maker` 未確認分支 | 建一個 `MakerOrder`（UNKNOWN、`known_filled`＝當下已知成交），放進 `_hedge_orphans`；吃單照送 | §2.3：本地狀態只能被交易所的回報清除 |
| `engine.py:_hedge_cancel_once` | 送一次撤單、記入 send budget、絕不拋；回傳 accepted/gone | 單一實作，迴圈內與對帳迴圈共用 |
| `engine.py:_service_hedge_orphans` | 每輪：poll → `apply()`；終態才移除；多出來的成交**不記本地帳**，只蓋 `last_traded_ts` 並觸發對帳；未終態就再撤一次；每 60 秒 CRITICAL `HEDGE ORDER STILL UNRESOLVED` | §2.4 採信鏈上後不疊本地 delta；不放棄 |
| `engine.py:_reconcile_loop` | 對帳前先 service orphans，例外被吞並記錄 | 同一輪剛成交時，對帳要看得到「它是我們的」 |
| `engine.py:_we_touched` | 該場館有 orphan → True | 事故的 HALT 就死在這一格 |
| `engine.py:_scan_maker` | 有 orphan → 不開新報價 | 跟 UNKNOWN 報價單擋下一張同一條規則 |
| `engine.py:_has_exposure` | orphan 算曝險 | 一張可能還活著的單 |
| `engine.py:_run_inner` 關機 | orphan 各撤一次（best effort），CRITICAL 說明 | 重啟時 `_cancel_stale_orders` 是第二道 |
| 狀態行／`control_status` | 顯示 orphan | 看得見 |
| `ops/hmm_watch.py` | 抓 `HEDGE ORDER STILL UNRESOLVED` 送 Discord | 看護原本只認報價腿那句 |
| `config_MON.yaml` | `hedge_maker_poll_sec: 1.0` | 120 權重/分；逆選擇 5–120 秒是平的，晚 1 秒發現沒有可量成本 |

**刻意用不同的字**：`tools/halt_recover.py` 看到 `MAKER ORDER STILL UNRESOLVED (N cancel attempts)`
累積 100 次會**自動殺行程重啟**。對沖腿那張單可能真的還在簿上，這裡要人看，不要自動殺。
有一條測試釘住這件事。

## 3 三條「必須」對照（PEER_INFRA §7）

1. **本地狀態只被交易所清除** —— 修之前**違反**（未確認就丟 handle）。修之後 orphan 只在
   `apply()` 收到終態時移除（`test_service_keeps_cancelling_until_terminal`、破壞注入 M6）。
2. **部分成交要對沖** —— 不變。orphan 登記時已知的成交已由 `_book_hedge_fill` 記帳並從吃單
   數量扣掉；之後多出來的交給對帳＋淨額對沖。
3. **未確認的撤單是悲觀的** —— 修之前只悲觀了一半（仍吃單，但不再追）。修之後登記為 UNKNOWN、
   暫停新報價、繼續撤。

## 4 對「多出來的成交」的處理為什麼是對的

事故形狀：orphan 是 BUY，事後成交 → HL 多 +qty → 淨額 +qty。

- 引擎**不**在本地加這 qty。它在對帳時被 `v.position = r` 採信（完整讀數）。若本地也加，
  對帳會把它當成已知、而下一次讀數又把 delta 算一次 → 重複記帳。
- 採信之後 `_maybe_hedge`：|net| 超過 `max_net_base`（491.873 < 661）→ 開始寬限計時
  （`net_grace_sec` 45），**同一次呼叫**就走到 `_hedge(net)`：對「帶著失衡的那一腿」
  （HL，position×sgn 最大）送 reduce-only 吃單。這跟事故當天的 SELF-RESCUE 是同一筆單，
  差別是**不 HALT**。
- 若 45 秒內收不回來（例如 HL 仍在限流），原本的 net-imbalance HALT 照響。**沒有任何一道
  HALT 被移除**；只有「不明變動」這一道在 orphan 存在時不再誤判。

## 4b 獨立審查抓到的三件事（2026-09-15，python-reviewer 子代理）與處置

1. **（高，已驗證）`test_service_resolves_a_late_fill_without_booking_it_twice` 後半是空轉的。**
   時間戳剛蓋上就呼叫 `_reconcile_venue`，撞到 `RECONCILE_GRACE_SEC` 第一行 return，
   沒讀部位、沒判歸因 —— 任何實作都會過。**已修**：把時鐘撥到「寬限已過、仍在歸因窗內」
   （= 真實迴圈的下一輪），並先斷言鏈上部位確實被採信，證明對帳真的跑了；另加控制組
   `test_late_fill_after_the_attribution_window_still_halts`。
2. **（高，推測→已修）orphan 存在期間，任何大小、任何方向的變動都被當成我們的。**
   orphan 設計上可以無限期存在，所以那段期間強平／ADL／手動單不會 HALT。**已修**：
   `_we_touched` 不再看 orphan，改成 `_reconcile_venue` 另問 `_orphan_explains(v, delta)` ——
   只接受**同方向**且 **≤ 未成交殘量 + net_tolerance_base** 的變動，只算同一條腿。
   審查順帶暴露我自己一條測試方向寫反（賣單 orphan 卻讓部位 +10），第一版能過正是因為
   沒有方向檢查。新增三關：反方向、超過殘量、別的場館 —— 都必須 HALT。
   **仍存在的邊界**：orphan 解決後蓋的時間戳在歸因窗內（MON 30 秒）不看大小，
   這跟既有「任何自己的下單之後 30 秒」是同一個形狀，不是新洞。
3. **（中，已驗證）撤單重送現在計入該場館的 `max_orders_per_min` 滑動額度。**
   舊版對沖腿的撤單不計；報價腿的 `_send_maker_cancel` 一直都計（「計入但從不被擋」）。
   **保留**，理由：與報價腿一致，而且方向是保守的（多算不少算）。量級：初始最多約 4 次，
   之後每個 reconcile_sec 一次；orphan 期間新報價本來就暫停。**沒有測試**。

破壞注入加到 9 個（M4 改成「對帳不問 orphan」、新增 M8 不限方向大小、M9 不分場館），全紅。

## 5 沒有跑過的部分（明寫）

1. **真實 HL 上完全沒跑過。** 所有驗證是 `FakeVenue`。特別是：
   - HL `cancelByCloid` 被 429 拒絕時，`_post_exchange` 回 `err="RATE_LIMITED: ..."`、
     `cancel_order` 回 `status="rejected"` —— 程式碼路徑讀過（`venue_hl.py:455-457`），沒實測。
   - HL `orderStatus` 對**已成交／已撤**的 cloid 會不會一直回得出終態、還是過一段時間變
     `unknownOid`。若變 unknownOid，orphan 永遠不會移除 → 引擎永遠暫停報價，每分鐘 CRITICAL，
     看護會報。那時要人：到 HL 看那張單 → 重啟（啟動掃單會清）。
2. **真實時序下的歸因窗口。** orphan 解決時蓋 `last_traded_ts`；對帳因 `RECONCILE_GRACE_SEC`
   跳過 5 秒內交易過的場館，下一輪在 ~15 秒後，落在 `_attribution_window()`＝30 秒內。
   測試直接呼叫 `_reconcile_venue`，**沒有**走真實的 grace + 迴圈節奏。
3. **1.0 秒輪詢對掛單對沖捕獲率的影響**沒量（成交最多晚 1 秒被看到，理論上不影響是否成交）。
4. **flow_system 的 HL 錄製仍會把 IP 吃滿。** 撤單在 :05–:33 仍可能 429；這個修法讓那種情況
   從「HALT」變成「orphan → 對帳 → 淨額對沖」，**不是**讓 429 消失。

## 6 對 §1.41d（09-16 結算）的影響 —— 語意分界

§1.41d 數的是 `[HEDGE MAKER]` 行的捕獲率與「未確認」次數。這個改動會**降低未確認次數**
（撤單重送），並改變輪詢頻率。**分界：本改動上線（MON 重啟）那一刻。** 之前與之後的
「撤單未確認」不可以加在一起算。截至 HALT：捕獲 1/2、未確認 1/2（13:37 修正版之後；
那 1 次的原因是 IP 限流）。MON 從 15:34 停到現在，這段時間沒有樣本。

## 7 測試清單（`engine/tests/test_hedge_orphan.py`，12 個）

| 測試 | 破壞注入 |
|---|---|
| `test_poll_interval_default_is_the_old_behaviour` | — （預設值） |
| `test_poll_interval_is_honoured_by_the_hedge_loop` | M1 紅 |
| `test_rate_limited_cancel_is_resent_inside_the_budget` | M2 紅 |
| `test_accepted_cancel_is_not_spammed` | — |
| `test_unconfirmed_cancel_is_tracked_not_forgotten` | M3 紅 |
| `test_orphan_makes_a_later_position_move_ours`（含控制組：無 orphan 必須 HALT） | M3、M4 紅 |
| `test_orphan_pauses_new_quotes_and_counts_as_exposure` | M3、M7 紅 |
| `test_service_keeps_cancelling_until_terminal` | M3、M6 紅 |
| `test_service_resolves_a_late_fill_without_booking_it_twice` | M3、M5 紅 |
| `test_service_resolves_a_clean_cancel` | — |
| `test_service_survives_venue_errors` | M3、M6 紅 |
| `test_unresolved_log_does_not_trip_the_quote_leg_auto_restart` | — （字串釘） |

M1 拿掉新間隔／M2 拿掉撤單重送／M3 不登記 orphan／M4 `_we_touched` 不看 orphan／
M5 解決時不蓋時間戳／M6 未終態就移除／M7 `_scan_maker` 不看 orphan。
既有 `test_hedge_maker.py` 14 個全部照過（含「撤單失敗仍吃單」「確認不到仍吃單」）。
