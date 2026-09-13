"""M3：掛單成交之後，行情往哪邊走（逆選擇）。

===========================================================================
它回答的問題，以及為什麼那不是 M2 回答的問題
===========================================================================
M2 問「我們的報價成交得了嗎」，M3 問**「成交到我們的是什麼流」**。
兩者可以完全相反：一個成交率 100% 的報價，如果每次都是在行情要往那個方向
跑之前被吃掉，那它只是在免費提供選擇權。

X4 的判準逐字是：**掛單成交後 60 秒的中價漂移（相對成交價，帶符號），
平均 > −1 bps 代表沒有被系統性挑走。**

    markout_bps = maker_sign x (mid(t+h) − fill_px) / fill_px x 1e4
    maker_sign  = +1 我們買到（之後 mid 漲 = 我們賺）
                  −1 我們賣出（之後 mid 跌 = 我們賺）

**符號是從掛單方看的**，所以正數永遠代表「這筆對我們有利」。
（等價於 §1.29 寫的 `s x (P − mid)/P`，那裡的 s 是吃單方向。）

===========================================================================
兩個一定要寫下來的設計決定
===========================================================================
1. **視窗必須長於該市場簿口的更新間隔。** 60 秒不是隨便挑的：record-only
   實測 Lighter GMX 的簿口 **9.5 秒**沒動過，而 HL 是 0.3 秒。視窗比更新率
   短的話，`mid(t+h)` 常常就等於 `mid(t)`，於是量到的是**半價差本身**而不是
   逆選擇 —— 那正是 TODO §1.41 裡 AI 那個 +57 bps 的假訊號（1 秒視窗套在
   917 ms 的簿口上，拉到 30 秒就翻成 −32.6）。

2. **用掛單那一腿的 mid，不是對沖腿的。** 逆選擇發生在我們掛單的那本簿口上
   —— 是那裡的人選擇吃我們。拿對沖腿的 mid 會把兩所之間的基差混進來。

`settle()` 用**當下**的 mid 結算已到期的那些，所以取樣點會比 t+h 晚一點點；
呼叫端要在行情更新時呼叫（而不是每 30 秒的狀態迴圈），讓那個誤差保持在
一次簿口更新之內。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class Fill:
    ts: float
    px: float
    usd: float
    maker_is_buy: bool


class MarkoutTracker:
    """固定視窗的掛單方 markout。無 I/O、無時鐘 —— 時間由呼叫端傳入。"""

    def __init__(self, horizon_sec: float = 60.0, keep: int = 500) -> None:
        self.horizon = float(horizon_sec)
        self._pending: deque = deque()
        self._done: deque = deque(maxlen=keep)

    # ------------------------------------------------------------- 記錄
    def record(self, ts: float, px: float, usd: float,
               maker_is_buy: bool) -> None:
        if px > 0 and usd > 0:
            self._pending.append(Fill(ts, px, usd, maker_is_buy))

    # ------------------------------------------------------------- 結算
    def settle(self, now: float, mid: Optional[float]) -> int:
        """把已到期的成交用當下的 mid 結算掉，回傳結算了幾筆。

        `mid` 是 None（簿口空／過期）時**什麼都不做** —— 到期的那些留在
        佇列裡等下一次有效的 mid。用一個壞掉的 mid 結算會產生一個看起來
        完全正常的數字，而那比沒有數字糟得多。
        """
        if mid is None or mid <= 0:
            return 0
        n = 0
        while self._pending and now - self._pending[0].ts >= self.horizon:
            f = self._pending.popleft()
            sign = 1.0 if f.maker_is_buy else -1.0
            self._done.append((f.usd, sign * (mid - f.px) / f.px * 1e4))
            n += 1
        return n

    # ------------------------------------------------------------- 讀數
    def summary(self) -> dict:
        """usd 加權才是「我們這段時間實際被挑走多少」；等權會讓一筆 $10 的
        成交和一筆 $60 的一樣重。兩個都報，因為它們不一致本身就是資訊
        （少數大單在被挑走 vs 全體均勻）。"""
        if not self._done:
            return {"n": 0, "pending": len(self._pending),
                    "horizon_sec": self.horizon,
                    "mean_bps": None, "usd_bps": None, "worst_bps": None}
        vals = [v for _, v in self._done]
        w = sum(u for u, _ in self._done)
        return {
            "n": len(self._done),
            "pending": len(self._pending),
            "horizon_sec": self.horizon,
            "mean_bps": sum(vals) / len(vals),
            "usd_bps": (sum(u * v for u, v in self._done) / w) if w else None,
            "worst_bps": min(vals),
        }
