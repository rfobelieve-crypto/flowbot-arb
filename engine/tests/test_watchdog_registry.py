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
    # 豁免必須**具名並寫理由** —— 放寬條件而不寫理由，就是讓這支測試慢慢
    # 變成不存在（mistake.md 2026-08-26 的形狀）。
    EXEMPT = {
        # 掃描器 2026-09-13 搬到 Railway，本機不可以再起第二支。
        "run_scanner.bat":
            "已搬到 Railway；本機再起一支就是 duplicate-scanner bug",
        # 2026-09-14 MET **退場**（不是暫停）。它在 Lighter 的中位成交切片是
        # $0.27，而兩腿最小單都是 $10 -> 93% 的成交對沖不掉，HL 那一腿在
        # 20 分鐘 17 筆成交裡一次都沒被碰到。註冊它 = 讓看門狗把一個已知
        # 跑不起來的策略拉起來送真單。啟動器留著當紀錄，取代者是 FIL。
        "run_hmm_MET.bat":
            "2026-09-14 退場：成交切片 $0.27 vs 對沖最小單 $10，做不了 HMM",
        # 2026-09-14 FIL 停用：一小時 0 成交、只報價 2 次。不是壞掉 ——
        # 淨邊際 +0.46 bps 貼在損益平衡線上（Lighter 半價差 7.86 − HL 2.5
        # − 費用 4.90），所以引擎大部分時間不報價是對的。
        # **這個豁免有恢復條件**：判準修好（G1 改成淨值、M3 改基準）之後
        # 重篩，若 FIL 仍在名單上就把看門狗那行放回去、這裡刪掉。
        "run_hmm_FIL.bat":
            "2026-09-14 停用：淨 +0.46 bps 貼損益平衡線，一小時 0 成交",
        # ---- 2026-09-14 一次退場的八支。啟動器留在磁碟上當紀錄,但不註冊
        #      —— 註冊等於讓看門狗把已知跑不起來的東西拉起來送真單。
        #      要重開任何一支,先讓它過 arblib/hmm_universe.py 的關。
        "run_hmm_AERO.bat":
            "2026-09-14 停用：上線後 Lighter 半價差從 10.57 塌到 5.31 -> 淨 -0.90",
        "run_hmm_GMX.bat":
            "2026-09-14 退場：市場一天只成交 45 筆（條件 4）",
        "run_recorder_CHIP.bat":
            "2026-09-14 退場：切片 $0.03、吃單流 97.4% 單向（G4/G5）",
        "run_recorder_GRAM.bat":
            "2026-09-14 退場：價差太窄,shadow 決策 0 次",
        "run_recorder_MNT.bat":
            "2026-09-14 退場：新的五關判掉",
        "run_recorder_OPENAI.bat":
            "2026-09-14 退場：HIP-3 的 io 池是 $0.00,永遠不可能成交",
        "run_recorder_ANSEM.bat":
            "2026-09-14 退場：HIP-3 的 para 池是 $0.00",
        "run_recorder_MINIMAX.bat":
            "2026-09-14 退場：HIP-3 的 xyz 池是 $0.00",
    }
    missing = sorted(on_disk - known - set(EXEMPT))
    assert not missing, "這些啟動器沒有登記在看門狗裡：%s" % missing
    # 反向：豁免清單不可以留著指向已經不存在的檔案 —— 那會讓下一個人以為
    # 某個啟動器被刻意停用，而它其實只是被刪了。
    # 注意：這裡要用 os.path.exists 而不是 `- on_disk` —— on_disk 只收
    # run_recorder_* / run_hmm_*，而 run_scanner.bat 依定義不在裡面，
    # 用集合差會把它誤判成「不存在」。（第一版就是這樣紅的。）
    stale = sorted(n for n in EXEMPT
                   if not os.path.exists(os.path.join(ROOT, n)))
    assert not stale, "豁免清單裡有不存在的啟動器，該刪了：%s" % stale


def test_no_two_launchers_share_a_signature():
    """磁碟上不可以有兩支啟動器帶同一個 `--symbol X ` 簽章。

    2026-09-14：`make_hmm_config.py` 產 FIL 時同時產了 `run_recorder_FIL.bat`
    （--shadow），而我另外寫了 `run_hmm_FIL.bat`（live）。兩支的簽章都是
    `--symbol FIL ` —— 看門狗照簽章比對，所以它會覺得「有一個活著」就不管；
    而**任何人手動點開另一支，就會有兩個引擎在同一個帳戶的同一個市場上**
    互相看到對方的部位（`unexplained_position_halt` 會跳，但那是事後）。

    這正是本檔開頭那個 duplicate-scanner bug 的形狀，只是換成 duplicate-engine。
    """
    # **只掃真正的指令列，不掃 REM。** 第一版掃整個檔，於是把註解裡的散文
    # 也當成簽章，抓到一個叫 `is` 的「符號」出現在六支 .bat 裡 ——
    # 又一次「自己剛寫的儀器」（mistake.md 2026-07-29 同族）。
    # 救它的是那個輸出本身不合理：`is` 顯然不是一個市場。
    sigs = {}
    for p in (glob.glob(os.path.join(ROOT, "run_recorder_*.bat"))
              + glob.glob(os.path.join(ROOT, "run_hmm_*.bat"))):
        body = io.open(p, "rb").read().decode("ascii", "replace")
        for ln in body.splitlines():
            s = ln.strip()
            if s.lower().startswith("rem") or "main.py" not in s:
                continue
            for m in re.finditer(r"--symbol\s+([A-Za-z0-9_]+)", s):
                sigs.setdefault(m.group(1), []).append(os.path.basename(p))
    assert sigs, "一個簽章都沒解析到 —— 正則或 glob 壞了，這支等於不存在"
    dup = {k: sorted(set(v)) for k, v in sigs.items() if len(set(v)) > 1}
    assert not dup, "同一個簽章有多支啟動器（會變成兩個引擎）：%s" % dup
