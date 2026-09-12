"""Minute recorder: aggregation, rollover, CSV output.

Run:  python3 -m pytest tests/  (or  python3 tests/test_recorder.py)
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.recorder import HEADER, MinuteRecorder  # noqa: E402


def set_book(book, bid, ask):
    book.apply_hl([[{"px": str(bid), "sz": "10"}],
                   [{"px": str(ask), "sz": "10"}]])


def test_minute_aggregation_and_rollover():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)

    t0 = 1_700_000_000.0            # 20s into a minute (boundary at ...020)
    # minute 1: entropy 10 bps rich, then 20 bps rich
    set_book(e_book, 100.09, 100.11)   # mid 100.10
    set_book(h_book, 99.99, 100.01)    # mid 100.00
    rec.sample(t0)
    set_book(e_book, 100.19, 100.21)   # mid 100.20
    rec.sample(t0 + 10)
    # next minute: back to 10 bps rich -> flushes minute 1
    set_book(e_book, 100.09, 100.11)
    rec.sample(t0 + 45)
    rec.close()                        # flushes the partial minute 2

    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [*rows[0]] == HEADER
    assert len(rows) == 2
    m1, m2 = rows
    assert int(m1["samples"]) == 2 and int(m2["samples"]) == 1
    assert abs(float(m1["premium_open_bps"]) - 10.0) < 0.2
    assert abs(float(m1["premium_high_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_close_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_mean_bps"]) - 15.0) < 0.2
    # executable edges: sell = bid_e/ask_h - 1, buy = bid_h/ask_e - 1
    assert abs(float(m2["sell_edge_max_bps"])
               - ((100.09 / 100.01 - 1) * 1e4)) < 0.05
    assert abs(float(m2["buy_edge_max_bps"])
               - ((99.99 / 100.11 - 1) * 1e4)) < 0.05
    # closes carry the last books
    assert float(m2["entropy_bid"]) == 100.09
    assert float(m2["hedge_ask"]) == 100.01


def test_depth_columns_land_and_only_count_levels_inside_the_band():
    """The depth columns must actually reach the CSV, with the right levels.

    Judged on the ARTEFACT (what the file contains), not on "the code has a
    line that writes it" -- the whole point of adding these columns is that
    for months we had the full book in memory and were writing only its
    first level, and nothing anywhere went red about it.

    Reverse proof is built in: the 60 bps level must be absent from d5 and
    d25 and present in d100. If the band filter were dropped, all three
    columns would be equal and this test fails.
    """
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)

    # entropy mid = 100.00; bids at 0, 20 and 60 bps below it.
    e_book.apply_hl([[{"px": "100.00", "sz": "1"},     # 0 bps   -> $100
                      {"px": "99.80", "sz": "1"},      # 20 bps  -> $99.80
                      {"px": "99.40", "sz": "1"}],     # 60 bps  -> $99.40
                     [{"px": "100.00", "sz": "1"}]])
    set_book(h_book, 99.99, 100.01)
    rec.sample(1_700_000_000.0)
    rec.close()

    with open(path, newline="") as fh:
        row = list(csv.DictReader(fh))[0]
    d5, d25, d100 = (float(row["e_bid_d5"]), float(row["e_bid_d25"]),
                     float(row["e_bid_d100"]))
    assert abs(d5 - 100.00) < 0.01            # touch only
    assert abs(d25 - 199.80) < 0.01           # touch + the 20 bps level
    assert abs(d100 - 299.20) < 0.01          # all three
    assert d5 < d25 < d100                    # bands must be nested, not equal
    # the other nine columns exist and are numbers (a missing one would
    # KeyError here, which is the point)
    for side in ("e_ask", "h_bid", "h_ask"):
        for b in (5, 25, 100):
            float(row[f"{side}_d{b}"])


def test_traded_volume_splits_by_side_and_by_offset():
    """成交量必須分方向、分距離落進正確的欄位。

    這一關的重點是**方向不可以被合池**：#1 的容量算法第一步就是拆買賣兩側
    （「常常 80% 的量在同一個方向，那正是套利的成因」），合起來那件事就
    看不到了。方向搞反不會報錯，只會讓「誰在買」整個顛倒。

    順便釘住 offset 的分桶與清算欄。
    """
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)

    set_book(e_book, 99.99, 100.01)          # entropy mid = 100.00
    set_book(h_book, 99.99, 100.01)
    # 吃單方買 $300 @ 100.01（離 mid 1 bps）、吃單方賣 $100 @ 99.50（50 bps）
    e_book.on_trade(True, 100.01, 300.0)
    e_book.on_trade(False, 99.50, 100.0)
    # hedge 側一筆清算買單 $250 @ 100.00（0 bps）
    h_book.on_trade(True, 100.00, 250.0, True)
    rec.sample(1_700_000_000.0)
    rec.close()

    with open(path, newline="") as fh:
        row = list(csv.DictReader(fh))[0]
    assert float(row["e_buy_usd"]) == 300.0      # 方向沒有被合池
    assert float(row["e_sell_usd"]) == 100.0
    assert int(row["e_ntrd"]) == 2
    assert float(row["e_liq_usd"]) == 0.0
    # offset：1 bps 那筆進全部三桶；50 bps 那筆只進 100 bps 桶
    assert float(row["e_voff5"]) == 300.0
    assert float(row["e_voff25"]) == 300.0
    assert float(row["e_voff100"]) == 400.0
    assert float(row["h_buy_usd"]) == 250.0
    assert float(row["h_liq_usd"]) == 250.0      # 清算另外記
    assert int(row["h_ntrd"]) == 1

    # 排空之後不可以被算第二次（tape 累積在 book 上，沒排空會溢到下一分鐘）
    assert e_book.tape.n == 0 and h_book.tape.n == 0


def test_schema_rotation_actually_renames_and_keeps_the_old_rows():
    """舊 schema 的檔必須**真的**被改名，而且舊資料留著。

    這一關之前不存在，所以一個致命的 bug 潛伏到 2026-09-12：
    `os.replace` 寫在 `with open(self.path)` 區塊**裡面**，行程自己還開著
    那個檔。Linux 允許對已開啟的 fd 改名，**Windows 不允許** —— 於是九個
    錄製器每分鐘 log 一次「rotated to …」然後 PermissionError，一列都沒寫。

    判準是**產物**（檔案有沒有被改名、舊列在不在、新檔的欄位對不對），
    不是「程式碼裡有那一行」——那一行一直都在，而它從來沒有成功過。
    """
    d = tempfile.mkdtemp()
    path = os.path.join(d, "minutes.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["minute_ts", "time_utc", "premium_close_bps"])   # 舊 schema
        w.writerow([1_700_000_000, "2023-11-14T22:13:20Z", "1.23"])

    e_book, h_book = OrderBook(), OrderBook()
    set_book(e_book, 100.09, 100.11)
    set_book(h_book, 99.99, 100.01)
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
    rec.sample(1_700_000_000.0)
    rec.close()

    olds = [f for f in os.listdir(d) if f.endswith(".old")]
    assert len(olds) == 1, f"沒有輪替：{os.listdir(d)}"
    with open(os.path.join(d, olds[0]), newline="") as fh:
        old_rows = list(csv.reader(fh))
    assert old_rows[0] == ["minute_ts", "time_utc", "premium_close_bps"]
    assert old_rows[1][2] == "1.23"          # 舊資料沒有被吃掉

    with open(path, newline="") as fh:
        new_rows = list(csv.reader(fh))
    assert new_rows[0] == HEADER              # 新檔用新 schema
    assert len(new_rows) == 2                 # 標題 + 剛寫的那一分鐘


def test_stale_books_are_skipped():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
    rec.sample(1_700_000_000.0)        # both books empty -> nothing recorded
    set_book(e_book, 100.0, 100.02)    # only one side fresh
    rec.sample(1_700_000_001.0)
    rec.close()
    assert rec.rows_written == 0
    assert not os.path.exists(path)    # no row, no file


def test_append_keeps_single_header():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    for start in (1_700_000_000.0, 1_700_000_060.0):
        rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
        rec.sample(start)
        rec.close()
    with open(path) as fh:
        lines = fh.read().strip().splitlines()
    assert len(lines) == 3             # one header + two rows
    assert lines[0].startswith("minute_ts,")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
