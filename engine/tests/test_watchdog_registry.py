# -*- coding: utf-8 -*-
"""看門狗的註冊表與它啟動的東西必須自洽。

===========================================================================
為什麼（2026-09-14，一個真的差點發生的事）
===========================================================================
`arb_watchdog.ps1` 的每個成員是 `(存活簽章, 啟動器.bat)`。它用簽章去比對
行程指令列，比不到就當它死了、用那支 .bat 拉起來。

所以**簽章與啟動器不一致的後果不是「拉不起來」，是「無界地一直拉」**：

    比不到 -> 判定死亡 -> 啟動 .bat -> 新行程的指令列還是比不到
    -> 五分鐘後再判定死亡 -> 再啟動一個 ...

那正是看門狗自己的檔頭在防的 duplicate-scanner bug，只是換成由看門狗
自己製造。而它沒有任何東西會報錯 —— 行程數會慢慢長大。

**今天真的發生過一次**：OPENAI 的簽章寫 `--symbol OPENAI `，而它的 .bat
用 `--symbol OAI `（HL 側是 io:OAI，兩所對同一資產叫不同名字）。
是臨時手動比對抓到的，不是守衛。所以把那次比對變成這支。
"""
import glob
import io
import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
WD = os.path.normpath(os.path.join(ROOT, "..", "ops", "arb_watchdog.ps1"))

MEMBER = re.compile(r"^\s+'([A-Za-z0-9_]+)'\s*=\s*@\(\s*'([^']+)'\s*,"
                    r"\s*'([^']+)'\s*\)", re.M)


def members():
    src = io.open(WD, encoding="utf-8").read()
    out = MEMBER.findall(src)
    # 解析壞掉時這支會變成「零個成員全部通過」—— 那是 2026-08-26 的形狀
    # （SKIP_DIRS 過寬讓一支測試等於不存在）。所以先釘住規模。
    assert len(out) >= 8, "只解析到 %d 個成員，正則可能壞了" % len(out)
    return out


@pytest.mark.parametrize("name,sig,bat", members())
def test_signature_appears_in_its_own_launcher(name, sig, bat):
    """簽章必須真的出現在它要啟動的那支 .bat 裡。

    對不上 = 看門狗每五分鐘多開一個行程，而且不會報錯。
    """
    p = os.path.join(ROOT, bat)
    assert os.path.exists(p), "%s 的啟動器不存在：%s" % (name, bat)
    body = io.open(p, "rb").read().decode("ascii", "replace")
    assert sig in body, (
        "%s 的簽章 %r 不在 %s 裡 —— 看門狗會判定它永遠是死的，"
        "每五分鐘再開一個" % (name, sig, bat))


@pytest.mark.parametrize("name,sig,bat", members())
def test_launcher_config_exists(name, sig, bat):
    """啟動器引用的 --config 必須存在（不存在 = 開機即死，然後無限重啟）。"""
    p = os.path.join(ROOT, bat)
    body = io.open(p, "rb").read().decode("ascii", "replace")
    m = re.search(r"--config\s+(\S+)", body)
    if not m:
        pytest.skip("%s 用預設設定檔" % name)
    cfg = os.path.join(ROOT, m.group(1))
    assert os.path.exists(cfg), "%s 指向不存在的設定 %s" % (name, m.group(1))


def test_every_hmm_launcher_is_registered():
    """反過來：磁碟上的每個 run_recorder_*.bat / run_hmm_*.bat 都要在表上。

    沒登記的啟動器死了不會有人拉它起來 —— 那是 `account_budget.py` 的 B4
    在 live 側擋的同一件事（不在註冊表裡的啟動器），這裡是 record/shadow 側。
    """
    known = {b for _n, _s, b in members()}
    on_disk = {os.path.basename(p) for p in
               glob.glob(os.path.join(ROOT, "run_recorder_*.bat"))
               + glob.glob(os.path.join(ROOT, "run_hmm_*.bat"))}
    # run_scanner.bat 刻意不在表上（掃描器 2026-09-13 搬到 Railway）
    missing = sorted(on_disk - known - {"run_scanner.bat"})
    assert not missing, "這些啟動器沒有登記在看門狗裡：%s" % missing
