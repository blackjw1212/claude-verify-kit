"""Stage 3-1 跨平台檢查(Ubuntu / Windows / macOS × Python 3.8–3.13 都跑這一支)。

十項檢查各自對到下面的測試(報告 stage3-cross-platform-results.md 也照這個編號):
  X1 中文 UTF-8 輸入輸出       X2 Windows .cmd / .bat      X3 路徑分隔符號與空白
  X4 中文檔名與目錄            X5 子程序啟動與停止          X6 exit code 傳遞
  X7 JSON 與 exit 2 擋下       X8 防迴圈                    X9 快取的跨平台一致性
  X10 Windows 權限與檔案鎖定

只在某個平台才有意義的測試用 skipif 標出平台與原因;在 Linux 上被跳過的 Windows 測試
在報告裡記為「未驗證」,不是「通過」。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from conftest import HOOK, POSIX, WINDOWS, FakeTools, configure, pid_alive, run_hook, write

windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows 專屬(在其他平台記為未驗證)")
BAD = '{"a": }'
DEBUG = {"CLAUDE_VERIFY_DEBUG": "1"}     # 計劃(plan)只在 debug 模式寫進紀錄


def stop(project, active, **kw):
    return run_hook(project, {"session_id": "s1", "stop_hook_active": active, "hook_event_name": "Stop"}, **kw)


def log(project):
    return [json.loads(x) for x in (project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()]


def plan(project, vid):
    return next((p for p in log(project)[-1].get("plan", []) if p["id"] == vid), None)


@pytest.fixture
def odd_project(tmp_path):
    """路徑同時含空白與中文的專案(X3 + X4)。"""
    p = tmp_path / "我的 專案" / "子 目錄"
    p.mkdir(parents=True)
    return p


@pytest.fixture
def odd_fake(tmp_path):
    """假工具放在含空白與中文的目錄裡:Windows 上走 cmd.exe /s /c "…" 的引號規則。"""
    d = tmp_path / "工具 目錄"
    d.mkdir()
    lg = tmp_path / "calls.log"
    lg.write_text("", encoding="utf-8")
    return FakeTools(d, lg)


# ---------------------------------------------------------------- X1 中文 UTF-8 輸入輸出

def test_x1_chinese_tool_output_reaches_the_reason_intact(project, fake):
    write(project, "部署.sh", "#!/bin/sh\necho $1\n")
    fake.make("shellcheck", rc=1, output="部署.sh:2:6: warning: 變數要加雙引號 [SC2086]")
    r = run_hook(project, env=fake.env)
    assert r.blocked
    assert "部署.sh:2:6: warning: 變數要加雙引號 [SC2086]" in r.reason


@pytest.mark.parametrize("io_encoding", ["ascii", "cp1252", "cp950"])
def test_x1_hook_output_survives_a_non_utf8_stdio(project, io_encoding):
    # Windows 的管線預設是 ANSI 碼頁(英文版 cp1252、繁中版 cp950);用 PYTHONIOENCODING 在任何平台重現。
    write(project, "資料 夾/壞 檔.json", BAD)
    r = run_hook(project, env={"PYTHONIOENCODING": io_encoding})
    assert r.raw_stdout.isascii()                    # stdout 一律 ASCII JSON
    assert r.blocked and "資料 夾/壞 檔.json:1" in r.reason


@pytest.mark.parametrize("io_encoding", ["cp1252", "cp950"])
def test_x1_exit2_stderr_is_utf8_even_when_stdio_is_not(project, io_encoding):
    write(project, "資料.json", BAD)
    configure(project, block_mode="exit2")
    r = run_hook(project, env={"PYTHONIOENCODING": io_encoding})
    assert r.rc == 2
    assert "資料.json:1" in r.stderr and "\\u" not in r.stderr   # 不可以變成跳脫字元


def test_x1_stdin_with_bom_and_chinese_is_parsed(project):
    # 讀不懂輸入 → stop_hook_active 跟著消失 → 防迴圈永遠不放行。所以用「第二次能不能被放行」來判斷
    # 輸入有沒有被正確解析(只看第一次有沒有擋,輸入整個被丟掉也一樣會擋,測不出來)。
    write(project, "a.json", BAD)

    def payload(active):
        return b"\xef\xbb\xbf" + json.dumps(
            {"session_id": "s1", "stop_hook_active": active, "hook_event_name": "Stop",
             "last_assistant_message": "已完成,全部測試通過 ✅"}, ensure_ascii=False).encode("utf-8")

    assert run_hook(project, raw=payload(False)).blocked
    r = run_hook(project, raw=payload(True))
    # 放行(上限或「擋下後沒有變更」)都只會在讀到 stop_hook_active=true 時發生;讀不到就會再擋一次
    assert not r.blocked and "這不是通過" in r.system


def test_x1_cp950_tool_output_is_decoded_with_the_windows_code_page(hook):
    # 程序內單元測試:Windows 的解碼順序是 UTF-8 → OEM → ANSI。這裡直接指定碼頁序列,任何平台都能驗。
    raw = "錯誤:找不到檔案".encode("cp950")
    assert hook.decode_output(raw, ("utf-8", "cp950")) == "錯誤:找不到檔案"
    assert hook.decode_output("\ufeff中文".encode("utf-8")) == "中文"     # BOM 被拿掉


@windows_only
def test_x1_tool_output_in_the_oem_code_page_is_readable(project, fake):
    import ctypes  # noqa: PLC0415
    oem = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    text = {"cp950": "部署.sh:1:1: 錯誤", "cp936": "部署.sh:1:1: 错误"}.get(oem, "deploy.sh:1:1: Fehler \u00fc")
    write(project, "deploy.sh", "#!/bin/sh\n")
    fake.make("shellcheck", rc=1, output=text, encoding=oem)
    r = run_hook(project, env=fake.env)
    assert r.blocked and text in r.reason


# ---------------------------------------------------------------- X2 Windows .cmd / .bat

def test_x2_resolve_argv_builds_a_cmd_exe_line_for_cmd_and_bat(hook):
    # 字串組法本身在任何平台都能驗(實際交給 cmd.exe 執行的部分見下面的 Windows 測試)
    for ext in (".cmd", ".bat"):
        spawn, why = hook.resolve_argv(["tool", "a b"], is_win=True,
                                       which=lambda n, e=ext: r"C:\工具 目錄\tool" + e)
        assert why is None and spawn.startswith('"') and "/d /s /c" in spawn
        assert f'"C:\\工具 目錄\\tool{ext}" "a b"' in spawn


@windows_only
@pytest.mark.parametrize("ext", [".cmd", ".bat"])
def test_x2_batch_override_failure_blocks_with_its_exit_code(project, ext):
    write(project, f".claude/verify{ext}", "@echo src\\main.c:3:1: error: boom\r\n@exit /b 3\r\n")
    r = run_hook(project)
    assert r.blocked and "(exit 3)" in r.reason and "main.c:3:1" in r.reason


@windows_only
@pytest.mark.parametrize("ext", [".cmd", ".bat"])
def test_x2_batch_override_success_passes(project, ext):
    write(project, f".claude/verify{ext}", "@echo ok\r\n@exit /b 0\r\n")
    r = run_hook(project)
    assert not r.blocked and r.stdout == ""


def test_x2_fake_tools_run_through_the_platform_launcher(project, fake):
    # Windows 上 fake.make 產生的是 .cmd:每一個用假工具的測試都順便走過 .cmd 執行路徑。
    assert fake.launcher("shellcheck").suffix == (".cmd" if WINDOWS else "")
    write(project, "a.sh", "#!/bin/sh\n")
    fake.make("shellcheck", rc=0)
    assert not run_hook(project, env=fake.env).blocked
    assert fake.runs("shellcheck") == 1


# ---------------------------------------------------------------- X3 + X4 路徑:空白、中文、分隔符號

def test_x3_x4_paths_with_spaces_and_chinese_are_reported_with_forward_slashes(odd_project):
    write(odd_project, "資料 夾/壞 檔.json", BAD)
    r = run_hook(odd_project)
    assert r.blocked
    assert "資料 夾/壞 檔.json:1" in r.reason        # 所有平台都用 / —— 跟 Claude Code 的路徑寫法一致
    assert "資料 夾\\壞 檔.json" not in r.reason


def test_x3_x4_tool_in_a_directory_with_spaces_and_chinese(odd_project, odd_fake):
    write(odd_project, "腳本 們/部署.sh", "#!/bin/sh\n")
    odd_fake.make("shellcheck", rc=1, output="腳本 們/部署.sh:1:1: error: x")
    r = run_hook(odd_project, env=odd_fake.env)
    assert r.blocked and "腳本 們/部署.sh:1:1" in r.reason
    assert odd_fake.runs("shellcheck") == 1


def test_x3_x4_override_runs_in_a_project_with_spaces_and_chinese(odd_project):
    write(odd_project, ".claude/verify.py",
          "import os, sys\nprint('cwd=' + os.getcwd())\nsys.exit(0 if os.path.isfile('標記 檔.txt') else 1)\n")
    write(odd_project, "標記 檔.txt", "x")
    assert not run_hook(odd_project).blocked
    (odd_project / "標記 檔.txt").unlink()
    r = run_hook(odd_project)
    assert r.blocked and "(exit 1)" in r.reason


def test_x3_x4_fix_then_cache_in_an_odd_path(odd_project):
    f = write(odd_project, "資料 夾/壞 檔.json", BAD)
    assert run_hook(odd_project, env=DEBUG).blocked
    f.write_text("{}", encoding="utf-8")
    assert not run_hook(odd_project, env=DEBUG).blocked
    assert not run_hook(odd_project, env=DEBUG).blocked
    assert log(odd_project)[-1]["decision"] == "allow-unchanged-green"   # 沒變更:走全綠快速路徑


# ---------------------------------------------------------------- X5 子程序啟動與停止

_SPAWN_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({pidfile!r}, "w").write(str(child.pid))
print("started", flush=True)
time.sleep(60)
"""


def test_x5_timeout_kills_the_whole_tree_on_this_platform(project, tmp_path):
    # POSIX:killpg;Windows:taskkill /F /T。孫程序沒被收掉 = 逾時後還在背景吃 CPU。
    pidfile = tmp_path / "grandchild.pid"
    write(project, ".claude/verify.py", _SPAWN_GRANDCHILD.format(pidfile=str(pidfile)))
    configure(project, on_timeout="warn", validators={"override": {"timeout": 2}})
    r = run_hook(project)
    assert r.elapsed < 20, r
    assert not r.blocked and "逾時" in r.system
    gc = int(pidfile.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while pid_alive(gc) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not pid_alive(gc), f"孫程序 {gc} 還活著"
    res = log(project)[-1]["results"][0]
    assert res["status"] == "timeout" and res["action"] == "killed_process_tree"


def test_x5_normal_exit_of_a_tool_is_a_pass(project):
    write(project, ".claude/verify.py", "print('ok')\n")
    r = run_hook(project)
    assert not r.blocked
    assert log(project)[-1]["results"][0]["status"] == "pass"


# ---------------------------------------------------------------- X6 exit code 傳遞

@pytest.mark.parametrize("code", [1, 2, 3, 127, 255])
def test_x6_nonzero_exit_codes_block_and_are_reported(project, code):
    write(project, ".claude/verify.py", f"import sys\nprint('fail here')\nsys.exit({code})\n")
    r = run_hook(project)
    assert r.blocked and f"(exit {code})" in r.reason


def test_x6_fake_tool_exit_code_propagates_through_the_launcher(project, fake):
    write(project, "a.sh", "#!/bin/sh\n")
    fake.make("shellcheck", rc=3, output="a.sh:1:1: error: x")
    r = run_hook(project, env=fake.env)
    assert r.blocked and "(exit 3)" in r.reason


@windows_only
def test_x6_windows_ntstatus_exit_code_is_readable(project):
    write(project, ".claude/verify.py", "import os\nos._exit(-1073741819)\n")     # 0xC0000005 存取違規
    r = run_hook(project)
    assert r.blocked and "0xC0000005" in r.reason


@pytest.mark.skipif(not POSIX, reason="POSIX 專屬:被訊號終止(負的 returncode)")
def test_x6_killed_by_signal_is_reported_as_such(project):
    write(project, ".claude/verify.py", "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    r = run_hook(project)
    assert r.blocked and "被訊號 9 終止" in r.reason


# ---------------------------------------------------------------- X7 JSON 與 exit 2

@pytest.mark.parametrize("mode", ["json", "exit2"])
def test_x7_both_block_modes_work_with_chinese_paths(odd_project, mode):
    write(odd_project, "設定/壞.json", BAD)
    configure(odd_project, block_mode=mode)
    r = run_hook(odd_project)
    if mode == "json":
        assert r.rc == 0 and r.json["decision"] == "block" and "設定/壞.json:1" in r.json["reason"]
    else:
        assert r.rc == 2 and "設定/壞.json:1" in r.stderr


def test_x7_exit2_with_no_such_file_also_emits_json(project):
    # 官方文件:exit 2 + stdout 空 + stderr 像 "No such file or directory" 會被當成 hook 壞掉
    write(project, ".claude/verify.py", "import sys\nprint('cc: error: x.c: No such file or directory')\nsys.exit(1)\n")
    configure(project, block_mode="exit2")
    r = run_hook(project)
    assert r.rc == 2 and r.json and r.json["decision"] == "block"


# ---------------------------------------------------------------- X8 防迴圈

def test_x8_loop_guard_releases_after_the_cap_in_an_odd_path(odd_project):
    f = write(odd_project, "資料.json", BAD)
    configure(odd_project, max_consecutive_blocks=2)
    assert stop(odd_project, False).blocked
    f.write_text(BAD + " ", encoding="utf-8")
    assert stop(odd_project, True).blocked
    f.write_text(BAD + "  ", encoding="utf-8")
    r = stop(odd_project, True)
    assert not r.blocked and "已連續擋下 2 次" in r.system


def test_x8_state_file_is_written_atomically_and_readable(odd_project):
    write(odd_project, "資料.json", BAD)
    stop(odd_project, False)
    state = json.loads((odd_project / ".claude/.verify_state.json").read_text(encoding="utf-8"))
    assert isinstance(state, dict)
    assert not list((odd_project / ".claude").glob("*.tmp"))


# ---------------------------------------------------------------- X9 快取的跨平台一致性

def test_x9_cache_keys_are_posix_relative_paths(odd_project):
    write(odd_project, "深 1/深 2/檔.json", "{}")
    write(odd_project, "頂.json", "{}")
    run_hook(odd_project)
    cache = json.loads((odd_project / ".claude/.verify_cache.json").read_text(encoding="utf-8"))
    keys = list(cache["manifest"])
    assert "深 1/深 2/檔.json" in keys and "頂.json" in keys
    assert not any("\\" in k or os.path.isabs(k) or ":" in k for k in keys), keys


def test_x9_crlf_rewrite_with_same_meaning_is_still_a_change(project):
    # git 的 autocrlf 會在 Windows 上把 LF 換成 CRLF:內容雜湊變了就要重驗,不能靠「看起來一樣」。
    f = write(project, "a.json", b'{\n  "a": 1\n}\n')
    assert not run_hook(project, env=DEBUG).blocked
    f.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')
    run_hook(project, env=DEBUG)
    p = plan(project, "json")
    assert p and p["action"] == "run" and p["files"] == ["a.json"]


def test_x9_case_only_rename_is_seen(project):
    # Windows / macOS 預設檔案系統不分大小寫:只改大小寫的改名要當成新檔案驗證。
    write(project, "Data.json", "{}")
    assert not run_hook(project).blocked
    tmp = project / "tmp-rename.json"
    os.replace(str(project / "Data.json"), str(tmp))
    os.replace(str(tmp), str(project / "data.json"))
    (project / "data.json").write_text(BAD, encoding="utf-8")
    r = run_hook(project)
    assert r.blocked and "data.json:1" in r.reason


# ---------------------------------------------------------------- X10 權限與檔案鎖定

def test_x10_cache_write_retries_when_replace_is_refused(hook, tmp_path, monkeypatch):
    # 模擬:Windows 上目標檔被別的程序開著時 os.replace 會回 PermissionError。前兩次被拒、第三次成功。
    calls = []
    real = os.replace

    def flaky(a, b):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(13, "模擬:目標檔被鎖")
        return real(a, b)

    monkeypatch.setattr(hook.os, "replace", flaky)
    hook.save_cache(tmp_path, types.SimpleNamespace(entries={}), {}, None, "x")
    assert len(calls) == 3
    assert (tmp_path / ".claude" / hook.CACHE_NAME).exists()
    assert not list((tmp_path / ".claude").glob("*.tmp"))


def test_x10_cache_write_that_never_succeeds_leaves_no_temp_file(hook, tmp_path, monkeypatch):
    def refuse(a, b):
        raise PermissionError(13, "模擬:一直被鎖")

    monkeypatch.setattr(hook.os, "replace", refuse)
    monkeypatch.setattr(hook.time, "sleep", lambda s: None)
    assert hook.atomic_write(tmp_path / ".claude" / "x.json", "{}", "測試") is False
    assert not list((tmp_path / ".claude").glob("*.tmp"))


def test_x10_unwritable_cache_path_never_produces_a_wrong_pass(project):
    # 快取路徑被一個目錄佔住(root 也擋得住、各平台都能重現的「寫不進去」):
    # 寫入失敗只能讓下次重驗,不能讓壞檔被放行,也不能讓 hook 當掉。
    (project / ".claude" / ".verify_cache.json").mkdir(parents=True)
    write(project, "a.json", BAD)
    r1 = run_hook(project)
    assert r1.blocked and "a.json:1" in r1.reason
    (project / "a.json").write_text("{}", encoding="utf-8")
    assert not run_hook(project).blocked
    (project / "a.json").write_text(BAD, encoding="utf-8")
    assert run_hook(project).blocked
    assert not list((project / ".claude").glob("*.tmp"))


@windows_only
def test_x10_cache_held_open_by_another_process_on_windows(project):
    # 實機:另一個程序開著快取檔(Windows 不給 FILE_SHARE_DELETE → os.replace 被拒)
    write(project, "a.json", "{}")
    assert not run_hook(project).blocked
    write(project, "a.json", BAD)
    cache = project / ".claude" / ".verify_cache.json"
    holder = subprocess.Popen([sys.executable, "-c",
                               "import sys, time\nf = open(sys.argv[1], 'rb')\nprint('held', flush=True)\n"
                               "time.sleep(30)\n", str(cache)], stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b"held"
        r = run_hook(project)
        assert r.blocked and "a.json:1" in r.reason
    finally:
        holder.kill()
        holder.wait()
    assert not list((project / ".claude").glob("*.tmp"))
    assert run_hook(project).blocked                     # 鎖解除後也不可以沿用舊的通過


@windows_only
def test_x10_readonly_attribute_on_cache_file_does_not_crash(project):
    write(project, "a.json", "{}")
    assert not run_hook(project).blocked
    cache = project / ".claude" / ".verify_cache.json"
    subprocess.run(["attrib", "+R", str(cache)], check=True)
    try:
        write(project, "a.json", BAD)
        r = run_hook(project)
        assert r.blocked and "內部出錯" not in (r.system + r.reason)
    finally:
        subprocess.run(["attrib", "-R", str(cache)], check=True)


def test_x10_hook_file_itself_can_be_run_from_a_path_with_spaces(tmp_path, project):
    # 外掛安裝後 hook 在 ${CLAUDE_PLUGIN_ROOT} 底下,使用者目錄可能有空白(C:\Users\Wang Xiao Ming)
    d = tmp_path / "plugin root 外掛" / "hooks"
    d.mkdir(parents=True)
    copy = d / "verify.py"
    shutil.copy2(str(HOOK), str(copy))
    write(project, "a.json", BAD)
    cp = subprocess.run([sys.executable, str(copy)], input=b"{}", capture_output=True,
                        env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project)), cwd=str(project), timeout=120)
    out = json.loads(cp.stdout.decode("utf-8"))
    assert out["decision"] == "block" and "a.json:1" in out["reason"]
