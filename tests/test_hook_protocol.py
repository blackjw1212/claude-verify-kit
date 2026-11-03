"""Stop hook 協定:放行/擋下、兩種擋法、防無限迴圈、輸出編碼。

協定依據 https://code.claude.com/docs/en/hooks 的 Stop 章節(2026-11 查核)。
"""

import io
import json
import sys

import pytest

from conftest import configure, run_hook, write

BROKEN = '{"a": 1,,}'


def test_clean_project_passes_silently(project):
    write(project, "data.json", '{"ok": true}')
    r = run_hook(project)
    assert r.rc == 0 and not r.blocked
    assert r.stdout == ""


def test_block_json_mode_reports_file_line_col(project):
    write(project, "data.json", BROKEN)
    r = run_hook(project)
    assert r.rc == 0 and r.blocked
    assert "data.json:1:9" in r.reason
    assert "[json]" in r.reason


def test_block_exit2_mode_uses_stderr(project):
    write(project, "data.json", BROKEN)
    configure(project, block_mode="exit2")
    r = run_hook(project)
    assert r.rc == 2
    assert r.stdout == ""
    assert "data.json:1:9" in r.stderr


def test_exit2_mode_adds_json_when_reason_mentions_missing_file(project):
    # 官方文件:Stop hook exit 2 + stdout 空 + stderr 像 "No such file or directory"
    # 會被當成 hook 自己壞掉而不擋。編譯錯誤常含這句,所以要補 JSON。
    write(project, ".claude/verify.sh", 'echo "main.c:3:10: fatal error: foo.h: No such file or directory"\nexit 1\n')
    configure(project, block_mode="exit2")
    r = run_hook(project)
    assert r.rc == 2
    assert r.json and r.json["decision"] == "block"
    assert "foo.h" in r.json["reason"]


def test_stdout_is_ascii_only_json(project):
    write(project, "資料.json", BROKEN)
    r = run_hook(project)
    assert r.blocked
    assert all(b < 128 for b in r.raw_stdout)
    assert "資料.json:1:9" in r.reason


# ---------------------------------------------------------------- 防無限迴圈

def stop(project, active, session="s1", **kw):
    return run_hook(project, {"session_id": session, "stop_hook_active": active,
                              "hook_event_name": "Stop"}, **kw)


def test_loop_guard_allows_when_nothing_changed_since_block(project):
    write(project, "data.json", BROKEN)
    first = stop(project, False)
    assert first.blocked
    second = stop(project, True)
    assert not second.blocked
    assert "沒有新修改" in second.system


def test_loop_guard_reblocks_when_files_changed_but_still_broken(project):
    write(project, "data.json", BROKEN)
    assert stop(project, False).blocked
    write(project, "data.json", '{"a": [1,}')  # 改了,但還是壞的
    again = stop(project, True)
    assert again.blocked
    assert "data.json:1:10" in again.reason


def test_loop_guard_allows_after_fix(project):
    write(project, "data.json", BROKEN)
    assert stop(project, False).blocked
    write(project, "data.json", '{"a": 1}')
    r = stop(project, True)
    assert not r.blocked and r.stdout == ""


def test_loop_guard_detects_deleted_file_as_a_change(project):
    write(project, "data.json", BROKEN)
    write(project, "other.json", BROKEN)
    assert stop(project, False).blocked
    (project / "other.json").unlink()
    r = stop(project, True)
    assert r.blocked  # data.json 仍壞,而且刪檔被視為「有修改」→ 重驗


def test_consecutive_block_cap(project):
    write(project, "data.json", BROKEN)
    configure(project, max_consecutive_blocks=2)
    assert stop(project, False).blocked                  # 第 1 次
    write(project, "data.json", BROKEN + " ")
    assert stop(project, True).blocked                   # 第 2 次
    write(project, "data.json", BROKEN + "  ")
    r = stop(project, True)
    assert not r.blocked and "已連續擋下 2 次" in r.system
    # 使用者下一則訊息(stop_hook_active=false)重新計數
    assert stop(project, False).blocked


def test_legacy_mode_always_allows_when_active(project):
    write(project, "data.json", BROKEN)
    configure(project, rerun_when_active=False)
    assert stop(project, False).blocked
    write(project, "data.json", BROKEN + " ")
    r = stop(project, True)
    assert not r.blocked and r.stdout == ""


def test_loop_guard_survives_windows_console_encoding(project):
    """Windows 主控台預設 cp950:舊版 json.load(sys.stdin) 會在中文訊息上失敗,
    整個輸入(含 stop_hook_active)被丟掉 → 防迴圈失效。"""
    write(project, "data.json", BROKEN)
    env = {"PYTHONIOENCODING": "cp950"}
    msg = "完成了,測試都通過。𠀋"  # 含 cp950 沒有的字
    payload = {"session_id": "s1", "stop_hook_active": False, "last_assistant_message": msg}
    assert run_hook(project, payload, env=env).blocked
    payload["stop_hook_active"] = True
    r = run_hook(project, payload, env=env)
    assert not r.blocked
    assert "沒有新修改" in r.system


def test_garbage_stdin_does_not_crash(project):
    write(project, "data.json", '{"ok": 1}')
    r = run_hook(project, raw=b"\xff\xfe not json")
    assert r.rc == 0 and not r.blocked


def test_skip_verify_file_still_allows(project):
    write(project, "data.json", BROKEN)
    write(project, ".claude/skip-verify", "")
    r = run_hook(project)
    assert r.rc == 0 and r.stdout == ""


def test_green_run_is_cached_until_something_changes(project):
    write(project, "data.json", '{"ok": 1}')
    write(project, "tests/test_x.py", "def test_x():\n    assert True\n")
    assert not run_hook(project).blocked
    log = (project / ".claude" / ".verify.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1
    assert not run_hook(project).blocked      # 沒變更:不重跑,也不寫新紀錄
    log = (project / ".claude" / ".verify.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1
    write(project, "data.json", BROKEN)
    assert run_hook(project).blocked


# ---------------------------------------------------------------- 訊息長度

def test_max_lines_per_item(project):
    lines = "\n".join(f"src/a.c:{i}:1: error: boom {i}" for i in range(1, 51))
    write(project, ".claude/verify.sh", f"cat <<'EOF'\n{lines}\nEOF\nexit 1\n")
    configure(project, max_lines=3)
    r = run_hook(project)
    assert r.blocked
    shown = [ln for ln in r.reason.splitlines() if "error: boom" in ln]
    assert len(shown) == 3
    assert "另 47 行省略" in r.reason


def test_reason_respects_char_cap(project):
    for i in range(40):
        write(project, f"d{i:02d}/" + "x" * 60 + ".json", BROKEN)
    configure(project, max_reason_chars=600, max_lines=50)
    r = run_hook(project)
    assert r.blocked and len(r.reason) <= 600


# ---------------------------------------------------------------- 設定與內部錯誤

def test_invalid_config_falls_back_to_defaults_but_says_so(project):
    write(project, "data.json", BROKEN)
    write(project, ".claude/verify.json", "{bad")
    r = run_hook(project)
    assert r.blocked                      # 設定壞了不可以讓閘門失效
    assert "無法解析" in r.system


def test_unknown_config_key_is_reported(project):
    write(project, "data.json", '{"ok": 1}')
    configure(project, max_line=3)
    r = run_hook(project)
    assert not r.blocked and "max_line" in r.system


def _run_entry(hook, monkeypatch, capsys, project, payload):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    with pytest.raises(SystemExit) as ex:
        hook.entry()
    return ex.value.code, capsys.readouterr()


def test_internal_error_fails_open_but_is_visible(hook, project, monkeypatch, capsys):
    write(project, "data.json", BROKEN)

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(hook, "fingerprint", boom)
    code, out = _run_entry(hook, monkeypatch, capsys, project, {})
    assert code == 0
    assert "kaboom" in json.loads(out.out)["systemMessage"]


def test_crashing_validator_is_a_warning_not_a_block(hook, project, monkeypatch, capsys):
    write(project, "data.json", '{"ok": 1}')

    def check_boom(_ctx):
        raise ValueError("validator exploded")
    monkeypatch.setattr(hook, "BUILTIN_CHECKS", [check_boom] + list(hook.BUILTIN_CHECKS))
    code, out = _run_entry(hook, monkeypatch, capsys, project, {})
    assert code == 0
    msg = json.loads(out.out)
    assert "decision" not in msg
    assert "validator exploded" in msg["systemMessage"]
