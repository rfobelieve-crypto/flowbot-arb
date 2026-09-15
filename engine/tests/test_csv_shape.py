# -*- coding: utf-8 -*-
"""每一個 `_append_csv` 的列長度必須等於它的表頭長度。

**為什麼要用 AST 而不是跑一次引擎**:這件事錯了不會拋例外。
`_append_csv` 的契約是「Never raises: a log write must not be able to stop
trading」,所以一列多寫或少寫一格,只會讓那一列之後的每一個欄位**平移**,
而 `pandas.read_csv` 會照樣讀進來、照樣給你一張看起來很正常的表 ——
只是 `px` 那一欄裝的是 `qty`。

這週已經三次往 maker.csv 加欄位（09-14 behind/edge_at_cancel、
09-15 maker_mid/behind_at_fill）,而每一次「有沒有對齊」都只靠眼睛。
這支把它變成會紅的東西。

同族：flow_system 的 `test_public_payload_shape.py`（SELECT 了卻沒 emit）。
"""
import ast
import os
import sys

ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ENGINE, "entropy_arb", "engine.py")


def _headers_and_calls():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    heads = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                # `CSV_HEADER`（沒有前綴）也算 —— 第一版寫 `_CSV_HEADER`,
                # 於是 trades.csv 那個呼叫點被判成「未知表頭」。
                and node.targets[0].id.endswith("CSV_HEADER")
                and isinstance(node.value, ast.List)):
            heads[node.targets[0].id] = len(node.value.elts)
    calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_append_csv"
                and len(node.args) == 3):
            hdr, row = node.args[1], node.args[2]
            if isinstance(hdr, ast.Name) and isinstance(row, ast.List):
                calls.append((node.lineno, hdr.id, len(row.elts)))
    return heads, calls


def test_every_row_matches_its_header():
    heads, calls = _headers_and_calls()
    assert heads, "一個 *_CSV_HEADER 都沒找到 —— 這支測試等於不存在"
    # 掃不到呼叫點的話這支也等於不存在（2026-08-26 的形狀:
    # 一個範圍過寬／過窄的守衛跟一個沒有的守衛一樣）。
    assert len(calls) >= 3, "只掃到 %d 個 _append_csv 呼叫,太少" % len(calls)
    bad = []
    for lineno, name, n in calls:
        want = heads.get(name)
        if want is None:
            bad.append("engine.py:%d 用了未知的表頭 %s" % (lineno, name))
        elif want != n:
            bad.append("engine.py:%d %s 表頭 %d 欄，這一列寫了 %d 格"
                       % (lineno, name, want, n))
    assert not bad, "CSV 欄位對不齊（欄位會整排平移而且不會報錯）:\n  " \
                    + "\n  ".join(bad)


def test_maker_header_has_no_duplicate_names():
    """重複的欄名讓 `pandas` 只留最後一個 —— 前面那欄靜默消失。"""
    sys.path.insert(0, ENGINE)
    from entropy_arb.engine import MAKER_CSV_HEADER
    dup = [c for c in set(MAKER_CSV_HEADER)
           if MAKER_CSV_HEADER.count(c) > 1]
    assert not dup, "maker.csv 有重複欄名:%s" % dup


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
