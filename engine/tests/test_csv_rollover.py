"""表頭變了的時候,舊檔要被搬走、新檔要開得起來。

**2026-09-14 查到這件事在這台機器上從來沒有成功過一次。**

`Engine._append_csv` 的原版把 `os.replace(path, path + ".old")` 寫在
`with open(path) as fh0:` 的**區塊裡面**。Windows 不准對一個自己還開著的
檔案改名（Python 的 `open()` 不帶 FILE_SHARE_DELETE）-> WinError 32;
而 POSIX 上改名一個開著的檔完全合法。所以:

  * 在 Linux 的 CI 上這段**一直是綠的**
  * 在實際跑交易的這台 Windows 上**一直是壞的**
  * 而 `except Exception: log.exception("csv write failed")` 把它吞掉

後果不是「輪替晚一點」——是那個 CSV **從表頭改動的那一刻起完全停止記錄**,
因為下一列會再走一次同一條路。而 maker.csv 正是 M2（成交率）/ M3（逆選擇）
/ M4（兩腿延遲）三個上線判準的唯一證據來源。

**所以這一關不能只跑「預設情境」** —— 那在 POSIX 上驗不到任何東西。
它把「改名之前檔案還開著嗎」直接問出來:包一層 `os.replace`,在被呼叫的
那一刻試著獨占開啟同一個檔。開得起來 = 沒人握著它 = 修法成立,而且這個
斷言在**兩個平台上都有意義**。

同族:mistake.md 2026-08-29（輪替 CSV,下游計數器從零重數)。那次錯在讀的
那一側,這次錯在寫的那一側 —— 而寫的那一側錯得更徹底:根本沒有資料了。
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.engine import Engine  # noqa: E402


class _Bare:
    """只借 `_append_csv`。建一個真的 Engine 要整份設定,而這一關與設定無關。"""
    _append_csv = Engine._append_csv


def test_header_change_rotates_and_keeps_recording():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "maker.csv")
    e = _Bare()

    e._append_csv(p, ["a", "b"], [1, 2])
    e._append_csv(p, ["a", "b"], [3, 4])
    e._append_csv(p, ["a", "b", "c"], [5, 6, 7])      # <- 表頭變了

    assert os.path.exists(p + ".old"), \
        "表頭變了而舊檔沒有被搬走 —— 輪替沒發生"
    old = list(csv.reader(open(p + ".old", encoding="utf-8")))
    assert old[0] == ["a", "b"] and len(old) == 3, old

    cur = list(csv.reader(open(p, encoding="utf-8")))
    assert cur[0] == ["a", "b", "c"], "新檔沒有用新表頭開:%s" % cur[0]
    # **這一行才是這個 bug 的核心**:輪替失敗時,這一列會整個消失,
    # 而且之後每一列都會。
    assert cur[1] == ["5", "6", "7"], "換表頭之後那一列沒有落地:%s" % cur


def test_rotation_does_not_hold_the_file_open():
    """改名的那一刻,這個行程不可以還開著那個檔。

    上面那一關已經會抓到這個 bug —— **但只在 Windows 上**。錯的正是 Windows,
    而 CI 跑 Linux,所以它在該發現的地方是綠的。這一關補的是那個縫:直接問
    「改名的時候還有沒有開著的 handle」,而那個問題在兩個平台上答案一樣。

    **第一版問錯了問題**:我讓它去獨占開啟同一個檔,以為開得起來就代表沒人
    握著。Windows 擋的是**改名**不是第二次開啟（`open()` 預設帶
    FILE_SHARE_READ|WRITE）,所以那個斷言在壞版本下照樣綠 —— 又一個
    「守衛存在但量不到它該量的東西」,而且是寫在一支為了防這件事的檔案裡。
    改成列舉這個行程還沒關掉的檔案物件,那才是那個命題本身。
    """
    import gc
    import io as _io

    d = tempfile.mkdtemp()
    p = os.path.join(d, "maker.csv")
    e = _Bare()
    e._append_csv(p, ["a"], [1])

    seen = {}
    real = os.replace

    def open_handles(target):
        out = []
        for o in gc.get_objects():
            if isinstance(o, _io.IOBase) and not o.closed:
                if os.path.abspath(str(getattr(o, "name", ""))) == target:
                    out.append(o)
        return out

    def spy(src, dst):
        seen["held"] = len(open_handles(os.path.abspath(src)))
        return real(src, dst)

    os.replace = spy
    try:
        e._append_csv(p, ["a", "b"], [1, 2])
    finally:
        os.replace = real

    assert seen.get("held") == 0, (
        "改名的時候這個行程還開著 %s 個 handle —— Windows 上這就是 WinError 32,"
        "而 except 會把它吞掉,那個 CSV 從此不再記錄" % seen.get("held"))


if __name__ == "__main__":                            # pragma: no cover
    test_header_change_rotates_and_keeps_recording()
    test_rotation_does_not_hold_the_file_open()
    print("ok")
