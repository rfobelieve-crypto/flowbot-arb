# HMM Gate 0 / Stage 0–1 研究腳本（2026-09-15 從 flow_system 複製）

使用者：「全部複製過去」。這是**複製不是搬移**：flow_system 裡的原檔一個位元組都沒動，
那邊還有別的腳本 import 它們（`research/mrp/spread_arb_realshare.py` 等）。

**兩份會分岔。** 從今天起 HMM 的研究改這裡；flow_system 那份是凍結的歷史，不要回頭改它，
也不要假設兩份一樣。

## 路徑規則

- 每支檔頭第二行寫著原路徑。
- `ROOT` / `ARB` / `ARB_ROOT` 一律指 **arb 根目錄**（原本是 flow_system 根目錄或寫死的
  `C:/Users/rfo/Desktop/flowbot/arb`）。
- 讀寫的結果檔全部在 `research/hmm_gate0/results/`（原本是 flow_system 的 `research/results/`）。
- `D:/flowbot_data/...` 原樣保留。那些資料由 **flow_system 的常駐錄製器**寫
  （`hl_mid.py`、`hl_tape.py`、`lighter_mid.py`、`lighter_tape.py`，看門狗
  `FlowBot_ExitPathsWatchdog`）。錄製器**沒有**複製過來，複製一份會變成雙錄。
- 沒有任何一支 import 或讀取 flow_system 的路徑（`grep flow_system` 只剩註解）。

## 對照表

| 原路徑（flow_system/） | 新路徑（arb/research/hmm_gate0/） | 做什麼 | TODO 節 | 改了路徑 | 沒驗證的地方 |
|---|---|---|---|---|---|
| `research/mm/quote_life.py` | `mm/quote_life.py` | 頂檔存活時間、50 ms 撤單來不來得及 | §1.40 Stage 0 | ROOT、OUT | 沒重跑 |
| `research/mm/inventory_bound.py` | `mm/inventory_bound.py` | 做市方庫存會長多大、倒不倒得掉 | §1.40 Stage 0 → Stage 1 部位上限 | ROOT、OUT | 沒重跑 |
| `research/mm/sweep_markout.py` | `mm/sweep_markout.py` | 被掃單的 markout（逆選擇） | §1.40 Stage 0 | ROOT、OUT | 沒重跑 |
| `research/mm/rotation_validity.py` | `mm/rotation_validity.py` | 輪換選標的的規則前半挑後半驗 | §1.40 | ROOT（本支沒用到） | 沒重跑 |
| `research/mm/candidate_join.py` | `mm/candidate_join.py` | 四個 Stage 0 量測 join 成候選 | §1.40 Stage 0 收尾 | ROOT、RES | 沒重跑；依賴上面三支的輸出 |
| `research/mm/stage1_sizing.py` | `mm/stage1_sizing.py` | Stage 1 挑標的與 size | §1.40 Stage 1 | ROOT、RES | **會打 Lighter REST**（`orderBookDetails`），沒跑 |
| `research/mrp/venue_spread_arb.py` | `mrp/venue_spread_arb.py` | 場館價差套利面板（binance 當對沖腿） | §1.40 開線前 | `ARB` 改相對 | 讀 `arblib.scan_rank.load()`，沒跑 |
| `research/mrp/spread_arb_hedge.py` | `mrp/spread_arb_hedge.py` | 換對沖腿重算毛邊際 | §1.40 開線前 | 加 `ARB_ROOT` | 沒跑 |
| `research/mrp/spread_arb_paired.py` | `mrp/spread_arb_paired.py` | 配對毛邊際，寫 `spread_arb_pairs.json` | §1.40 G1 上游 | 加 `ARB_ROOT`、輸出路徑 | 沒跑 |
| `research/mrp/lighter_flow_gate0.py` | `mrp/lighter_flow_gate0.py` | Gate 0 G1 流量（$/天） | §1.40 Gate 0 | ROOT、PAIRS、OUT | 沒跑；分母錯 15 倍的已知錯在下一支更正 |
| `research/mrp/lighter_flow_fixdenom.py` | `mrp/lighter_flow_fixdenom.py` | G1 重算：分母改實錄時間 | §1.40 Gate 0 | ROOT、PAIRS、sys.path | 沒跑 |
| `research/mrp/spread_arb_realshare.py` | `mrp/spread_arb_realshare.py` | 份額假設換成量到的競爭者數 | §1.40 Gate 0 | ROOT、PAIRS、FLOW、sys.path | 沒跑 |
| `research/hl/gate0.py` | `hl/gate0.py` | HL 執行可行性 Gate 0（每來回要賺幾 bps） | §1.19（HMM 的前身） | ROOT、OUT、用法行 | **會打 HL info API ＋ ws**，沒跑 |
| `research/hl/mm_markout.py` | `hl/mm_markout.py` | 倉位型做市 Gate 0：HL 上當掛單方一筆值多少 | §1.29（HMM 表裡「倉位型做市」） | ROOT、OUT、用法行 | 沒跑 |

結果檔（`results/`，從 flow_system `research/results/` 原樣複製，時間戳保留）：
`quote_life.json`（**23 MB**）、`quote_life_by_coin.json`、`inventory_bound.json`、
`sweep_markout.json`、`mm_candidates.json`、`stage1_sizing.json`、
`spread_arb_pairs.json`（1 MB）、`lighter_flow_gate0.json`、`hl_gate0.json`、`hl_mm_markout.json`。

`arblib/hedge_leg_check.py` 現在讀這裡的 `results/sweep_markout.json`。

## 驗證做到哪

- 14 支全部 `python -m py_compile` 通過（anaconda 3.9）。
- 只驗證了路徑解析到 arb 根目錄、結果檔路徑都指向 `hmm_gate0/results/`。
- **一支都沒有重跑**，所以「複製後輸出與原本相同」是未驗證的。
- 原 `research/results/` 裡本來就沒有 `rotation_validity`、`spread_arb_hedge`、
  `lighter_flow_fixdenom`、`spread_arb_realshare`、`venue_spread_arb` 的 JSON（這幾支只印不寫）。

## 沒有複製的

| 檔 | 理由 |
|---|---|
| `research/hl/hl_mid.py`、`hl_tape.py`、`research/lighter/lighter_mid.py`、`lighter_tape.py`、`research/exit_paths/*`、`research/hl/hl_fuel_recorder.py` | 常駐錄製器，複製會雙錄 |
| `research/mrp/mrp_crossvenue.py`、`mrp_portmanteau.py`、`mrp_sparse.py`、`test_gep_known_answer.py` | §1.38/§1.39 的 MRP（均值回歸組合）線，不是 HMM |
| `research/mrp/scanner_leg_skew.py` | §1.39 掃描器腿間偏移，不是 HMM |
| `research/hl/flow_toxicity.py`、`lighter_spread_probe.py`、`exec_census.py`、`hl_verify.py`、`hl_candles.py`、`onchain_publish.py`、`prereg_fuel_mechanism.py` | 通用或其他線；HMM 各節沒有引用 |
| `research/ops/notify.py` | 已由 `ops/alert.py` 取代 |
| `research/arb_home.py` | flow_system 讀 arb 的橋，屬於 flow_system |
