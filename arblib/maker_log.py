"""maker.csv 由**幾個檔案**組成 —— 這裡是唯一一份答案。

引擎的 `_append_csv` 在**表頭變了**的時候會 `os.replace(path, path+".old")`
然後開新檔。所以任何只讀 `maker.csv` 的程式,會在我們每次加欄位的那一刻
**安靜地從零重數**。

這不是假設,是這個專案發生過的事（mistake.md 2026-08-29：錄價器輪替
`minutes.csv`,而看板的計數器只數現行檔 -> 進度顯示 137 分鐘而真相是 950,
使用者截圖問「沒有在動了」）。當時的結論逐字是:

> 對任何資料檔做**佈局級變更**之前,先 `grep 檔名` 枚舉**每一個**讀它的程式。

2026-09-14 加 `behind_at_cancel_bps` / `edge_at_cancel_bps` 兩欄時,枚舉出
三個讀者,而**三個都只讀現行檔**:

    arblib/hmm_screen.py          G2 兩側可做性,門檻是 `len(sd) >= 100`
                                  -> 輪替後變「未量」,整個篩選停擺
    engine/tools/missed_fills.py  漏掉的成交 -> 樣本安靜縮水
    engine/tools/maker_uptime.py  在簿口的時間 -> 同上

所以修法不是「三個地方各加一行」,是**讓「這個 log 由哪些檔組成」只有一份
實作**。要再加欄位時,這裡不用改。

**一個已知的限制,寫在這裡免得被當成完整歷史**：`.old` 只有**一代**
（`os.replace` 每次都寫同一個名字）,所以連續輪替兩次會蓋掉更早那一份。
要長期保存得另外搬走,而這支不負責那件事。

讀法由呼叫端自己決定（`csv.DictReader` 或 pandas 都行）—— 這裡只回答
「讀哪幾個檔、什麼順序」。**表頭不同是正常的**:舊世代欄位比較少,
用 DictReader 那一欄就不存在、用 pandas 就是 NaN,兩個都等於「未量」,
而那正是對的語意（mistake.md 2026-09-14：未知狀態不可以長得像已知狀態）。
"""
from __future__ import annotations

import os

__all__ = ["maker_log_paths"]


def maker_log_paths(logdir: str, name: str = "maker.csv") -> list:
    """組成這份 log 的所有檔案,**由舊到新**。都不存在就回空 list。"""
    cur = os.path.join(logdir, name)
    out = [p for p in (cur + ".old", cur) if os.path.exists(p)]
    return out
