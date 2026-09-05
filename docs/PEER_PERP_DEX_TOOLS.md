# perp-dex-tools：同一個作者的另一支，而且它就是我們的形狀（2026-09-05）

> 使用者 2026-09-05 把兩個 repo 放進 `arb/` 當參考：`entropy-arb-main`
> （我們引擎的上游）與 `perp-dex-tools-main`。**兩個都在 `.gitignore` 裡**
> ——讀它、不提交它：它們有自己的 LICENSE 與上游，混進本 repo 的歷史會讓
> 「哪些程式是我們的」永遠答不清楚。
>
> **紀律照 `PEER_INFRA.md`：只寫我親自讀過的段落，附行號；沒讀的明講。**

## 我實際讀了什麼

| 檔案 | 段落 |
|---|---|
| `hedge_mode.py` | 1–75（入口與參數） |
| `hedge/hedge_mode_edgex.py` | 385–430、765–800、965–1020、1080–1100，以及全檔關鍵字掃描 |
| `exchanges/edgex.py` | 只掃了 `get_account_positions` |
| `README_EN.md` | 前 40 行 |

**沒讀**：另外七支 `hedge_mode_*.py`（bp / ext / apex / grvt / grvt_v2 / nado /
standx，合計約 7,900 行）、`trading_bot.py`、十一個 `exchanges/*.py` 連接器。
下面每一條結論都只來自 edgex 那一支。

---

## 一、它跟我們是同一個形狀，而且參數一模一樣

`hedge_mode.py` 的用法就是我們 B3 的規格：

```
python hedge_mode.py --exchange edgex --ticker BTC --size 0.001 --iter 20
    --fill-timeout 5      # maker order fills 的逾時，預設 5 秒
    --max-position 0.05
```

**支援的八個交易所全部是「X + Lighter」**：backpack、extended、apex、grvt、
edgex、nado、standx（`hedge_mode.py:12-19`）。跟我們一樣把 Lighter 當對沖腿。

流程也一樣：**在 X 掛 post-only → 成交才去 Lighter 對沖 → 5 秒沒成交就撤單**
（`hedge_mode_edgex.py:389-424`）。

## 二、它做對的四件事（我們也這樣做，互相驗證）

1. **訂單狀態由交易所推送驅動，不由本地假設。**
   `place_and_wait` 的迴圈 switch 在 `self.edgex_order_status` 上，而那個值
   只由 ws 的訂單更新設定（`:393-424`）。跟我們 `MakerOrder.apply()`
   「只有交易所回報能推進狀態」同一個原則。
2. **撤單輸掉競速被正確處理。**
   ```python
   if status == 'CANCELED':
       if filled_size > 0:  status = 'FILLED'      # :978-981
   ```
   撤掉但有成交量 → 當成成交。**這正是 XEMM 漏掉的那一筆**，他們沒漏。
3. **重複的 FILLED 訊息有守衛**（`:1015-1017`），所以同一筆成交不會入帳兩次。
   跟我們 `apply()` 的單調成交量是同一個目的。
4. **部位每一輪都從交易所重讀**，不是只靠本地累加
   （`:1217`、`:1225`、`:1298` 都是 `self.edgex_position = await
   self.get_edgex_position()`）。B1 說「對帳只採信真相」——他們也這樣。

## 三、三處我們比較嚴，而且理由具體

### 3.1 部分成交要等訂單終結才對沖

`:1006` 那個分支只有 `status == 'FILLED'` 才呼叫
`handle_edgex_order_update`。訂單**還掛著而且已經成交一半**時，走的是最後
那個 `else:` 分支——**只印一行日誌**（`:1017-1018`）。

所以半筆成交會裸著，直到訂單終結。**這是 XEMM 那個坑的溫和版**：他們有
5 秒的 `fill_timeout` 把窗口關起來，所以裸露時間有界；XEMM 沒有。
我們的 `unhedged` 從**第一筆觀察到的成交**就開始對沖
（`B3_MAKER_AUDIT.md` §1.2）。

### 3.2 撤單十次之後，它把「猜測」寫進部位

```python
if cancel_count > 10:
    if side == 'buy':  self.edgex_position = self.edgex_position + quantity
    else:              self.edgex_position = self.edgex_position - quantity
    raise Exception("Too many cancellations")          # :404-410
```

撤不掉十次之後，**假設整筆成交了**，把完整數量記進本地部位，然後拋例外。

方向上比「假設沒成交」安全（那是兄弟 $1.1M 的病），但它**仍然是把猜測寫進
狀態**——而且這個猜測錯了也會咬人：若訂單其實沒成交，機器人現在相信自己
有一個不存在的部位，接下來的對沖腿就會裸著。

**兩個方向的假設都不安全，唯一安全的是去讀交易所。** 我們的做法是
`to_unknown()` → 觸發對帳 → 採信鏈上（`B3_MAKER_AUDIT.md` §1.3）。
公平地說：他們 `raise` 之後外層會重讀部位，所以實務上窗口很短。

### 3.3 對沖腿是被動限價單

`place_lighter_limit_order(lighter_side, quantity, price)`（`:797`），
價格用 maker 腿的成交價。**省手續費，但對沖單自己也可能不成交**——
那條腿就裸著。我們的對沖腿是帶 `leg_slippage_bps` 上限的 IOC：
貴一點，但「對沖有沒有發生」不是一個開放問題。

## 四、我們沒有的：廣度

`exchanges/` 有十一個連接器（edgex、backpack、paradex、aster、lighter、grvt、
extended、apex、nado、standx、ethereal），全部對 Lighter 對沖。

`PEER_INFRA.md` §四 的結論本來就是「我們在風險軸領先，缺的是它的廣度」——
這支把那句話具體化了：**同一個作者已經替八個場館寫好了對 Lighter 的連接器。**

**但這不改變 `VENUES.md` 的第三層**（現在不要接新場館）。它改變的是：
真的要接的時候，**這裡有一份讀得到的參考實作**，不是從零開始。
（授權要求註明出處，見它的 README。抄要註明，不能默默拿。）

## 五、它沒有的（對照 `PEER_INFRA.md` §四那張表）

只就 edgex 那一支而言，我沒有找到：**波動熔斷、簿口過期守衛、單場次虧損
下限、毛曝險上限、未解釋部位變動偵測、停機後的自動平倉**。
它有的風控是 `max_position`（`:34-37`）與「撤單十次就拋例外」。

**這跟 Hummingbot 的結論同一族**：能跑、連接器多、風控薄。
不同的是這一支的**執行語意比 Hummingbot 正確得多**——第二節那四條，
Hummingbot 的 XEMM 錯了兩條。

## 六、一句話

**它證實了 B3 的形狀是對的**（同一個作者、同樣的 5 秒逾時、同樣的
post-only → 成交 → 對沖），而我們在三個地方更嚴：部分成交即時對沖、
不確定時去讀交易所而不是寫猜測、對沖腿保證送達。

**它值得再讀的是 `exchanges/`**——但那是「接新場館」那一天的事，
不是現在。
