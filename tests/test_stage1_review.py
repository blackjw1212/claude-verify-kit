"""Stage 1 最終審查:每個審查項目各自的對應測試(2026-11-03)。

只有標「模擬」的測試是在 Linux 上模擬 Windows 行為,不能當成 Windows 實機驗證。
"""

import json
import os
import shutil
import time

import pytest

from conftest import configure, run_hook, write

BROKEN = '{"a": 1,,}'
POSIX = os.name != "nt"


def stop(project, active, session="s1", **kw):
    return run_hook(project, {"session_id": session, "stop_hook_active": active,
                              "hook_event_name": "Stop"}, **kw)


# ---------------------------------------------------------------- 逾時與錯誤分類(程序內單元測試)

def make_ctx(hook, project, **cfg):
    notes = []
    base = hook.load_config(project, notes)
    base.update(cfg)
    return hook.Ctx(project, base, time.monotonic())


def test_fail_detect_cannot_turn_a_timeout_into_a_pass(hook, project, monkeypatch):
    monkeypatch.setattr(hook, "run_tool", lambda *a, **k: ("timeout", None, "", 2.0))
    ctx = make_ctx(hook, project)
    r = ctx.tool("adblock", "aglint", ["aglint"], fail_detect=lambda rc, text: False)
    assert r.status == "timeout"
    assert hook.policy_of(ctx.cfg, r) == "block"


def test_pytest_probe_timeout_is_a_timeout_not_missing_pytest(hook, project, monkeypatch):
    # 舊寫法:探測 import pytest 逾時 → 回報「沒有 pytest」→ on_missing=warn → 整組測試沒跑就放行。
    write(project, "tests/test_a.py", "def test_a():\n    assert True\n")
    real = hook.run_tool

    def fake(argv, cwd, timeout, env=None, stdin_bytes=None):
        if argv[-1] == "import pytest":
            return "timeout", None, "", timeout
        return real(argv, cwd, timeout, env=env, stdin_bytes=stdin_bytes)
    monkeypatch.setattr(hook, "run_tool", fake)
    results = hook.check_python(make_ctx(hook, project))
    pytest_r = [r for r in results if r.vid == "pytest"]
    assert [r.status for r in pytest_r] == ["timeout"]


def test_pytest_probe_import_error_is_missing(hook, project, monkeypatch):
    write(project, "tests/test_a.py", "def test_a():\n    assert True\n")
    real = hook.run_tool

    def fake(argv, cwd, timeout, env=None, stdin_bytes=None):
        if argv[-1] == "import pytest":
            return "fail", 1, "ModuleNotFoundError: No module named 'pytest'", 0.1
        return real(argv, cwd, timeout, env=env, stdin_bytes=stdin_bytes)
    monkeypatch.setattr(hook, "run_tool", fake)
    results = hook.check_python(make_ctx(hook, project))
    assert [r.status for r in results if r.vid == "pytest"] == ["missing"]


def test_per_file_keeps_failures_found_before_a_timeout(hook, project, monkeypatch):
    a = write(project, "a.dsl", "x")
    b = write(project, "b.dsl", "y")
    calls = iter([("fail", 1, "a.dsl     3:  Error 6126 - syntax error", 0.2),
                  ("timeout", None, "", 5.0)])
    monkeypatch.setattr(hook, "run_tool", lambda *a_, **k: next(calls))
    out = hook._per_file(make_ctx(hook, project), "acpi", "ACPI", [a, b], lambda f: ["iasl", str(f)])
    assert [r.status for r in out] == ["fail", "timeout"]
    assert "a.dsl" in "\n".join(out[0].lines)


def test_budget_stop_keeps_json_failures_found_so_far(hook, project, monkeypatch):
    write(project, "a.json", BROKEN)
    write(project, "b.json", "{}")
    ctx = make_ctx(hook, project)
    left = iter([100.0, 1.0])  # 檢查 a.json 時還有預算,輪到 b.json 時用完
    monkeypatch.setattr(ctx, "remaining", lambda: next(left))
    out = hook.check_json(ctx)
    assert [r.status for r in out] == ["fail", "timeout"]
    assert out[1].meta["cause"] == "budget_exhausted" and out[1].meta["action"] == "stopped_mid_scan"


def test_validator_not_started_when_budget_is_gone(hook, project):
    ctx = make_ctx(hook, project, budget_seconds=1)
    r = ctx.tool("node", "npm test", ["npm", "test"])
    assert r.status == "timeout" and r.meta["action"] == "not_started"
    assert r.meta["cause"] == "budget_exhausted"


@pytest.mark.skipif(not POSIX, reason="用 sh 腳本模擬長時間工具")
def test_loop_guard_release_after_timeout_says_it_was_not_a_pass(project):
    write(project, ".claude/verify.sh", "sleep 30\n")
    configure(project, validators={"override": {"timeout": 1}})
    assert stop(project, False).blocked
    r = stop(project, True)
    assert not r.blocked
    assert "override:timeout" in r.system and "這不是通過" in r.system


# ---------------------------------------------------------------- 防迴圈與 stop_hook_active

def test_default_cap_is_five_consecutive_blocks(project):
    write(project, "data.json", BROKEN)
    assert stop(project, False).blocked                     # 1
    for i in range(2, 6):                                   # 2..5
        write(project, "data.json", BROKEN + " " * i)
        assert stop(project, True).blocked, i
    write(project, "data.json", BROKEN + " " * 9)
    r = stop(project, True)                                 # 第 6 次:放行
    assert not r.blocked and "已連續擋下 5 次" in r.system


def test_active_flag_from_another_hook_still_verifies(project):
    # stop_hook_active=true 也可能是別的 Stop hook(例如 plan-review)擋的;
    # 本 hook 沒擋過時不可直接放行,要照常驗證。
    write(project, "data.json", '{"ok": 1}')
    clean = stop(project, True, session="other")
    assert not clean.blocked and clean.stdout == ""
    write(project, "data.json", BROKEN)
    assert stop(project, True, session="other").blocked


# ---------------------------------------------------------------- 輸出格式

ALLOWED_KEYS = {"decision", "reason", "systemMessage"}


def test_json_output_shape_when_blocking(project):
    write(project, "data.json", BROKEN)
    r = run_hook(project)
    assert r.rc == 0
    assert set(r.json) <= ALLOWED_KEYS
    assert r.json["decision"] == "block"
    assert isinstance(r.json["reason"], str) and r.json["reason"].strip()


def test_json_output_shape_when_allowing_with_warning(project, tmp_path):
    empty = tmp_path / "bin"
    empty.mkdir()
    write(project, "x.sh", "#!/bin/sh\n")
    r = run_hook(project, env={"PATH": str(empty)})
    assert r.rc == 0 and set(r.json) == {"systemMessage"}


def test_exit2_mode_clean_project_exits_zero_silently(project):
    write(project, "data.json", '{"ok": 1}')
    configure(project, block_mode="exit2")
    r = run_hook(project)
    assert r.rc == 0 and r.stdout == "" and r.stderr == ""


# ---------------------------------------------------------------- Node(Linux 上用真的 npm)

needs_npm = pytest.mark.skipif(shutil.which("npm") is None, reason="npm 未安裝")


@needs_npm
def test_node_validator_runs_real_npm_test(project):
    write(project, "package.json", json.dumps({"name": "t", "version": "1.0.0",
                                               "scripts": {"test": "node -e \"process.exit(3)\""}}))
    r = run_hook(project)
    assert r.blocked and "[node] npm test" in r.reason and "(exit 3)" in r.reason
    write(project, "package.json", json.dumps({"name": "t", "version": "1.0.0",
                                               "scripts": {"test": "node -e \"process.exit(0)\""}}))
    assert not run_hook(project).blocked


@needs_npm
def test_node_validator_falls_back_to_build(project):
    write(project, "package.json", json.dumps({"name": "t", "version": "1.0.0",
                                               "scripts": {"build": "node -e \"process.exit(1)\""}}))
    r = run_hook(project)
    assert r.blocked and "npm run build" in r.reason


def test_windows_npx_shim_resolution_simulated(hook, monkeypatch):
    # 模擬:在 Linux 上驗證 Windows 組指令的字串,不是 Windows 實機結果。
    monkeypatch.delenv("COMSPEC", raising=False)
    spawn, why = hook.resolve_argv(["npx", "--no-install", "aglint"], is_win=True,
                                   which=lambda n: {"npx": r"C:\Program Files\nodejs\npx.cmd"}.get(n))
    assert why is None
    assert spawn == r'"cmd.exe" /d /s /c ""C:\Program Files\nodejs\npx.cmd" --no-install aglint"'


# ---------------------------------------------------------------- JSONC 名單(只收有出處的)

@pytest.mark.parametrize("rel", [".eslintrc.json", ".devcontainer/devcontainer.json",
                                 ".devcontainer.json", "api-extractor.json", "tslint.json",
                                 ".babelrc.json", "jsconfig.json", "packages/web/tsconfig.base.json"])
def test_documented_jsonc_files_accept_comments(project, rel):
    write(project, rel, '{\n  // comment\n  "a": [1, 2,],\n}\n')
    r = run_hook(project)
    assert not r.blocked, r


@pytest.mark.parametrize("rel", ["deno.json", "turbo.json", "biome.json", "package.json"])
def test_unlisted_files_with_comments_are_still_errors(project, rel):
    # deno/turbo 官方文件要求註解改用 .jsonc;biome 查不到出處 → 維持嚴格。
    write(project, rel, '{\n  // comment\n  "a": 1\n}\n')
    r = run_hook(project)
    assert r.blocked and f"{rel}:2:3" in r.reason


# ---------------------------------------------------------------- 工具缺失(override 的直譯器不在)

def test_override_interpreter_missing_warns_by_default_and_can_block(project, tmp_path):
    empty = tmp_path / "bin"
    empty.mkdir()
    write(project, ".claude/verify.sh", "exit 1\n")
    r = run_hook(project, env={"PATH": str(empty)})       # 連 sh 都找不到
    assert not r.blocked and "工具缺失" in r.system
    configure(project, validators={"override": {"on_missing": "block"}})
    r = run_hook(project, env={"PATH": str(empty)})
    assert r.blocked and "工具缺失" in r.reason
