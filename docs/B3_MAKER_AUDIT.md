# B3 逐行審：新增的掛單路徑（2026-09-04）

> 對象：`engine/entropy_arb/maker.py`（新）、`book.py` 的 `plan_maker` 一族（新）、
> `venue_hl.py` / `venue_lighter.py` 的 `send_maker` / `poll_order` /
> `cancel_order` / `cancel_open_orders`（新）、`engine.py` 的 `_scan_maker` →
> `_quote` → `_maker_lifecycle` → `_consume_maker_fill` → `_hedge_maker_fill`
> → `_finish_maker`（新）。
> **這是新寫的碰錢程式碼，所以照 B1 的規格自審：判準仍是那對兄弟 $1.1M 的病灶
> （資料過期 → 以為沒成交 → 一直補單），再加上 XEMM 那兩個坑。**
> CLAUDE.md 硬規則：「下單程式碼要逐行審」。

## 結論先講

`PEER_INFRA.md` §7 的三條「必須」都落在**可以單獨測試的地方**（`maker.py`
沒有 I/O、沒有自己的時鐘），四個規格點名的情境都有測試。
**自審過程中改了四處**（§4），其中兩處是真缺口（§4.1 的錯誤計數器重置、
§4.2 的無編號掛單）。**剩下四項已知限制寫在 §6，沒有一項是「以為沒成交就補單」
那一族。**

沒有實盤驗證過——兩個 SDK 本機都沒裝（錄價模式不需要），所以**簽章與送出那
一層是照官方原始碼寫的，不是跑過的**（§5 列出每一條的出處）。

---

## 1 三條「必須」逐條驗

### 1.1 必須一：撤單確認前不得清掉本地訂單狀態

XEMM 的病灶（`xemm_executor.py:234-235`）：

```python
self._strategy.cancel(...)
self.maker_order = None          # ← 下一行就忘掉
```

我們的對應物是 `maker.py` 的 `request_cancel()`：

```python
def request_cancel(self, now: float) -> None:
    if self.state == DONE:
        return
    if self.cancel_ts is None:
        self.cancel_ts = now
    self.cancel_attempts += 1
    if self.state != UNKNOWN:
        self.state = CANCELLING
```

**它不碰 `handle`、不碰 `filled_base`、不碰 `hedged_base`，也不把訂單從
`_maker_open` 拿掉。** 唯一能讓訂單變成 `DONE` 的是 `apply()`，而
`apply()` 只被兩個地方餵：`poll_order()` 的回報，與送單回應本身。

| 誰可以讓訂單終結 | 有沒有 |
|---|---|
| 交易所回報終局狀態（`apply` → `is_terminal_status`） | ✅ 唯一路徑 |
| 我們送出撤單 | ❌ |
| 撤單 API 回 `accepted` | ❌（只是「請求被接受」，不是「訂單死了」） |
| 撤單 API 回 `gone`（已成交/已撤/沒下過） | ❌ 只記 log，仍然去 poll |
| 逾時 | ❌ 轉 `UNKNOWN`，**不是** `DONE` |

還有一個刻意的例外，寫在程式裡：送單當下回 `rejected`（4xx／簽章失敗）時
`order.apply("rejected")` 直接終結。**理由是那個拒絕本身就是交易所的回報**，
不是我們的推測。這是全路徑唯一一處不經 poll 就終結訂單的地方。

**測試**：`test_cancel_does_not_forget_the_order`、
`test_only_exchange_truth_is_terminal`、
`test_quote_cancel_loses_the_race_and_still_hedges`（撤單輸掉競速仍成交 →
**對沖腿仍然送出**，這正是 XEMM 會漏掉的那一筆）。

### 1.2 必須二：部分成交要對沖

XEMM `xemm_executor.py` 全檔 `OrderFilledEvent` 出現 0 次。
我們的對應物是每輪迴圈都跑的 `_consume_maker_fill()`，它讀的是
`order.unhedged`（已成交 − 已對沖），**這個數字從第一筆部分成交就有值**。

一個 B1 沒有的細節，寫程式時才浮出來：**「已入帳」與「已對沖」必須分開**。

```python
applied_base   # 已寫進 venue.position 的量 —— 成交當下就要寫，那是事實
hedged_base    # 已交給另一條腿的量 —— 可能要等，因為對沖所有的最小單量
```

合成一個會出兩種錯：要嘛部位少報（等湊到最小單才入帳），要嘛送出一張
對沖所會直接拒絕的單。低於對沖所最小量的零星成交**留在 `unhedged` 累積**，
若訂單就這樣結束，交給既有的淨差額對沖路徑（`_maybe_hedge` → `_hedge`，
B1 已審過）。這裡用的下限是**對沖所自己的 `min_quote`，不是策略的
`min_order_notional`**——這不是「要不要開倉」的決定，是「把已經存在的部位
補完」。

**測試**：`test_quote_partial_fill_is_hedged_before_it_completes`
（明確斷言第一半在訂單還掛著的時候就對沖了）、
`test_applied_and_hedged_advance_separately`、
`test_partial_below_hedge_minimum_accumulates`。

### 1.3 必須三：撤單有自己的預算格，逾時當「可能已成交」

`cancel_timeout_sec` 是獨立設定鍵，**不是** `staleness_sec`、
**不是** `maker_timeout_sec`。逾時的處理：

```python
if order.state != mk.UNKNOWN and order.cancel_overdue(now, cfg.cancel_timeout_sec):
    order.to_unknown(...)          # 不是 DONE
    self.maker_unknown += 1
    log.critical("MAKER ORDER UNRESOLVED: ... 按「可能已成交」處理")
    self._reconcile_evt.set()      # 去讀鏈上真實部位
```

`UNKNOWN` 的四個性質，每一個都是刻意的：

1. **不是終局**——`needs_attention` 仍為 True，仍然繼續 poll、繼續重送撤單。
2. **會擋住下一張報價**——`_scan_maker` 開頭 `if self._maker_open: return None`。
   不知道自己有沒有部位的時候不該再掛一張。
3. **會觸發對帳**——真相從鏈上來，不從我們的猜測來；對帳採信鏈上後，
   淨差額對沖負責把找到的裸倉平掉。這條路徑 B1 已經審過。
4. **永不永久放棄**——每 60 秒再喊一次 CRITICAL 並再觸發一次對帳，
   跟 `_self_rescue` 同一個哲學（輪詢間隔會從 0.25 秒放慢到 2 秒，避免打爆限流）。

唯一的終點是關機：`stop` 之後給 `2 × cancel_timeout_sec`，仍未解決就印
CRITICAL 說明**具體哪一張單、什麼狀態**，然後退出。不假裝解決了。

**測試**：`test_unconfirmed_cancel_is_pessimistic_and_not_final`、
`test_cancel_that_never_confirms_goes_unknown`、
`test_unresolved_order_blocks_the_next_quote`。

---

## 2 掛單特有的風險，以及對應的守衛

B1 那張表是為吃單寫的。掛單多出五種風險，逐一對應：

| 掛單特有的風險 | 守衛 | 在哪 |
|---|---|---|
| **掛了對沖不掉的量** | 尺寸由**對沖所深度**決定，不是自己的簿口 | `plan_maker` → `hedgeable_base` |
| **掛著的時候行情跑掉（逆選擇）** | 每輪用**當下**對沖簿口重算邊際，低於 `maker_min_edge_bps` 立刻撤 | `_maker_cancel_reason` |
| **自己變成過期報價被挑走** | 邊際「太好」也撤（偷自 XEMM，但指向 resting 單） | 同上，`max_edge_bps` 那條 |
| **對沖腿在掛單期間死掉** | 對沖簿口過期／場館 down／被限流 → 撤單 | 同上 |
| **程序死掉留下活著的掛單** | 啟動時掃描並撤掉所有殘單，**撤不掉就拒絕啟動** | `_cancel_stale_orders` |

第五條值得多說一句：那是「撤單輸掉競速」的極端版本——沒有人在看。
所以它跟啟動時的 `strict=True` 對帳同級：**寧可不開跑，也不要在自己不掌握的
掛單旁邊交易。**

另外兩個守衛是併發性質的，寫程式時才發現：

- **對帳採信鏈上數字之後，掛單自己的入帳紀錄立即作廢**
  （`_reconcile_venue` 裡的 `mark_applied(unapplied)` / `mark_hedged(unhedged)`）。
  鏈上讀數在它被讀取的那一刻是**完整的**，之後再把本地的 delta 加上去
  就是同一筆成交算兩次。作廢之後那筆差額歸淨差額對沖管，而
  `_reconcile_positions` 下一行就是 `_maybe_hedge()`。
  測試：`test_reconcile_supersedes_maker_accounting`。
- **入帳與對沖之間持有掛單所的鎖**。中間那幾百毫秒裡，淨差額對沖會看到
  一條單邊部位並去 reduce 它，然後我們的對沖再落地，差額變成鏡像而不是消失。
  `_hedge` 遇到上鎖的場館會跳過並 carry 到下次對帳——那正是這幾百毫秒該有的行為。

---

## 3 舊路徑有沒有被弄壞

| 檢查 | 結果 |
|---|---|
| `mode` 預設 | `taker`，九個既有設定檔載入結果不變（29 個舊測試全過） |
| `_execute` / `_hedge` / `_reconcile_venue` | 只有 `_reconcile_venue` 加了一段，且只在 `_maker_open` 非空時有效果 |
| `_log_csv` | 抽出 `_append_csv`，欄位與輪替行為逐位元相同 |
| `AccountOrdersFeed` | 原本只認終局狀態，現在也記 open 狀態；`watch()`／`_resolve()` 的行為不變（吃單路徑走的仍是同一條） |
| `taker_fee_bps` | 沒動；新增的 `maker_fee_bps` **預設等於它**，不預設 0 |

最後一條是刻意的：**沒有對過帳單的費率不是折扣，是猜測。**
（M1 還沒做，見 `NEXT_SESSION.md` A1。）

---

## 4 自審過程中改掉的四處

### 4.1 ⚠ 真缺口：送單成功會重置錯誤計數器

原本 `_quote` 在送單成功後 `self.consec_errors = 0`。這會讓
「掛單一直成功、對沖一直失敗」這個迴圈**永遠累積不到
`max_consecutive_errors`**——而那正是「一條腿一直失敗」的形狀。
改成只有**乾淨結束的報價**（成交／撤掉／post-only 被拒，且期間沒有對沖錯誤）
才重置。

### 4.2 ⚠ 真缺口：拿不到編號的掛單

兩個場館都在網路呼叫**之前**配好本地編號並在所有回傳裡帶出來，所以正常
路徑一定有 handle。但若場館 adapter 自己拋例外（缺 SDK、斷言失敗），
`handle` 會是 `None`——這時「悲觀行動」根本不存在：**有可能有一張活著的
掛單，而我們沒有編號去撤它。** 原本會在 `UNKNOWN` 裡永遠空轉。
改成直接 `_risk_halt` 並明講要人工去交易所確認。

### 4.3 訂單迴圈崩潰時的會計

`_finish_maker` 原本有三個呼叫點。改成 `_execute_maker` 包一層
`try/except/finally`，**只有一個呼叫點**；迴圈崩潰時記 CRITICAL、停機
（可能有活著的掛單），而且統計與 CSV 仍然寫得出來。

### 4.4 M2 的分母

原本 `maker_posts` 算「送出的報價」，把 post-only 被拒的也算進去，
會**低估成交率**。改成 `maker_rested`（交易所確認上過簿口的）當分母，
post-only 被拒另外計。CSV 的 `outcome` 欄位把四種結局分開，離線算 M2
時不必相信狀態行的那個百分比。

---

## 5 沒有跑過的部分（明寫，免得被當成已驗證）

兩個簽章 SDK 本機都沒裝（`--record-only` 不需要），所以下面每一條都是
**照官方原始碼寫的，不是跑過的**：

| 東西 | 出處（2026-09-04 核對） |
|---|---|
| Lighter `cancel_order(market_index, order_index=<建單時的 client_order_index>)` | `examples/orders/create_modify_cancel_order_http.py`——建單用 `client_order_index=123`，撤單用 `order_index=123` |
| Lighter `ORDER_TIME_IN_FORCE_POST_ONLY = 2`、`ORDER_TYPE_LIMIT = 0` | `lighter/signer_client.py` 常數 |
| Lighter 訂單狀態全集（含 `canceled-post-only`） | `lighter/models/order.py` 的 status enum |
| HL `cancelByCloid`：`{"asset": <asset_id>, "cloid": cloid.to_raw()}` | `hyperliquid/exchange.py:bulk_cancel_by_cloid` |
| HL 批次撤單：`{"a": asset, "o": oid}` | 同檔 `bulk_cancel` |
| HL post-only = `{"limit": {"tif": "Alo"}}` | HL 文件 + SDK order_type |

**撤單編號用 client order index（Lighter）／cloid（HL）帶來兩個好性質**，
兩者都被程式依賴：送單回應還沒回來也能撤；撤兩次跟撤一次結果相同。

第一次實盤前要人工確認的三件事，寫在這裡：
1. Lighter 的 `accountActiveOrders` 需要 `authorization` header——
   `_auth_get` 用 `signer.create_auth_token_with_expiry()`，**沒跑過**。
2. HL `openOrders` 帶 `dex` 參數在 HIP-3 dex 上的行為。**查不出來現在會
   擋住啟動**（§6.4 已補成與 Lighter 對稱）——所以若這個查詢在 HIP-3 上
   不支援，第一次實盤會在啟動時就失敗。那是想要的行為，但要知道會發生。
3. post-only 被拒的錯誤字串是否真的含 "post only"（`_parse_maker` 靠它
   分辨「正常沒掛上」與「真的錯了」）。**猜錯的方向是安全的**：會被當成
   send-failed，記 error 並累積 `consec_errors`，不會變成裸倉。

---

## 6 已知限制（不修，寫下來）

### 6.1 Lighter 沒有 REST 的單一訂單查詢備援
`poll_order` 只讀帳戶 ws 的最新一幀。**這是刻意的**：Lighter 的 REST 帳戶
狀態落後它的 ws 結算，這個專案已經為此付過一次代價（`RECONCILE_GRACE_SEC`
的註解就是那次留下的）。多一個延遲不同的真相來源，等於製造 B4 花了整段
時間消滅的那種分歧。ws 沉默時我們說 `unknown`，然後走對帳——**對帳讀的是
部位，那是最權威的東西。**

### 6.2 沒有「簿口比我們的成交更舊」那道守衛
吃單的 `_scan` 有一條「不對早於本場館上次成交的簿口再開火」。掛單路徑
**沒有抄這條**：安靜的市場裡簿口本來就不更新，抄過來會讓報價在最該掛的
時候掛不出去。代價是對沖所剛被我們吃掉的深度可能還顯示在簿口上，導致下一張
報價的尺寸高估——但這個錯誤有界而且會自己修：掛著的時候每輪都用當下簿口
重算，深度不夠時 `maker_edge_bps` 回 `None` → 一輪內撤單。

### 6.3 一次只掛一張
單配對單倉（`LIVE_50U_SPEC` §5「不做多配對併發」）。兩側同時掛可以提高
成交率，但也讓「哪一筆成交對應哪一筆對沖」變成一個需要證明的問題。
**先量 M2 再說。**

### 6.4 ~~啟動殘單掃描在兩個場館的嚴格度不同~~ ✅ 已補（2026-09-04 晚）
原本 HL 側 `openOrders` 查不到只記 warning 並回 0——而「回 0」讀起來就是
「沒有殘單」，那正是查詢失敗**不支持**的那個結論。現在兩側一致：查不出來
就拒絕啟動，另外 `openOrders` 回 `None` 也視為失敗（分不出「沒有掛單」與
「這個查詢不支援」）。

代價寫明：若 HL 的 `openOrders` 帶 `dex` 在 HIP-3 上真的不支援，實盤會在
啟動時就大聲失敗——**那正是想要的**，總比在不掌握的掛單旁邊交易好。

### 6.5 成交價假設
掛單成交價用我們自己的限價（`fill_px`）。掛單是被動方，成交就發生在自己
的價位上，所以這**不是估計**——但只有在場館不回報均價時才用它；
兩個場館只要給了 `avg_px` 一律以它為準。

---

## 7 沒有找到的問題（明寫，免得下次重查）

- ❌ 沒有「以為沒成交就重送」——`_hedge_maker_fill` **不重試**，失敗就交給
  淨差額對沖，跟 `_execute` 同一個規矩
- ❌ 沒有在對帳裡下修正單
- ❌ 沒有無上限重試（撤單重試有指數退避、上限 5 秒間隔；`UNKNOWN` 的輪詢
  放慢到 2 秒）
- ❌ 沒有裸露的 `except: pass`
- ❌ 沒有任何地方用「我們送出了 X」來推論「X 發生了」

---

## 8 測試清單（`engine/tests/test_maker.py`，22 個）

規格 §7 點名的四個情境：

| 情境 | 測試 | 斷言的重點 |
|---|---|---|
| 掛 → 成交 | `test_quote_fills_then_hedges` | 對沖腿送出、兩腿相加為零 |
| 掛 → 逾時撤 | `test_quote_times_out_and_cancels` | 有撤單、沒部位、沒殘留 |
| 掛 → 部分成交 | `test_quote_partial_fill_is_hedged_before_it_completes` | **第一半在訂單還掛著時就對沖了** |
| 掛 → 撤單輸掉競速仍成交 | `test_quote_cancel_loses_the_race_and_still_hedges` | 那筆成交仍然被對沖（XEMM 會漏） |

另外 18 個涵蓋狀態機本身、報價定價與尺寸、悲觀分支、停機、對帳優先。
`cd engine && python -m pytest -q` → **51 passed**（原 29 + 新 22）。
