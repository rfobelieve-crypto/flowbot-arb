# perp-dex-tools 全本閱讀（2026-09-05）

> 使用者把兩個 repo 放進 `arb/` 當參考，並指出：「之前只有從網址去讀可能漏掉了
> 很多東西，這次一次看清楚。」**那個判斷是對的——第一次只讀了 edgex 一支，
> 兩個結論是錯的**（見第二節）。
>
> 兩個參考 repo 都在 `.gitignore` 裡：讀它、不提交它。它們有自己的 LICENSE
> 與上游，混進本 repo 的歷史會讓「哪些程式是我們的」永遠答不清楚。

## 一、這次讀了什麼

| 檔案 | 讀法 |
|---|---|
| `hedge_mode.py` | 1–75 全讀 |
| `hedge/hedge_mode_edgex.py` | 385–430、765–800、965–1020、1080–1100 |
| **`hedge/hedge_mode_grvt_v2.py`** | **1165–1345（策略主體）、790–808** |
| `hedge/hedge_mode_ext.py` | 640–670、935–955 |
| `exchanges/lighter.py` | 245–330、關鍵字全掃 |
| `exchanges/lighter_custom_websocket.py` | 1–35、300–345、關鍵字全掃 |
| `exchanges/base.py` | 13–33（重試裝飾器） |
| 八支 `hedge_mode_*` | 結構化關鍵字比對（部分成交／對帳／撤單猜測） |

**第二輪（2026-09-05 晚）補讀完**：`trading_bot.py`、`helpers/` 全部四支、
`exchanges/base.py` 全部、`exchanges/edgex.py` 的下單路徑，以及對十一個連接器
與八支 `hedge_mode_*` 的全庫模式掃描（狀態對映、post-only、取整、重試、
風控關鍵字、註解裡的已知坑）。

`hedge_mode_{bp,apex,grvt,nado,standx}` 經比對是**同一份模板的逐venue複製**
（同樣的例外訊息出現在相差 ±2 的行號上），沒有獨有的守衛——所以第二節那個
「只有 grvt_v2 有差額檢查與自適應中樞」的結論成立。

---

## 二、更正第一次讀錯的兩件事

我第一次只讀 `hedge_mode_edgex.py`，然後對整個家族下了結論。**兩條是錯的：**

### ❌ 錯誤一：「它沒有兩腿差額檢查」

`hedge_mode_grvt_v2.py:1175-1191` 有，而且會停機：

```python
async def check_position_balance(self):
    while attempts < 4:
        self.lighter_position = await self.get_lighter_position()   # 讀真實部位
        self.grvt_position  = await self.get_grvt_position()        # 兩邊都讀
        if abs(self.grvt_position + self.lighter_position) > self.order_quantity:
            log.error("Position imbalance")
            await asyncio.sleep(5)
        else:
            return True
    return False
# 呼叫端 :1281-1284
if not position_is_balanced:
    self.stop_flag = True
    break
```

**這就是 `max_net_base` 的對應物**——B1 說「兄弟的 `maxDelta` 沒有對應物」，
在這一支裡有。而且它是**每一輪交易迴圈都先跑**，不是事後檢查。

### ❌ 錯誤二：「它的門檻是寫死的」

**最重要的發現，而且它直接打到我們今天遇到的問題。**

`hedge_mode_grvt_v2.py:1290-1297`：

```python
self.spread_history.append(self.lighter_best_bid - self.grvt_best_bid)

if len(self.spread_history) > 1000:
    median_val = statistics.median(data)
    long_grvt_threshold  =   median_val + self.grvt_best_ask * Decimal("0.0002")
    short_grvt_threshold = -(median_val - self.grvt_best_ask * Decimal("0.0002"))
else:
    # "logging spread history. N/1000" —— 樣本不足前不交易
    continue
```

**中樞是最近 1000 筆價差的滾動中位數，帶是它上下各 2 bps。**
不是設定檔裡的常數。而且**樣本湊滿 1000 筆之前完全不交易**。

---

## 三、這個家族其實是兩種不同的策略

第一次讀漏掉的根本原因：我以為八支是同一個策略的八個連接器。不是。

| | 七支（bp/ext/apex/grvt/edgex/nado/standx） | **grvt_v2** |
|---|---|---|
| 形狀 | 在 X 掛 post-only → 成交才去 Lighter 對沖 | **兩腿同時吃單** |
| 門檻 | 固定 `fill_timeout` 5 秒 | **滾動中位數 ± 2 bps** |
| 這是誰的策略 | 是 B3 掛單路徑的形狀 | **就是我們的 `_execute`** |

`grvt_v2` 的主迴圈（`:1305-1341`）：

```python
if lighter_best_bid - grvt_best_ask > long_grvt_threshold and grvt_position <= max_position:
    order_quantity = min(self.order_quantity, self.grvt_best_ask_size)   # 深度上限
    await asyncio.gather(
        self.place_grvt_market_order('buy',  order_quantity),
        self.place_lighter_market_order('sell', order_quantity))          # 兩腿並發
```

**兩腿並發送出、尺寸被頂檔深度夾住、帶滑價保護**（`:800-804`，
`best_ask × 1.002` / `best_bid × 0.998`，即 20 bps；我們是 `leg_slippage_bps: 50`）。
這跟我們 `_execute` 的 `asyncio.gather(buy.send_taker, sell.send_taker)` 是同一段程式的不同寫法。

---

## 四、那個滾動中位數，正好是今天的答案

今天量到：NBIS 的溢價在 09-03 **階躍 15 bps** 並停在那裡，而九個設定檔
**全部 `midline_bps: 0.0`**（`NBIS_REGIME_20260903.md`）。我當時的結論是
「改成 +10 是押注 regime 不再變，維持 0 是押注它會回來，兩個都是押注」。

**這一支給了第三個選項：不押注，讓中樞自己跟著資料走。**

同一個作者的兩支程式，一支（entropy-arb，我們的上游）用設定檔裡的常數
`midline_bps`，另一支（grvt_v2）用滾動中位數。**v2 這個命名說明了方向**：
他自己從固定改成了自適應。

### 但這件事不能今天做，理由有三個

1. **這是策略變更，不是 bug 修正。** `CLAUDE.md` §4：判準凍結後不重寫。
   各配對的 7 天閘門 09-06～09-10 陸續到期，改中樞會污染那個判決。
2. **它換掉一種風險，換來另一種。** 滾動中位數會跟著階躍走——**包括跟著
   一個「我們本來應該拒絕交易」的壞掉市場走**。固定中樞至少會在
   `max_edge_bps` 那裡撞牆。自適應要自己的守衛。
3. **它需要一個「暖機期不交易」的狀態**（他們是 1000 筆）。我們的引擎現在
   沒有這個概念——`--record-only` 與實盤之間只有 `--shadow`，沒有
   「已上線但還在收樣本」。

**建議：09-06 各配對過閘之後，把「滾動中樞」當成一個明確的提案來評估，
連同它需要的守衛一起。** 不是今天偷偷加進去。

---

## 五、逐項對照（這次是全本，不是一支）

| | 他們 | 我們 |
|---|---|---|
| 訂單狀態由交易所推送驅動 | ✅ 全部八支 | ✅ |
| 撤單輸掉競速當成成交 | ✅ `CANCELED && filled>0 → FILLED` | ✅ |
| 重複成交訊息守衛 | ✅ | ✅ 單調成交量 |
| 部位從交易所重讀 | ✅ 六支有 | ✅ 對帳迴圈 |
| **兩腿差額 → 停機** | ✅ 只有 grvt_v2 | ✅ `max_net_base` |
| **自適應中樞** | ✅ 只有 grvt_v2 | ❌ **我們沒有** |
| 深度夾尺寸 | ✅ `min(qty, best_size)` | ✅ `take_fraction` + 深度走訪 |
| 滑價保護 | ✅ 20 bps | ✅ 50 bps（可設定） |
| **部分成交即時對沖** | ❌ 八支都沒有 | ✅ |
| **不確定時讀交易所而非寫猜測** | ❌ edgex 撤單十次後寫入猜測 | ✅ `UNKNOWN` → 對帳 |
| 對沖腿保證送達 | ❌ 被動限價（七支） | ✅ IOC |
| 簿口序列跳號 | ✅ offset 檢查 → 重連 | ✅ nonce 檢查 → 清空重訂 |
| 波動熔斷 | ❌ | ✅ |
| 簿口過期守衛 | ❌ | ✅ |
| 單場次虧損下限 | ❌ | ✅ |
| 毛曝險上限 | ❌ | ✅ |
| 未解釋部位變動偵測 | ❌ | ✅ |
| 停機後自動平倉 | ❌ | ✅ |
| 連接器廣度 | ✅ 十一個 | ❌ 兩個 |

### 部分成交那一條，八支都一樣

`hedge_mode_ext.py:944-945` 是最清楚的證據——它**知道**有部分成交，
然後明確選擇不處理：

```python
if status == 'PARTIALLY_FILLED':
    self.extended_order_status = "OPEN"      # 改標成「還掛著」
```

不是疏漏，是設計選擇。**八支實作、零支即時對沖半筆。**
他們的緩解是 5 秒 `fill_timeout` 把裸露窗口關起來。

---

## 六、他們的連接器裡值得記的兩件事

1. **`_submit_order_with_retry` 沒有重試。** 名字有 `with_retry`，但
   `@query_retry` 只掛在查詢類函式上（`:233` `:433` `:488`），**送單那支沒掛**
   （`exchanges/lighter.py:251`）。名字誤導，行為是安全的——**送單不重試**
   跟我們同一個立場。
2. **`client_order_index = int(time.time()*1000) % 1000000`**（`:288`）
   ——**每 16.7 分鐘繞回一次**。而 Lighter 就是**用這個編號撤單**的。
   協定沒有這個限制（SDK 的 `ClientOrderIndex` 是 `c_longlong`），是他們自己
   選的。一次只有一張單所以踩不到，但那是顆地雷。
   我們用單調遞增不取模，沒有這個問題。

---

## 六之二、讀 `trading_bot.py` 找到我們的一個洞（已修）

`trading_bot.py` 本身是單場館的網格／止盈 bot，不是套利，直接可比的不多。
但它每次做價格決策前都先做這一步（`:457-459`）：

```python
best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(...)
if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
    raise ValueError("No bid/ask data available")
```

**`best_bid >= best_ask` —— 交叉簿口，它拒絕。** 回頭查我們：

| 路徑 | 修之前 |
|---|---|
| `OrderBook.is_fresh()` | ❌ 只看新鮮度，不看合理性 |
| `plan_arb`（吃單） | ❌ 完全沒查 |
| `plan_maker`（掛單） | ⚠ 只查掛單那一腿，不查對沖腿 |
| `_hedge` / `_flatten_step` | ❌ 只用 `is_fresh` |

**沒有任何交易所會報交叉簿口——我們的簿口交叉時，錯的是我們自己那份副本**
（漏掉一筆刪除的 diff、半套用的更新、該消失卻留著的檔位）。而這種錯誤特別
危險：**負價差讀起來就是「立刻有錢賺」**。

修法：新增 `is_crossed()` 與 `tradeable() = is_fresh() and not is_crossed()`，
**六條會送單的路徑全部改用 `tradeable()`**；`plan_arb` 自己再擋一層。

**`is_fresh()` 刻意不動**——錄價器用它數 `samples`，而錄製家族正在閘門中，
改變「什麼算一筆樣本」等於在量測進行中換掉儀器。六個新測試釘住這件事，
包括一個專門斷言 `is_fresh` 對交叉簿口仍然回 True。

## 六之三、全部讀完之後，帶回來並已經修掉的四個缺陷

| # | 我們的缺陷 | 從哪裡看到的 | 狀態 |
|---|---|---|---|
| 1 | **交叉簿口沒擋** —— `is_fresh()` 只看新鮮度，`plan_arb`／`_hedge`／`_flatten_step` 完全沒查 | `trading_bot.py:458` 每次價格決策前都擋 `best_bid >= best_ask` | ✅ 新增 `is_crossed()` / `tradeable()`，六條送單路徑全改 |
| 2 | **ws 參數過緊** —— `ping_interval=15, ping_timeout=15` 一次慢 pong 就斷；`max_queue` 用函式庫預設 32 | `helpers/lighter_ws.py` 用 50/20，註解說新版伺服器要求客戶端定期 ping | ✅ 改 50/20 + `max_queue=1024` |
| 3 | **訂單狀態比對大小寫敏感** —— `FILLED`/`CANCELED` 回 `False`，訂單永遠卡在 `unknown` 擋住後續所有報價 | `extended.py:654` 同時處理兩種拼法、`grvt.py:172` 有 per-venue 狀態表 | ✅ 正規化比對，並把 `PARTIALLY_FILLED`/`NEW` 歸為 open |
| 4 | **掛單路徑不分辨帳戶級撤單** —— 保證金／餘額／持倉限制被當成「行情動了」計入普通撤單 | `base.py` 的 `OrderInfo.cancel_reason` 欄位（我們沒有對應物） | ✅ `is_account_cancel()`，命中就暫停該場館並喊 CRITICAL |

第 2 條有數字支撐：錄製家族全部 log 共 161 次 ws 斷線（Lighter 系 118／HL 系
43），其中 142 次是「no close frame received」、10 次是我們自己送的
keepalive 1011。`max_queue=32` 很可能就是那 142 的成因——每一幀都喚醒策略
迴圈，策略走訪兩邊簿口的期間讀取端沒有排空，32 幀塞滿就 TCP 回壓、伺服器
掛斷。

第 3 條的失敗模式最陰險：它不會下錯單，它會**讓引擎安靜地停住**。

## 六之四、看起來像缺陷但其實是不同目標的兩處

1. **他們的掛單價貼著對面**（`edgex.py:283-289`：買單掛在 `best_ask − tick`）。
   我們是掛在自己這一側的最優價再改善一檔，且**永遠不超過門檻允許的價格**。
   他們優化成交率（README 全篇在講返佣與積分），我們優化邊際——**邊際就是
   我們的全部目的**。不是缺陷，是不同的目標函數。
2. **他們對 post-only 被拒重試 15 次**，每次重抓 BBO 重新定價。我們把它當成
   一次錯過，等下一次簿口更新重新規劃。我們那一圈會重跑全部風控閘門，
   比較慢但比較安全。刻意的差異。

另外一處**我們比較好而讀他們才注意到**：數量取整。他們每一腿各自
quantize 到該場館的 size increment（`paradex.py:336`、`extended.py:345`），
**兩腿的格線不同時會每筆留下固定的差額**。我們用兩所中較粗的那個格線
（`_step = 10 ** -min(size_decimals)`）並且**無條件捨去**，兩腿數量必然一致。

## 七、一句話

**上一次的結論「執行語意比 Hummingbot 正確、風控薄」大方向沒錯，但
「沒有差額檢查、門檻寫死」兩條是只讀一支造成的錯誤結論，已更正。**

真正值得帶回來的只有一個東西：**`grvt_v2` 的滾動中位數中樞**——
它正好是我們今天撞到的 NBIS 階躍問題的第三個選項，而且來自同一個作者的
「v2」，說明他自己也從固定走到了自適應。
**但那是 09-06 過閘之後才該評估的提案，不是今天的改動。**
