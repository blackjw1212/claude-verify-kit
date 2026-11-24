"""Stage 3 Final Acceptance — 任務一:自訂驗證不可以掩蓋內建檢查的失敗。

舊行為(Stage 1 起):專案有 .claude/verify* 時,內建檢查整批不跑(自訂驗證「取代」內建)。
新的預設是 custom_verify_mode = "augment":自訂驗證每次都跑,內建檢查照各自的快取規則照跑,
任何一邊失敗都擋。要回到舊行為,明確設定 custom_verify_mode = "replace"。

另一個同源的缺口:內建檢查走訪檔案時整棵略過 .claude/,所以就算沒有自訂驗證,
.claude/hooks/*.py 的語法錯誤、.claude/settings.json 的格式錯誤也從來沒被檢查過。
"""

import json

import pytest

from conftest import POSIX, configure, run_hook, write

DEBUG = {"CLAUDE_VERIFY_DEBUG": "1"}
HOOK_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/gate.py"'


def custom(project, ok=True, kind="verify.py"):
    """寫一份自訂驗證。kind=verify.py 跨平台;verify.sh 只在 POSIX(照字面驗證 verify.sh)。"""
    if kind == "verify.sh":
        write(project, ".claude/verify.sh", "echo custom-ok\nexit 0\n" if ok else
              "echo 'tests/custom_check.txt:7: custom check failed'\nexit 1\n")
    else:
        write(project, ".claude/verify.py", "print('custom-ok')\n" if ok else
              "import sys\nprint('tests/custom_check.txt:7: custom check failed')\nsys.exit(1)\n")


KINDS = ["verify.py", pytest.param("verify.sh", marks=pytest.mark.skipif(not POSIX, reason="sh 腳本(POSIX)"))]


def log(project):
    return [json.loads(x) for x in (project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 你列的五種情境

@pytest.mark.parametrize("kind", KINDS)
def test_custom_ok_but_python_hook_has_a_syntax_error(project, kind):
    custom(project, ok=True, kind=kind)
    write(project, ".claude/hooks/gate.py", "def main(:\n    pass\n")
    r = run_hook(project)
    assert r.blocked and ".claude/hooks/gate.py:1" in r.reason, r


@pytest.mark.parametrize("kind", KINDS)
def test_custom_ok_but_json_config_is_broken(project, kind):
    custom(project, ok=True, kind=kind)
    write(project, "config/app.json", '{"a": }')
    r = run_hook(project)
    assert r.blocked and "config/app.json:1" in r.reason, r


@pytest.mark.parametrize("kind", KINDS)
def test_custom_ok_but_claude_settings_json_is_broken(project, kind):
    custom(project, ok=True, kind=kind)
    write(project, ".claude/settings.json", '{"hooks": {"Stop": [}')
    r = run_hook(project)
    assert r.blocked and ".claude/settings.json:1" in r.reason, r


@pytest.mark.parametrize("kind", KINDS)
def test_custom_fails_and_builtins_pass(project, kind):
    custom(project, ok=False, kind=kind)
    write(project, "config/app.json", '{"a": 1}')
    r = run_hook(project)
    assert r.blocked and "tests/custom_check.txt:7" in r.reason and "(exit 1)" in r.reason


@pytest.mark.parametrize("kind", KINDS)
def test_both_fail_and_both_are_reported(project, kind):
    custom(project, ok=False, kind=kind)
    write(project, "config/app.json", '{"a": }')
    r = run_hook(project)
    assert r.blocked
    assert "驗證未通過(2 項)" in r.reason
    assert "tests/custom_check.txt:7" in r.reason and "config/app.json:1" in r.reason


def test_replace_mode_keeps_the_old_behaviour_explicitly(project):
    custom(project, ok=True)
    write(project, "config/app.json", '{"a": }')
    write(project, ".claude/hooks/gate.py", "def main(:\n")
    configure(project, custom_verify_mode="replace")
    r = run_hook(project, env=DEBUG)
    assert not r.blocked                                  # 明確選擇「自訂驗證全權負責」
    ids = [x["id"] for x in log(project)[-1]["results"]]
    assert ids == ["override"]


def test_replace_mode_still_blocks_when_custom_fails(project):
    custom(project, ok=False)
    configure(project, custom_verify_mode="replace")
    assert run_hook(project).blocked


# ---------------------------------------------------------------- 累加模式下的快取行為

def test_augment_runs_custom_every_time_and_builtins_by_their_cache(project):
    custom(project, ok=True)
    write(project, "config/app.json", '{"a": 1}')
    assert not run_hook(project, env=DEBUG).blocked
    run_hook(project, env=DEBUG)
    plan = {p["id"]: p["action"] for p in log(project)[-1]["plan"] if p["action"] != "n/a"}
    assert plan.get("override") == "run"                  # 自訂驗證:每次都跑(已核准的決策)
    assert plan.get("json") == "cached"                   # 內建:照它自己的快取規則


def test_augment_default_is_reported_in_the_plan(project):
    custom(project, ok=True)
    write(project, "a.json", "{}")
    run_hook(project, env=DEBUG)
    ids = [x["id"] for x in log(project)[-1]["results"]]
    assert "override" in ids and "json" in ids


def test_invalid_custom_verify_mode_falls_back_to_augment_with_a_note(project):
    custom(project, ok=True)
    write(project, "config/app.json", '{"a": }')
    configure(project, custom_verify_mode="sometimes")
    r = run_hook(project)
    assert r.blocked and "config/app.json:1" in r.reason   # 設定值不認得 → 用安全的預設,不是 replace


# ---------------------------------------------------------------- .claude/ 本身(與自訂驗證無關的缺口)

def test_hook_syntax_error_blocks_even_without_custom_verify(project):
    write(project, ".claude/hooks/gate.py", "def main(:\n    pass\n")
    r = run_hook(project)
    assert r.blocked and ".claude/hooks/gate.py:1" in r.reason


def test_settings_json_syntax_error_blocks_even_without_custom_verify(project):
    write(project, ".claude/settings.json", "{'hooks': {}}")
    r = run_hook(project)
    assert r.blocked and ".claude/settings.json:1" in r.reason


def test_settings_local_json_is_checked_too(project):
    write(project, ".claude/settings.local.json", '{"permissions": {"allow": [}}')
    assert run_hook(project).blocked


def test_hook_command_pointing_at_a_missing_script_blocks(project):
    write(project, ".claude/settings.json", json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": HOOK_CMD}]}]}}))
    r = run_hook(project)
    assert r.blocked and ".claude/hooks/gate.py" in r.reason and "不存在" in r.reason


def test_hook_command_pointing_at_an_existing_script_passes(project):
    write(project, ".claude/hooks/gate.py", "print('ok')\n")
    write(project, ".claude/settings.json", json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 600}]}]}}))
    assert not run_hook(project).blocked


@pytest.mark.parametrize("hooks", [
    {"Stop": {"command": HOOK_CMD}},                         # 事件底下應該是陣列
    {"Stop": [{"command": HOOK_CMD}]},                      # 少了 hooks 陣列那一層
    {"Stop": [{"hooks": [{"type": "command"}]}]},           # command 型別卻沒有 command
    {"Stop": [{"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": "600"}]}]},  # timeout 不是數字
])
def test_malformed_hook_settings_block(project, hooks):
    # 形狀錯的 hooks 設定可能整段被忽略 —— 驗證閘門會安靜地消失
    write(project, ".claude/hooks/gate.py", "print('ok')\n")
    write(project, ".claude/settings.json", json.dumps({"hooks": hooks}))
    r = run_hook(project)
    assert r.blocked and ".claude/settings.json" in r.reason, r


def test_plugin_root_and_absolute_hook_commands_are_not_guessed(project):
    # ${CLAUDE_PLUGIN_ROOT} 與專案外的路徑不在這個專案裡:不猜、不擋
    write(project, ".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify.py"'},
        {"type": "command", "command": "/usr/local/bin/some-gate --strict"}]}]}}))
    assert not run_hook(project).blocked


def test_claude_dir_checks_are_cached_like_other_files(project):
    write(project, ".claude/hooks/gate.py", "print('ok')\n")
    write(project, ".claude/settings.json", "{}")
    write(project, "a.json", "{}")
    run_hook(project, env=DEBUG)
    write(project, "a.json", '{"v": 2}')
    run_hook(project, env=DEBUG)
    plan = {p["id"]: p for p in log(project)[-1]["plan"] if p["action"] != "n/a"}
    assert plan["python"]["action"] == "cached"


def test_deleting_the_script_a_hook_points_at_is_seen_even_though_settings_did_not_change(project):
    # hooks 設定的結果取決於「它指向的腳本存不存在」:只用 settings.json 的內容當快取鍵會沿用舊的通過
    write(project, ".claude/hooks/gate.py", "print('ok')\n")
    write(project, ".claude/settings.json", json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": HOOK_CMD}]}]}}))
    assert not run_hook(project).blocked
    assert not run_hook(project).blocked
    (project / ".claude/hooks/gate.py").unlink()
    r = run_hook(project)
    assert r.blocked and "不存在" in r.reason
