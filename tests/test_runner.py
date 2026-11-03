"""外部工具執行:工具缺失 / 逾時 / 真失敗要分得開;Windows 指令組法;輸出解碼。"""

import json
import os
import sys
import time

import pytest

from conftest import configure, run_hook, write

POSIX = os.name != "nt"


def alive(pid):
    """程序還在跑嗎?被殺掉但沒人回收的殭屍(容器的 PID 1 常不回收)算死。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = f"/proc/{pid}/status"
    if os.path.exists(status):
        with open(status, encoding="utf-8") as fh:
            return not any(ln.startswith("State:") and "Z" in ln.split()[1] for ln in fh)
    return True


# ---------------------------------------------------------------- 工具缺失

@pytest.fixture
def no_tools_env(tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    return {"PATH": str(empty)}


def test_missing_tool_warns_but_does_not_block(project, no_tools_env):
    write(project, "run.sh", "#!/bin/sh\necho hi\n")
    r = run_hook(project, env=no_tools_env)
    assert not r.blocked
    assert "shellcheck" in r.system and "工具缺失" in r.system


def test_missing_tool_can_be_configured_to_block(project, no_tools_env):
    write(project, "run.sh", "#!/bin/sh\necho hi\n")
    configure(project, on_missing="block")
    r = run_hook(project, env=no_tools_env)
    assert r.blocked and "工具缺失" in r.reason


def test_missing_tool_can_be_ignored_per_validator(project, no_tools_env):
    write(project, "run.sh", "#!/bin/sh\necho hi\n")
    configure(project, validators={"shell": {"on_missing": "ignore"}})
    r = run_hook(project, env=no_tools_env)
    assert not r.blocked and r.stdout == ""


def test_override_that_cannot_execute_is_visible(project):
    write(project, ".claude/verify", "echo hi\n", mode=0o644)  # 無副檔名又沒有 +x
    r = run_hook(project)
    assert not r.blocked
    assert "執行錯誤" in r.system or "工具缺失" in r.system


# ---------------------------------------------------------------- 逾時

def read_log(project):
    return [json.loads(x) for x in (project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()]


@pytest.mark.skipif(not POSIX, reason="用 sh 腳本模擬長時間工具")
def test_timeout_blocks_by_default(project):
    # 逾時 = 沒驗完。預設必須擋,不可以當成通過(2026-11-03 使用者審查後改為預設 block)。
    write(project, ".claude/verify.sh", "sleep 30\n")
    configure(project, validators={"override": {"timeout": 1}})
    r = run_hook(project)
    assert r.elapsed < 15, r
    assert r.blocked
    assert "逾時,結果未判定(逾時不算通過)" in r.reason
    assert "validators.override.timeout" in r.reason       # 告訴人可以調哪個值
    assert "不算通過" in r.reason


@pytest.mark.skipif(not POSIX, reason="用 sh 腳本模擬長時間工具")
def test_timeout_warns_only_when_explicitly_configured_kills_tree_and_is_logged(project):
    pidfile = project / "bg.pid"
    write(project, ".claude/verify.sh",
          f'sleep 60 &\necho $! > "{pidfile}"\necho started\nsleep 60\n')
    configure(project, on_timeout="warn", validators={"override": {"timeout": 1}})
    r = run_hook(project)
    assert r.elapsed < 15, r
    assert not r.blocked
    assert "逾時" in r.system and "疑點" in r.system
    bg = int(pidfile.read_text())
    time.sleep(0.2)
    assert not alive(bg)  # 背景孫程序也被收掉了
    rec = read_log(project)[-1]
    assert rec["suspect"] == ["override"]
    res = rec["results"][0]
    # 記錄逾時工具、執行時間、上限、成因、處置
    assert res["id"] == "override" and res["status"] == "timeout"
    assert res["cmd"].startswith("sh ") and res["cmd"].endswith("verify.sh")
    assert 0.9 <= res["sec"] < 10 and res["limit"] == 1.0
    assert res["cause"] == "validator_timeout" and res["action"] == "killed_process_tree"


@pytest.mark.skipif(not POSIX, reason="用 sh 腳本模擬長時間工具")
def test_timeout_can_be_configured_to_block(project):
    write(project, ".claude/verify.sh", "sleep 30\n")
    configure(project, on_timeout="block", validators={"override": {"timeout": 1}})
    r = run_hook(project)
    assert r.blocked and "逾時" in r.reason


@pytest.mark.skipif(not POSIX, reason="用 sh 腳本模擬長時間工具")
def test_global_budget_bounds_total_time(project):
    write(project, ".claude/verify.sh", "sleep 30\n")
    configure(project, budget_seconds=5)
    r = run_hook(project)
    assert r.elapsed < 12, r
    assert r.blocked and "逾時" in r.reason
    res = read_log(project)[-1]["results"][0]
    assert res["cause"] == "budget_limited"   # 是整體預算不夠,不是驗證器自己的上限


@pytest.mark.skipif(not POSIX, reason="用 sh 腳本模擬長時間工具")
def test_timeout_is_never_recorded_as_success(project):
    write(project, ".claude/verify.sh", "sleep 30\n")
    configure(project, on_timeout="warn", validators={"override": {"timeout": 1}})
    r1 = run_hook(project)
    assert not r1.blocked
    # 深度驗收後:全綠紀錄和快取放在同一個檔(同一次原子寫入),不在狀態檔
    cache = json.loads((project / ".claude/.verify_cache.json").read_text(encoding="utf-8"))
    assert cache["ok_fp"] is None                     # 沒有寫入「上次全綠」
    rec = read_log(project)[-1]
    assert [x["status"] for x in rec["results"]] == ["timeout"]
    r2 = run_hook(project)                          # 沒有任何變更也必須重跑,不可走快速路徑
    assert len(read_log(project)) == 2 and r2.elapsed >= 0.9


@pytest.mark.skipif(not POSIX, reason="需要 POSIX 訊號")
def test_process_killed_by_signal_is_a_failure_not_a_pass(project):
    write(project, ".claude/verify.sh", "echo 'segfault-ish'\nkill -9 $$\n")
    r = run_hook(project)
    assert r.blocked
    assert "被訊號 9 終止" in r.reason


def test_real_failure_is_not_confused_with_missing_or_timeout(project):
    write(project, ".claude/verify.py", "import sys\nprint('tests/test_a.py:3: AssertionError')\nsys.exit(1)\n")
    r = run_hook(project)
    assert r.blocked
    assert "tests/test_a.py:3" in r.reason and "(exit 1)" in r.reason


def test_child_process_gets_no_stdin(project):
    # 舊版讓子程序繼承 hook 的 stdin;等輸入的工具會一路卡到逾時。
    write(project, ".claude/verify.py", "import sys\ndata = sys.stdin.read()\nsys.exit(0 if data == '' else 1)\n")
    r = run_hook(project)
    assert not r.blocked, r


# ---------------------------------------------------------------- Windows 指令組法(單元測試)

def fake_which(mapping):
    return lambda name: mapping.get(name)


def test_windows_cmd_shim_goes_through_cmd_exe(hook, monkeypatch):
    monkeypatch.delenv("COMSPEC", raising=False)
    spawn, why = hook.resolve_argv(["npm", "test"], is_win=True,
                                   which=fake_which({"npm": r"C:\Program Files\nodejs\npm.cmd"}))
    assert why is None
    assert spawn == r'"cmd.exe" /d /s /c ""C:\Program Files\nodejs\npm.cmd" test"'


def test_windows_bat_with_cjk_path_and_spaces_in_args(hook, monkeypatch):
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    spawn, why = hook.resolve_argv(["aglint", r"D:\我的 專案\rules.txt"], is_win=True,
                                   which=fake_which({"aglint": r"C:\工具\aglint.bat"}))
    assert why is None
    assert spawn == r'"C:\Windows\system32\cmd.exe" /d /s /c "C:\工具\aglint.bat "D:\我的 專案\rules.txt""'


@pytest.mark.parametrize("bad", ["100%", 'say "hi"', "a\nb"])
def test_windows_cmd_refuses_unescapable_arguments(hook, bad):
    spawn, why = hook.resolve_argv(["npm", bad], is_win=True,
                                   which=fake_which({"npm": r"C:\n\npm.cmd"}))
    assert spawn is None and why[0] == "error"


def test_windows_exe_and_python_scripts_are_spawned_without_shell(hook):
    spawn, _ = hook.resolve_argv(["cargo", "test"], is_win=True,
                                 which=fake_which({"cargo": r"C:\x\cargo.exe"}))
    assert spawn == [r"C:\x\cargo.exe", "test"]
    spawn, _ = hook.resolve_argv(["idf.py", "build"], is_win=True,
                                 which=fake_which({"idf.py": r"C:\esp\idf.py"}))
    assert spawn == [sys.executable, r"C:\esp\idf.py", "build"]


def test_missing_executable_is_reported_as_missing(hook):
    spawn, why = hook.resolve_argv(["idf.py", "build"], which=fake_which({}))
    assert spawn is None and why[0] == "missing"


# ---------------------------------------------------------------- 輸出解碼與摘要

def test_decode_falls_back_to_legacy_codepage(hook):
    data = "測試失敗:找不到檔案".encode("cp950")
    assert hook.decode_output(data, ("utf-8", "cp950")) == "測試失敗:找不到檔案"
    assert hook.decode_output("中文".encode("utf-8"), ("utf-8", "cp950")) == "中文"
    assert hook.decode_output(b"\xef\xbb\xbfok") == "ok"


def test_summary_prefers_project_locations_over_noise(hook, tmp_path):
    out = "\n".join([
        "============================= ERRORS ==============================",
        "Traceback:",
        f"{sys.prefix}/lib/importlib/__init__.py:88: in import_module",
        "tests/test_m.py:1: in <module>",
        "E   ImportError: cannot import name 'helper'",
        "collected 0 items / 1 error",
        "random chatter line",
    ])
    lines = hook.summarize(out, 2, tmp_path)
    assert lines[0] == "tests/test_m.py:1: in <module>"
    assert "ImportError" in lines[1]
    assert lines[2].startswith("…另")


def test_summary_of_node_test_tap_output(hook, tmp_path):
    # node --test 的 TAP:測試自己印的 log 是 "# ..." 診斷行,不該擠掉真正的失敗。
    root = tmp_path.as_posix()
    out = "\n".join([
        "# 讀取設定失敗 SyntaxError: Expected property name",
        "#     at loadPrefs (dash/index.html:472:24)",
        "# Subtest: feed matches schema",
        "not ok 7 - feed matches schema",
        "  ---",
        f"  location: '{root}/backend/test/schema.test.js:23:1'",
        "  failureType: 'testCodeFailure'",
        "  error: |-",
        "    Expected values to be strictly equal:",
        "    1 !== 2",
        "  stack: |-",
        f"    check (file://{root}/backend/test/schema.test.js:19:10)",
        "    async Test.run (node:internal/test_runner/test:1054:7)",
        "# fail 1",
    ])
    lines = hook.summarize(out, 5, tmp_path)
    assert lines[:4] == [
        "not ok 7 - feed matches schema",
        "  location: 'backend/test/schema.test.js:23:1'",
        "    Expected values to be strictly equal:",
        "    1 !== 2",
    ]
    assert lines[4] == "    check (backend/test/schema.test.js:19:10)"
    assert not any("讀取設定失敗" in ln for ln in lines)


def test_timeout_in_builtin_check_is_never_recorded_as_success(project):
    # 自訂驗證(override)路徑本來就不寫「上次全綠」,所以上面那條測不到內建檢查的路徑;
    # 反向測試(M10)抓出這個缺口後補上這條。
    write(project, "tests/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(20)\n")
    configure(project, on_timeout="warn", validators={"pytest": {"timeout": 1}})
    r1 = run_hook(project)
    assert not r1.blocked and "逾時" in r1.system, r1
    # 深度驗收後:全綠紀錄和快取放在同一個檔(同一次原子寫入),不在狀態檔
    cache = json.loads((project / ".claude/.verify_cache.json").read_text(encoding="utf-8"))
    assert cache["ok_fp"] is None
    assert [x["status"] for x in read_log(project)[-1]["results"] if x["id"] == "pytest"] == ["timeout"]
    run_hook(project)                              # 沒有任何變更也必須重跑
    assert len(read_log(project)) == 2
