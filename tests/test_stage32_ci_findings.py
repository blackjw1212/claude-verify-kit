"""Stage 3.2 — 第一次跨平台 CI(run 43137519055,2026-11-12)找到的問題,每一項一組回歸測試。

  A3  pid_alive 讀程序狀態檔途中程序消失 → 丟例外,而不是回答「已死」(ubuntu / Python 3.12)
  B6  兩個假工具同時往同一份紀錄檔附加 → Windows 上行被蓋掉或剩殘片(三條平行測試偶發)

hook 本身的修正(B1 / B2 / B3)與 shellcheck 的已知限制(B4)在檔案後半。
"""

import builtins
import errno
import os

import pytest

from conftest import POSIX, pid_alive

posix_only = pytest.mark.skipif(not POSIX, reason="POSIX 專屬:Windows 的 pid_alive 走 OpenProcess,不讀程序狀態檔")


# ---------------------------------------------------------------- A3 pid_alive 的競態

def _proc_reads_fail(monkeypatch, exc):
    """讓程序狀態檔「存在、但一讀就丟 exc」:重現 CI 上讀到一半程序消失的時間點。"""
    real_open, real_exists = builtins.open, os.path.exists

    def fake_open(path, *a, **k):
        if str(path).startswith("/proc/"):
            raise exc
        return real_open(path, *a, **k)
    monkeypatch.setattr(os.path, "exists", lambda p: str(p).startswith("/proc/") or real_exists(p))
    monkeypatch.setattr(builtins, "open", fake_open)


@posix_only
def test_a3_process_vanishing_while_its_status_is_read_counts_as_dead(monkeypatch):
    for exc in (ProcessLookupError(errno.ESRCH, "No such process"),      # CI 實測的那一個
                FileNotFoundError(errno.ENOENT, "No such file or directory")):
        with monkeypatch.context() as m:
            _proc_reads_fail(m, exc)
            assert pid_alive(os.getpid()) is False, type(exc).__name__


@posix_only
def test_a3_other_errors_reading_the_status_are_not_hidden(monkeypatch):
    _proc_reads_fail(monkeypatch, OSError(errno.EIO, "Input/output error"))
    with pytest.raises(OSError) as e:
        pid_alive(os.getpid())
    assert e.value.errno == errno.EIO
