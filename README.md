# flowbot-arb — 跨場館套利線

從量化線（`flow_system`：V7 指標、流動性獵取、撤單流）**分離出來的獨立
repo**，2026-09-04 搬出。分離不是整理，是風控：這條線未來要碰**自己的
帳戶、自己的憑證**，而量化線的帳戶已經被手動單爆過兩次
（2026-06-05、2026-07-27）。兩邊在檔案系統上、在 git 上、在交易所帳戶上
都不相通。

**目前狀態：只錄不做。** 沒有任何真錢在這條線上，10 個成員全部跑
`--record-only`。

---

## 目錄

```
engine/     錄價與（未來的）執行引擎。fork 自 entropy-arb。
            logs/ 是 320M 的分鐘 CSV 與掃描 CSV，不進 git。
arblib/     判斷層：費率真相源、七桶成本模型、各個判決評分器。
            純檔案輸入輸出，**不碰任何資料庫**。
docs/       戰場、成本、場館、上線規格、對照研究。
ops/        看門狗（每 5 分鐘，用程序命令列判定死活，不用 mtime）。
results/    評分器寫出的判決 JSON。量化線的網站橋接讀這裡。
```

## 兩條線之間只有兩座橋，方向都是單向

`flow_system` 讀這裡、寫 MySQL 給網站看；**這裡永遠不讀 flow_system，
也永遠不碰它的資料庫**。橋的兩端各只有一個檔案：

| 橋 | 在 flow_system 那側 | 讀什麼 |
|---|---|---|
| 網站看板 | `research/arb_publish.py` | `results/arb_*.json` ＋ `import arblib` 的成本模型 |
| 新鮮度／進度 | `research/freshness_board.py`、`prereg_publish.py` | `engine/logs/*/minutes.csv` 的 mtime 與行數 |

flow_system 那側**只有一個檔案知道這個 repo 在哪**：`research/arb_home.py`
（可用 `ARB_HOME` 環境變數覆寫，引擎將來搬去東京 VPS 時就是改那一行）。

> 為什麼是一個常數而不是幾條相對路徑：這次搬家有**十一個**舊路徑持有者，
> 其中一個（Windows 排程裡的看門狗 action）grep 這個 repo 是找不到的。
> mistake.md 2026-08-29 是同一個形狀——改了佈局，只驗了寫入端和剛改過的
> 那個讀取端，另一個讀取端安靜地從零重數，而那個錯誤的數字不會觸發任何告警。

## 風控（`engine/entropy_arb/engine.py`）

實盤模式**沒設風控就拒絕啟動**（`_require_armed_risk_block`）。
`--record-only` 不受影響。

| 開關 | 擋什麼 |
|---|---|
| `max_net_base` | 兩腿差額超標＝已經不是套利了 → HALT |
| `max_gross_usd` | 兩腿名目合計上限，設定打錯時的兜底 |
| `max_daily_loss_usd` | 本次運行的 MTM 虧損下限 |
| `max_consecutive_stale` | 盤口連續 N 次過期——死掉的資料源和安靜的行情長得一樣 |
| `max_edge_bps` | 價差好到不像真的＝**簿口壞了**（停牌／下市／壞點），不是市場慷慨 |
| `halt_flatten_attempts` | 停機後仍以 reduce-only 把裸露平回去 |

**停機不等於放棄。** HALT 只擋新的套利；已經存在的差額會繼續被平掉，
而且預算數的是「連續毫無進展」的次數——`|net|` 只要變小就重置，
所以場館短暫斷線不會耗盡它；用完也不是停止而是降速重試。
**這條線沒有「永久放棄」這個狀態。**

對照：讀過 Hummingbot 的套利執行器，它一腿成交另一腿失敗時重下 3 次就
`stop()`，**已成交那腿就那樣裸著**（`docs/PEER_INFRA.md` 附行號）。

## 進度

| 線 | 狀態 |
|---|---|
| §0.75 跨場館溢價家族 | 錄製中，判決日 09-04~07 |
| §0.75b 全市場掃描器 | 3000+ 配對，升格指標凍結 |
| §1.02 GOLD／NVDA 代幣化股票 | 錄製中 |
| M1 費率回執 | 待使用者開帳號（`docs/M1_CHECKLIST.md`） |
| B3 掛單路徑 | 唯一剩下的引擎缺口（規格見 `docs/PEER_INFRA.md` §7） |

下一步看 `docs/NEXT_SESSION.md`。

## 分支

- `main` — 這個 repo
- `engine-history` — `engine/` 在合併進來之前的 20 筆 commit 歷史
