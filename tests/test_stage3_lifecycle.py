"""Stage 3-3 程序生命週期:hook 怎麼死、工具怎麼結束,都不能留下孤兒程序或假的通過紀錄。

  L1 hook 被 SIGKILL(訊號處理器不會執行)       → 外部看門程序收掉工具的整個程序群組
  L2 工具正常結束但留下背景程序                    → 收掉(可用設定 kill_leftover_processes=false 保留舊行為)
  L3 hook 的父程序(Claude Code)死掉               → hook 停掉工具、不寫快取、立刻結束
  L4 每個工具的 PID、耗時、結束原因寫進紀錄
  L5 Windows:Job Object(KILL_ON_JOB_CLOSE),hook 被 TerminateProcess 時整個工作一起結束

Windows 專屬的在其他平台記為未驗證;POSIX 專屬的(SIGKILL、getppid)逐條標明。
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import HOOK, POSIX, WINDOWS, configure, pid_alive, run_hook, write

posix_only = pytest.mark.skipif(not POSIX, reason="POSIX 專屬:SIGKILL / getppid;Windows 對應的是 L5 的 Job Object")
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows 專屬(在其他平台記為未驗證)")

# 自訂驗證:開一個孫程序(記下 PID),自己也睡著 —— 模擬 npm → node、idf.py → ninja 這種多層建置
SLOW_TREE = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({pidfile!r}, "w").write(f"{{child.pid}} {{__import__('os').getpid()}}")
time.sleep(60)
"""

# 正常結束(exit 0),但留下一個背景程序沒收
LEAVES_BACKGROUND = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open({pidfile!r}, "w").write(str(child.pid))
print("ok")
"""


def _spawn_hook(project, env=None):
    e = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), **(env or {}))
    p = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=e, cwd=str(project))
    p.stdin.write(b"{}")
    p.stdin.close()
    return p


def _wait_file(path, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").split()
        time.sleep(0.05)
    raise AssertionError(f"{path} 沒有出現")


def _wait_dead(pids, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(pid_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return False


def _cleanup(pids):
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL if POSIX else signal.SIGTERM)
        except OSError:
            pass


def log(project):
    return [json.loads(x) for x in (project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- L1 hook 被 SIGKILL

@posix_only
def test_l1_sigkilled_hook_leaves_no_orphan_tools(project, tmp_path):
    pidfile = tmp_path / "tree.pid"
    write(project, ".claude/verify.py", SLOW_TREE.format(pidfile=str(pidfile)))
    hook = _spawn_hook(project)
    pids = [int(x) for x in _wait_file(pidfile)]
    try:
        hook.kill()                                # SIGKILL:hook 自己的訊號處理器不會執行
        hook.wait(timeout=10)
        assert _wait_dead(pids), f"hook 被 SIGKILL 後,工具 {pids} 還活著"
    finally:
        _cleanup(pids)


@posix_only
def test_l1_sigkilled_hook_writes_no_success_record(project, tmp_path):
    pidfile = tmp_path / "tree.pid"
    write(project, "a.json", '{"a": }')
    write(project, ".claude/verify.py", SLOW_TREE.format(pidfile=str(pidfile)))
    hook = _spawn_hook(project)
    pids = [int(x) for x in _wait_file(pidfile)]
    try:
        hook.kill()
        hook.wait(timeout=10)
    finally:
        _cleanup(pids)
    cache = project / ".claude/.verify_cache.json"
    assert not cache.exists() or json.loads(cache.read_text(encoding="utf-8")).get("ok_fp") is None
    (project / ".claude/verify.py").unlink()
    assert run_hook(project).blocked                     # 下一次照常驗,壞檔照樣擋


# ---------------------------------------------------------------- L2 正常結束卻留下背景程序

def test_l2_background_process_left_by_a_tool_is_reaped(project, tmp_path):
    pidfile = tmp_path / "bg.pid"
    write(project, ".claude/verify.py", LEAVES_BACKGROUND.format(pidfile=str(pidfile)))
    r = run_hook(project)
    bg = int(_wait_file(pidfile)[0])
    try:
        assert not r.blocked
        assert _wait_dead([bg]), f"工具留下的背景程序 {bg} 在 hook 結束後還活著"
    finally:
        _cleanup([bg])


def test_l2_old_behaviour_is_kept_behind_a_setting(project, tmp_path):
    pidfile = tmp_path / "bg.pid"
    write(project, ".claude/verify.py", LEAVES_BACKGROUND.format(pidfile=str(pidfile)))
    configure(project, kill_leftover_processes=False)
    run_hook(project)
    bg = int(_wait_file(pidfile)[0])
    try:
        time.sleep(0.5)
        assert pid_alive(bg)                             # 明確關掉時維持 Stage 2 的行為
    finally:
        _cleanup([bg])


def test_l2_reaped_leftovers_are_logged(project, tmp_path):
    pidfile = tmp_path / "bg.pid"
    write(project, ".claude/verify.py", LEAVES_BACKGROUND.format(pidfile=str(pidfile)))
    run_hook(project)
    bg = int(_wait_file(pidfile)[0])
    _cleanup([bg])
    res = next(x for x in log(project)[-1]["results"] if x["id"] == "override")  # Final:累加模式下內建檢查也在結果裡,按 id 取自訂驗證那一筆
    assert res.get("leftovers") is True


# ---------------------------------------------------------------- L3 父程序死掉

_WRAPPER = """
import os, subprocess, sys, time
p = subprocess.Popen([sys.executable, {hook!r}], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, cwd={proj!r}, env=dict(os.environ, CLAUDE_PROJECT_DIR={proj!r}))
p.stdin.write(b"{{}}")
p.stdin.close()
print(p.pid, flush=True)
time.sleep(120)
"""


@posix_only
def test_l3_hook_stops_its_tools_when_its_parent_dies(project, tmp_path):
    pidfile = tmp_path / "tree.pid"
    write(project, ".claude/verify.py", SLOW_TREE.format(pidfile=str(pidfile)))
    wrapper = subprocess.Popen([sys.executable, "-c", _WRAPPER.format(hook=str(HOOK), proj=str(project))],
                               stdout=subprocess.PIPE, start_new_session=True)
    hook_pid = int(wrapper.stdout.readline())
    pids = [int(x) for x in _wait_file(pidfile)]
    try:
        wrapper.kill()                                  # Claude Code(hook 的父程序)死掉
        wrapper.wait(timeout=10)
        assert _wait_dead([hook_pid] + pids, timeout=10), "父程序死後,hook 或它的工具還在跑"
    finally:
        _cleanup([hook_pid] + pids)


# ---------------------------------------------------------------- L4 紀錄 PID、耗時、結束原因

def test_l4_log_records_pid_duration_and_end_reason(project):
    write(project, ".claude/verify.py", "import sys\nprint('x')\nsys.exit(3)\n")
    run_hook(project)
    res = next(x for x in log(project)[-1]["results"] if x["id"] == "override")  # Final:累加模式下內建檢查也在結果裡,按 id 取自訂驗證那一筆
    assert isinstance(res["pid"], int) and res["pid"] > 0
    assert res["sec"] >= 0 and res["end"] == "exit"


def test_l4_timeout_end_reason(project):
    write(project, ".claude/verify.py", "import time\ntime.sleep(30)\n")
    configure(project, validators={"override": {"timeout": 1}})
    run_hook(project)
    res = next(x for x in log(project)[-1]["results"] if x["id"] == "override")  # Final:累加模式下內建檢查也在結果裡,按 id 取自訂驗證那一筆
    assert res["end"] == "timeout_killed" and isinstance(res["pid"], int)


@posix_only
def test_l4_signal_end_reason(project):
    write(project, ".claude/verify.py", "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    run_hook(project)
    res = next(x for x in log(project)[-1]["results"] if x["id"] == "override")  # Final:累加模式下內建檢查也在結果裡,按 id 取自訂驗證那一筆
    assert res["end"] == "signal 9"


# ---------------------------------------------------------------- L5 Windows Job Object

@windows_only
def test_l5_terminated_hook_takes_its_job_down_on_windows(project, tmp_path):
    pidfile = tmp_path / "tree.pid"
    write(project, ".claude/verify.py", SLOW_TREE.format(pidfile=str(pidfile)))
    hook = _spawn_hook(project)
    pids = [int(x) for x in _wait_file(pidfile)]
    try:
        hook.kill()                                     # Windows:TerminateProcess,等同 SIGKILL
        hook.wait(timeout=10)
        assert _wait_dead(pids), f"hook 被終止後,工具 {pids} 還活著(Job Object 沒有生效)"
    finally:
        _cleanup(pids)


@windows_only
def test_l5_job_object_is_used_on_windows(project):
    write(project, ".claude/verify.py", "print('ok')\n")
    run_hook(project)
    assert next(x for x in log(project)[-1]["results"] if x["id"] == "override").get("job") is True


# ---------------------------------------------------------------- L1 的延伸

@posix_only
def test_l1_killing_the_hooks_whole_process_group_still_reaps_tools(project, tmp_path):
    # Claude Code 可能對整個程序群組送 SIGKILL:看門程序若跟 hook 同一群組,會一起死、工具留著
    pidfile = tmp_path / "tree.pid"
    write(project, ".claude/verify.py", SLOW_TREE.format(pidfile=str(pidfile)))
    e = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    hook = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=e, cwd=str(project), start_new_session=True)
    hook.stdin.write(b"{}")
    hook.stdin.close()
    pids = [int(x) for x in _wait_file(pidfile)]
    try:
        os.killpg(hook.pid, signal.SIGKILL)
        hook.wait(timeout=10)
        assert _wait_dead(pids), f"hook 的整個群組被 SIGKILL 後,工具 {pids} 還活著"
    finally:
        _cleanup(pids)


@posix_only
def test_l1_killing_one_of_two_concurrent_hooks_leaves_the_other_alone(tmp_path):
    projects, hooks, pids = [], [], []
    for i in range(2):
        proj = tmp_path / f"p{i}"
        proj.mkdir()
        pidfile = tmp_path / f"tree{i}.pid"
        write(proj, ".claude/verify.py", SLOW_TREE.format(pidfile=str(pidfile)))
        projects.append(proj)
        hooks.append(_spawn_hook(proj))
        pids.append([int(x) for x in _wait_file(pidfile)])
    try:
        hooks[0].kill()
        hooks[0].wait(timeout=10)
        assert _wait_dead(pids[0])
        assert all(pid_alive(p) for p in pids[1]), "另一個 hook 的工具被誤殺"
    finally:
        hooks[1].kill()
        hooks[1].wait(timeout=10)
        _cleanup(pids[0] + pids[1])


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="用 /proc 找看門程序(Linux)")
def test_l1_watchdog_does_not_outlive_a_normal_run(project):
    write(project, ".claude/verify.py", "print('ok')\n")
    hook = _spawn_hook(project)
    out = hook.stdout.read()              # stdin 已經關了:3.12 以前的 communicate() 會對它 flush 而丟例外
    hook.wait(timeout=60)
    assert hook.returncode == 0 and b"block" not in out
    # 只認這個 hook 自己的看門程序(argv 最後一個是 hook 的 PID):其他測試故意殺掉的 hook,
    # 看門程序可能還在收尾的寬限期內
    deadline = time.monotonic() + 3
    while True:
        left = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as fh:
                    argv = fh.read().split(b"\0")
                with open(f"/proc/{name}/stat", "rb") as fh:
                    state = fh.read().rsplit(b")", 1)[1].split()[0]
            except OSError:
                continue
            if (len(argv) >= 7 and argv[1:4] == [b"-I", b"-S", b"-c"] and argv[6] == str(hook.pid).encode()
                    and state != b"Z"):
                left.append(int(name))
        if not left or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert not left, f"看門程序 {left} 在 hook 結束後還在"
