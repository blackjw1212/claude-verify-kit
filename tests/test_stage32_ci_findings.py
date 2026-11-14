"""Stage 3.2 — 第一次跨平台 CI(run 43137519055,2026-11-12)找到的問題,每一項一組回歸測試。

  A3  pid_alive 讀程序狀態檔途中程序消失 → 丟例外,而不是回答「已死」(ubuntu / Python 3.12)
  B6  兩個假工具同時往同一份紀錄檔附加 → Windows 上行被蓋掉或剩殘片(三條平行測試偶發)

hook 本身的修正(B2 路徑分隔符號、B3 自訂驗證的輸出、B1 Windows Job Object)在檔案後半;
shellcheck 的已知限制(B4)在 test_validators_basic.py,與它的 ASCII 對照組放在一起。
"""

import builtins
import errno
import json
import os
import subprocess
import sys

import pytest

from conftest import FAKE_IMPL, POSIX, pid_alive, run_hook, write

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


# ---------------------------------------------------------------- B6 假工具的紀錄檔

def test_b6_concurrent_fake_tools_never_lose_or_corrupt_log_lines(fake):
    # 第一次 CI:Windows 上兩個假工具同時附加寫同一份檔案,出現少行(IndexError)與殘行(ValueError)。
    # 這裡一次開 12 個、讓它們的開始與結束都擠在一起,每一個的 start / end 都必須完整讀得回來。
    n = 12
    fake.make("burst", sleep=0.3)
    cfg = fake.bindir / "burst.fake.json"
    env = dict(os.environ, FAKE_TOOL_LOG=str(fake.log))
    procs = [subprocess.Popen([sys.executable, str(FAKE_IMPL), str(cfg)], env=env) for _ in range(n)]
    assert [p.wait(timeout=60) for p in procs] == [0] * n
    rows = fake.calls("burst")
    assert sorted(k for k, _ in rows) == ["end"] * n + ["start"] * n, rows
    assert fake.runs("burst") == n


def test_b6_lines_written_straight_into_the_main_log_are_still_read(fake):
    # test_stage3_final_realworld 的假 npm 直接往 fake.log 寫一行:合併時不能漏掉主檔
    fake.log.write_text("npm start 0\n", encoding="utf-8")
    fake.make("x")
    cfg = fake.bindir / "x.fake.json"
    subprocess.run([sys.executable, str(FAKE_IMPL), str(cfg)], env=dict(os.environ, FAKE_TOOL_LOG=str(fake.log)),
                   check=True)
    assert fake.runs("npm") == 1 and fake.runs("x") == 1
    assert json.dumps(fake.calls())  # 每一列都解析得出 (工具, 種類, 時間)


# ---------------------------------------------------------------- B2 逐檔工具輸出的路徑分隔符號

def test_b2_attributed_lines_are_shown_with_forward_slashes(hook, tmp_path):
    # Windows 上 Python 語法檢查與 shellcheck 印的是 .claude\hooks\gate.py:1:10: …(第一次 CI 實測);
    # 歸屬一直是對的(比對前就換成 /),但顯示出來的那一行原樣保留了反斜線。
    text = ".claude\\hooks\\gate.py:1:10: SyntaxError: invalid syntax\n"
    by, stray = hook.attribute_lines(text, [".claude/hooks/gate.py"], tmp_path / "proj")
    assert by == {".claude/hooks/gate.py": [".claude/hooks/gate.py:1:10: SyntaxError: invalid syntax"]}
    assert stray == 0


def test_b2_only_the_location_prefix_is_rewritten(hook, tmp_path):
    # 訊息本身的反斜線(Windows 路徑、跳脫字元)不能被改掉
    text = "腳本 們\\部署.sh:2:6: note: see C:\\tools\\x and '\\n' [SC2086]\nother line\n"
    by, stray = hook.attribute_lines(text, ["腳本 們/部署.sh"], tmp_path / "proj")
    assert by == {"腳本 們/部署.sh": ["腳本 們/部署.sh:2:6: note: see C:\\tools\\x and '\\n' [SC2086]"]}
    assert stray == 1


# ---------------------------------------------------------------- B3 自訂驗證印出碼頁編不出的字

UNENCODABLE = "標記 😀"     # 只有 UTF 系列編得出來:cp1252、cp950、ASCII 都不行


def _narrow_output_env():
    """讓子程序的 stdout / stderr 用「編不出 UNENCODABLE」的編碼,而且不靠 PYTHONIOENCODING(那是要測的東西)。
    POSIX:C locale + 關掉 UTF-8 模式與 locale 強制轉換 → ASCII。Windows:系統碼頁本身(CI 的 runner 是 cp1252)。"""
    env = {"PYTHONIOENCODING": "", "PYTHONUTF8": "0"}
    if POSIX:
        env.update(LC_ALL="C", LANG="C", LC_CTYPE="C", PYTHONCOERCECLOCALE="0")
    return env


def _probe_stdio(env):
    """同樣的環境下,一支普通的 Python 程式(輸出導向管線)拿到的 stdout / stderr 編碼與錯誤處理。"""
    code = ("import sys, json; print(json.dumps([sys.stdout.encoding, sys.stdout.errors, "
            "sys.stderr.encoding, sys.stderr.errors]))")
    out = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, **env), capture_output=True, check=True)
    return json.loads(out.stdout.decode("ascii"))


def _assert_narrow(env):
    enc, errs, _, _ = _probe_stdio(env)
    try:
        UNENCODABLE.encode(enc)
    except UnicodeEncodeError:
        return enc, errs
    raise AssertionError(f"前提不成立:這個環境的輸出編碼 {enc} 編得出測試字串,測不到 B3")


STDIO_SCRIPT = """
import json, os, sys
open({mark!r}, "w", encoding="utf-8").write(json.dumps({{
    "env": os.environ.get("PYTHONIOENCODING"),
    "stdout": [sys.stdout.encoding, sys.stdout.errors], "stderr": [sys.stderr.encoding, sys.stderr.errors]}}))
print("out:" + {text!r})
print("err:" + {text!r}, file=sys.stderr)
sys.stdout.flush()
sys.stderr.flush()
sys.exit({rc})
"""


def test_b3_override_printing_unencodable_text_does_not_crash(project, tmp_path):
    env = _narrow_output_env()
    enc, errs = _assert_narrow(env)
    assert errs != "backslashreplace"                     # 不修正的話 stdout 是 strict / surrogateescape
    mark = tmp_path / "stdio.json"
    write(project, ".claude/verify.py", STDIO_SCRIPT.format(mark=str(mark), text=UNENCODABLE, rc=0))
    r = run_hook(project, env=env)
    assert not r.blocked, r.reason
    seen = json.loads(mark.read_text(encoding="utf-8"))
    assert seen["stdout"] == [enc, "backslashreplace"]    # 編碼照舊,只換錯誤處理
    assert seen["stderr"] == [enc, "backslashreplace"]


def test_b3_escaped_text_from_stdout_and_stderr_reaches_the_reason(project, tmp_path):
    env = _narrow_output_env()
    _assert_narrow(env)
    write(project, ".claude/verify.py", STDIO_SCRIPT.format(mark=str(tmp_path / "m.json"), text=UNENCODABLE, rc=1))
    r = run_hook(project, env=env)
    assert r.blocked
    esc = UNENCODABLE.encode("ascii", "backslashreplace").decode("ascii")
    assert "out:" + esc in r.reason and "err:" + esc in r.reason, r.reason
    assert "UnicodeEncodeError" not in r.reason, r.reason


def test_b3_a_pythonioencoding_set_by_the_user_is_left_alone(project, tmp_path):
    env = dict(_narrow_output_env(), PYTHONIOENCODING="ascii:strict")
    mark = tmp_path / "stdio.json"
    write(project, ".claude/verify.py", STDIO_SCRIPT.format(mark=str(mark), text=UNENCODABLE, rc=0))
    r = run_hook(project, env=env)
    assert json.loads(mark.read_text(encoding="utf-8"))["env"] == "ascii:strict"
    assert r.blocked and "UnicodeEncodeError" in r.reason, r.reason      # 使用者要 strict 就是 strict


def test_b3_only_python_overrides_get_the_error_handler(hook, tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    for name in ("verify", "verify.sh", "verify.cmd", "verify.bat"):
        assert hook._override_env(tmp_path / name) is None, name
    assert hook._override_env(tmp_path / "verify.py")["PYTHONIOENCODING"] == ":backslashreplace"
    monkeypatch.setenv("PYTHONIOENCODING", "")                 # 空字串:Python 自己也當成沒設
    assert hook._override_env(tmp_path / "verify.py")["PYTHONIOENCODING"] == ":backslashreplace"
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    assert hook._override_env(tmp_path / "verify.py") is None
