"""Stage 2 深度驗收(2026-11-03):快取會不會在「程式或環境已經改變」時沿用舊的通過結果。

每一條都是「改了某樣東西 → 必須重新驗證(或必須擋下)」。外部工具一樣用假工具(Stage 3-1 起是
Python 實作 + 依平台產生的 sh / .cmd 啟動器),
所以這裡驗的是「hook 有沒有察覺變化並重跑」,不是那些工具本身;標「模擬」的情境見報告。
"""

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import HOOK, POSIX, configure, run_hook, venv_python, write
from test_stage2_scope_cache import idf_project, last_log, pio_project, plan_of

# 假工具(fake fixture)在 conftest,跨平台。真正依賴 POSIX 的測試逐條標明原因。
def posix_only(why):
    return pytest.mark.skipif(not POSIX, reason=f"POSIX 專屬:{why}")
BAD = '{"a": }'


def fast_pathed(project):
    return last_log(project).get("decision") == "allow-unchanged-green"


def reverified(project, vid):
    if fast_pathed(project):
        return False
    step = plan_of(project, vid)
    return bool(step) and step["action"] == "run"


def green_twice(project, env, vid=None):
    """先跑到全綠,再確認第二次真的走快速路徑(前提成立,後面的斷言才有意義)。"""
    r = run_hook(project, env=env)
    assert not r.blocked, r
    run_hook(project, env=env)
    assert fast_pathed(project), last_log(project)


def ok_tests(project):
    write(project, "tests/test_ok.py", "def test_ok():\n    assert True\n")


# ================================================================ 1. 環境改變 → 相關驗證重跑

@posix_only("用 symlink 換掉 .venv 的直譯器;Windows 建 symlink 需要開發者模式或系統管理員權限")
def test_python_interpreter_switch_reverifies(project, fake):
    others = [p for p in (shutil.which("python3.11"), shutil.which("python3.12"), shutil.which("python3"))
              if p and os.path.realpath(p) != os.path.realpath(sys.executable)]
    if not others:
        pytest.skip("找不到第二個 Python 直譯器")
    link = project / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(sys.executable)
    ok_tests(project)
    green_twice(project, fake.env)
    link.unlink()
    link.symlink_to(others[0])                 # 同一個路徑,背後換成另一個版本
    run_hook(project, env=fake.env)
    assert reverified(project, "pytest") and reverified(project, "python")


def test_global_python_package_install_reverifies(project, fake, tmp_path):
    env = dict(fake.env, PYTHONUSERBASE=str(tmp_path / "userbase"))
    ok_tests(project)
    green_twice(project, env)
    # 使用者 site 的路徑各平台不同(POSIX lib/pythonX.Y/site-packages、Windows PythonXY\\site-packages),問直譯器本身
    site_dir = Path(subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                                   env=dict(os.environ, **env), capture_output=True, text=True).stdout.strip())
    assert str(site_dir).startswith(str(tmp_path / "userbase")), site_dir
    write(site_dir, "newpkg/__init__.py", "X = 1\n")   # 等同 pip install --user newpkg
    run_hook(project, env=env)
    # 直譯器在 venv 裡(或用 -s)時根本不讀使用者 site:那裡的變化對它不可見,不重驗才是對的。
    enabled = subprocess.run([sys.executable, "-c", "import site; print(site.ENABLE_USER_SITE)"],
                             env=dict(os.environ, **env), capture_output=True, text=True).stdout.strip()
    if enabled == "True":
        assert reverified(project, "pytest")
    else:
        assert fast_pathed(project)


def _venv(project):
    # 用真的 venv(有 pyvenv.cfg):第一版測試只放了一個 symlink,Python 根本不會把它當 venv,
    # 套件目錄也就不在直譯器的搜尋路徑裡 —— 那是測試夾具的錯,不是 hook 的。
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")], check=True)
    out = subprocess.run([str(venv_python(project / ".venv")), "-c",
                          "import site; print(site.getsitepackages()[0])"],
                         check=True, capture_output=True, text=True).stdout.strip()
    sp = Path(out)
    write(sp, "dep/__init__.py", "VERSION = 1\n")
    return sp


def test_venv_package_install_reverifies(project, fake):
    sp = _venv(project)
    ok_tests(project)
    green_twice(project, fake.env)
    write(sp, "another/__init__.py", "")
    run_hook(project, env=fake.env)
    assert reverified(project, "pytest")


def test_venv_package_edited_in_place_reverifies(project, fake):
    sp = _venv(project)
    ok_tests(project)
    green_twice(project, fake.env)
    write(sp, "dep/__init__.py", "VERSION = 2\n")    # 套件內容改了,目錄結構沒變
    run_hook(project, env=fake.env)
    assert reverified(project, "pytest")


def node_project(project):
    write(project, "package.json", json.dumps({"name": "t", "version": "1.0.0",
                                               "scripts": {"test": "node -e \"process.exit(0)\""}}))
    write(project, "node_modules/.package-lock.json", '{"lockfileVersion": 3, "packages": {}}')
    write(project, "node_modules/leftpad/package.json", '{"version": "1.0.0"}')
    write(project, "node_modules/leftpad/index.js", "module.exports = 1;\n")


needs_npm = pytest.mark.skipif(shutil.which("npm") is None or shutil.which("node") is None, reason="需要 npm")


@needs_npm
def test_npm_package_update_reverifies(project, fake):
    node_project(project)
    green_twice(project, fake.env)
    write(project, "node_modules/leftpad/package.json", '{"version": "1.0.1"}')
    write(project, "node_modules/.package-lock.json", '{"lockfileVersion": 3, "packages": {"x": 1}}')
    run_hook(project, env=fake.env)
    assert reverified(project, "node")


@needs_npm
def test_node_modules_file_edited_in_place_reverifies(project, fake):
    node_project(project)
    green_twice(project, fake.env)
    write(project, "node_modules/leftpad/index.js", "module.exports = 2;\n")
    run_hook(project, env=fake.env)
    assert reverified(project, "node")


@needs_npm
def test_node_version_switch_reverifies(project, fake):
    node_project(project)
    green_twice(project, fake.env)
    real = shutil.which("node")
    fake.wrap("node", real)                    # 模擬 nvm 切換:PATH 上的 node 換成另一個
    run_hook(project, env=fake.env)
    assert reverified(project, "node")


def test_platformio_toolchain_update_rebuilds(project, fake, tmp_path):
    core = tmp_path / "piocore"
    write(core, "packages/toolchain-xtensa-esp32/package.json", '{"version": "8.4.0"}')
    write(core, "platforms/espressif32/platform.json", '{"version": "6.5.0"}')
    env = dict(fake.env, PLATFORMIO_CORE_DIR=str(core))
    fake.make("pio")
    pio_project(project)
    green_twice(project, env)
    write(core, "packages/toolchain-xtensa-esp32/package.json", '{"version": "12.2.0"}')
    run_hook(project, env=env)
    assert reverified(project, "firmware") and fake.runs("pio") == 2


def test_platformio_platform_update_rebuilds(project, fake, tmp_path):
    core = tmp_path / "piocore"
    write(core, "platforms/espressif32/platform.json", '{"version": "6.5.0"}')
    env = dict(fake.env, PLATFORMIO_CORE_DIR=str(core))
    fake.make("pio")
    pio_project(project)
    green_twice(project, env)
    write(core, "platforms/espressif32/platform.json", '{"version": "6.9.0"}')
    run_hook(project, env=env)
    assert fake.runs("pio") == 2


def test_esp_idf_compiler_update_rebuilds(project, fake):
    fake.make("idf.py")
    cc = fake.make("xtensa-esp32-elf-gcc")
    idf_project(project)
    green_twice(project, fake.env)
    fake.bump(cc)
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 2


def test_esp_idf_target_change_rebuilds(project, fake):
    fake.make("idf.py")
    idf_project(project)
    green_twice(project, dict(fake.env, IDF_TARGET="esp32"))
    run_hook(project, env=dict(fake.env, IDF_TARGET="esp32s3"))
    assert fake.runs("idf.py") == 2


def test_rustup_toolchain_switch_reverifies(project, fake):
    fake.make("cargo")
    write(project, "Cargo.toml", "[package]\nname = 'x'\n")
    green_twice(project, dict(fake.env, RUSTUP_TOOLCHAIN="stable"))
    run_hook(project, env=dict(fake.env, RUSTUP_TOOLCHAIN="nightly"))
    assert fake.runs("cargo") == 2


def test_linter_upgrade_without_file_changes_reverifies(project, fake):
    sc = fake.make("shellcheck")
    write(project, "a.sh", "#!/bin/sh\necho ok\n")
    green_twice(project, fake.env)
    fake.bump(sc)
    run_hook(project, env=fake.env)
    assert reverified(project, "shell") and fake.runs("shellcheck") == 2


# ================================================================ 2. 被忽略目錄裡的相依檔

def test_managed_component_edit_rebuilds(project, fake):
    fake.make("idf.py")
    idf_project(project)
    write(project, "managed_components/espressif__led/src/led.c", "int led;\n")
    green_twice(project, fake.env)
    write(project, "managed_components/espressif__led/src/led.c", "int led = 1;\n")
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 2


def test_platformio_libdeps_edit_rebuilds(project, fake):
    fake.make("pio")
    pio_project(project)
    write(project, ".pio/libdeps/esp32/ArduinoJson/src/ArduinoJson.h", "#pragma once\n")
    green_twice(project, fake.env)
    write(project, ".pio/libdeps/esp32/ArduinoJson/src/ArduinoJson.h", "#pragma once\n// patched\n")
    run_hook(project, env=fake.env)
    assert fake.runs("pio") == 2


def test_gitignored_header_change_rebuilds(project, fake):
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    write(project, ".gitignore", "include/generated.h\n")
    fake.make("pio")
    pio_project(project)
    write(project, "include/generated.h", "#define V 1\n")
    green_twice(project, fake.env)
    write(project, "include/generated.h", "#define V 2\n")
    run_hook(project, env=fake.env)
    assert fake.runs("pio") == 2


# ================================================================ 3. 快取損壞 / 竄改 → 完整驗證,不得放行

CACHE = ".claude/.verify_cache.json"


def _green_json(project, env):
    write(project, "a.json", "{}")
    write(project, "b.json", "{}")
    green_twice(project, env)


def _full_check_happened(project):
    res = next(r for r in last_log(project)["results"] if r["id"] == "json")
    # Final 起 .claude/*.json 也檢查:有 verify.json 時要多算一個
    expected = 2 + int((project / ".claude" / "verify.json").exists())
    return not fast_pathed(project) and res.get("checked") == expected and res.get("cached") == 0


@pytest.mark.parametrize("damage", ["delete", "empty", "truncate", "not-json", "missing-fields",
                                    "unknown-format", "wrong-top-type"])
def test_damaged_cache_forces_full_verification(project, fake, damage):
    _green_json(project, fake.env)
    p = project / CACHE
    raw = p.read_text(encoding="utf-8")
    if damage == "delete":
        p.unlink()
    elif damage == "empty":
        p.write_text("", encoding="utf-8")
    elif damage == "truncate":
        p.write_text(raw[: len(raw) // 2], encoding="utf-8")
    elif damage == "not-json":
        p.write_text("{oops", encoding="utf-8")
    elif damage == "missing-fields":
        p.write_text(json.dumps({"format": json.loads(raw)["format"]}), encoding="utf-8")
    elif damage == "unknown-format":
        c = json.loads(raw)
        c["format"] = 999
        p.write_text(json.dumps(c), encoding="utf-8")
    elif damage == "wrong-top-type":
        p.write_text("[1, 2, 3]", encoding="utf-8")
    run_hook(project, env=fake.env)
    assert _full_check_happened(project), last_log(project)


def _resign(hook, c):
    """重算校驗碼:模擬「校驗碼正確、但內容結構有誤」的快取(例如舊版或有 bug 的寫入者)。"""
    c["checksum"] = hook._checksum({k: c.get(k) for k in hook.CACHE_FIELDS})


def _blocked_then_damage(project, env, mutate, hook=None):
    write(project, "bad.json", BAD)
    write(project, "ok.json", "{}")
    assert run_hook(project, env=env).blocked
    p = project / CACHE
    c = json.loads(p.read_text(encoding="utf-8"))
    mutate(c)
    if hook is not None:
        _resign(hook, c)
    p.write_text(json.dumps(c), encoding="utf-8")
    return run_hook(project, env=env)


def test_wrong_types_in_manifest_do_not_cause_a_pass(hook, project, fake):
    def mutate(c):
        for ent in c["manifest"].values():
            ent[2] = "not-a-number"            # 欄位型別錯誤
    r = _blocked_then_damage(project, fake.env, mutate, hook)
    assert r.blocked and "bad.json" in r.reason, r
    assert last_log(project).get("cache") == "schema"   # 是結構檢查擋下,不是崩潰後重試


def test_wrong_types_in_results_do_not_cause_a_pass(hook, project, fake):
    def mutate(c):
        c["results"]["json"]["files"]["bad.json"][2] = 123
    r = _blocked_then_damage(project, fake.env, mutate, hook)
    assert r.blocked and "bad.json" in r.reason, r
    assert last_log(project).get("cache") == "schema"


def test_hand_edited_cache_cannot_turn_a_failure_into_a_pass(project, fake):
    def mutate(c):
        c["results"]["json"]["files"]["bad.json"][1] = "pass"
        c["results"]["json"]["files"]["bad.json"][2] = []
    r = _blocked_then_damage(project, fake.env, mutate)     # 沒有重算校驗碼
    assert r.blocked and "bad.json" in r.reason, r
    assert last_log(project).get("cache") == "checksum"


def test_wrong_types_in_state_do_not_cause_a_pass(project, fake):
    write(project, "bad.json", BAD)
    write(project, ".claude/.verify_state.json", json.dumps({"session": "s1", "blocks": "x", "block_fp": 5}))
    r = run_hook(project, {"session_id": "s1", "stop_hook_active": True}, env=fake.env)
    assert r.blocked and "bad.json" in r.reason, r
    assert "內部出錯" not in r.system          # 是型別檢查處理掉的,不是崩潰後重試


def test_leftover_temp_file_is_ignored(project, fake):
    write(project, ".claude/.verify_cache.json.4242.tmp", "{garbage")
    write(project, "bad.json", BAD)
    assert run_hook(project, env=fake.env).blocked


def test_cache_copied_from_another_directory_is_not_trusted(project, fake, tmp_path):
    _green_json(project, fake.env)
    other = tmp_path / "copy-of-proj"
    shutil.copytree(project, other)            # 連 .claude/ 的快取與狀態一起複製
    run_hook(other, env=fake.env)
    assert _full_check_happened(other), last_log(other)


def test_config_change_invalidates_cached_results(project, fake):
    _green_json(project, fake.env)
    configure(project, max_lines=9)
    run_hook(project, env=fake.env)
    assert _full_check_happened(project), last_log(project)


# ================================================================ 4. 分支切換

def test_git_branch_switch_never_reuses_the_other_branch_result(project, fake):
    def git(*a):
        subprocess.run(["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@t"] + list(a),
                       check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    write(project, "a.json", '{"ok": 1}')
    git("add", "-A")
    git("commit", "-qm", "good")
    git("checkout", "-qb", "broken")
    write(project, "a.json", BAD)
    git("commit", "-qam", "bad")
    verdicts = []
    for branch in ("main", "broken", "main", "broken", "main"):
        git("checkout", "-q", branch)
        verdicts.append(run_hook(project, env=fake.env).blocked)
    assert verdicts == [False, True, False, True, False]


# ================================================================ 5. 並行與強制終止

def _spawn(project, env, payload=b"{}"):
    e = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), **env)
    return subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=e, cwd=str(project))


def _verdict(proc):
    out, _ = proc.communicate(b"{}", timeout=120)
    return proc.returncode == 2 or b'"decision": "block"' in out


def test_concurrent_runs_agree_and_leave_a_valid_cache(project, fake):
    for i in range(30):
        write(project, f"d/{i}.json", "{}")
    write(project, "d/bad.json", BAD)
    procs = [_spawn(project, fake.env) for _ in range(4)]
    assert [_verdict(p) for p in procs] == [True] * 4
    json.loads((project / CACHE).read_text(encoding="utf-8"))   # 沒有被寫壞


def test_concurrent_stress_never_yields_a_wrong_verdict(project, fake):
    rng = random.Random(20261103)
    for i in range(10):
        write(project, f"d/{i}.json", "{}")
    for step in range(12):
        bad = rng.random() < 0.5
        write(project, f"d/{rng.randrange(10)}.json", BAD if bad else '{"s": %d}' % step)
        procs = [_spawn(project, fake.env) for _ in range(3)]
        got = [_verdict(p) for p in procs]
        configure(project, cache=False)
        truth = run_hook(project, env=fake.env).blocked       # 不用快取的真值
        (project / ".claude/verify.json").unlink()
        assert got == [truth] * 3, (step, got, truth)
        assert run_hook(project, env=fake.env).blocked == truth  # 並行寫過的快取也不能說謊


def _slow_pio(fake, pidfile):
    script = fake.bindir / "pio"
    script.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\nsleep "${{SLOW:-0}}"\nexit 0\n', encoding="utf-8")
    script.chmod(0o755)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = f"/proc/{pid}/status"
    if os.path.exists(st):
        with open(st, encoding="utf-8") as fh:
            return not any(ln.startswith("State:") and "Z" in ln.split()[1] for ln in fh)
    return True


@posix_only("SIGKILL hook 本身 + /proc 檢查殭屍;Windows 的強制終止見 test_crossplatform")
def test_killed_mid_run_leaves_no_cache_that_passes_a_broken_file(project, fake, tmp_path):
    _slow_pio(fake, tmp_path / "pio.pid")
    pio_project(project)
    write(project, "a.json", "{}")
    assert not run_hook(project, env=fake.env).blocked
    write(project, "a.json", BAD)
    p = _spawn(project, dict(fake.env, SLOW="20"))
    p.stdin.write(b"{}")
    p.stdin.close()
    time.sleep(1.5)
    p.kill()                                    # SIGKILL:hook 來不及做任何事
    p.wait(timeout=10)
    json.loads((project / CACHE).read_text(encoding="utf-8"))
    r = run_hook(project, env=fake.env)
    assert r.blocked and "a.json" in r.reason


@posix_only("SIGTERM 是 POSIX 訊號;Windows 對應的是 CTRL_BREAK / TerminateProcess,見 test_crossplatform")
def test_sigterm_stops_child_processes(project, fake, tmp_path):
    pidfile = tmp_path / "pio.pid"
    _slow_pio(fake, pidfile)
    pio_project(project)
    p = _spawn(project, dict(fake.env, SLOW="60"))
    p.stdin.write(b"{}")
    p.stdin.close()
    for _ in range(50):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.1)
    child = int(pidfile.read_text())
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=20)
    time.sleep(0.5)
    alive = _alive(child)
    if alive:
        os.kill(child, signal.SIGKILL)
    assert not alive, "hook 被終止後,建置子程序還在跑"


# ================================================================ 6. 韌體相依:看不出範圍就完整重建

def _outside(tmp_path, rel, text):
    return write(tmp_path / "outside", rel, text)


@pytest.mark.parametrize("kind", ["cmake-include", "extra-component-dirs", "add-subdirectory", "embed-abs",
                                  "pio-lib-extra-dirs", "pio-build-flags-include", "pio-lib-deps-file"])
def test_dependency_outside_the_project_is_never_cached(project, fake, tmp_path, kind):
    out = tmp_path / "outside"
    _outside(tmp_path, "extra.cmake", "set(X 1)\n")
    _outside(tmp_path, "comps/c1/CMakeLists.txt", "idf_component_register(SRCS c1.c)\n")
    _outside(tmp_path, "comps/c1/c1.c", "int c1;\n")
    _outside(tmp_path, "lib/L/L.h", "#pragma once\n")
    _outside(tmp_path, "blob.bin", "x")
    fake.make("pio")
    fake.make("idf.py")
    if kind.startswith("pio"):
        extra = {"pio-lib-extra-dirs": f"lib_extra_dirs = {out / 'lib'}\n",
                 "pio-build-flags-include": f"build_flags = -I{out / 'lib' / 'L'} -DX=1\n",
                 "pio-lib-deps-file": f"lib_deps = file://{out / 'lib' / 'L'}\n"}[kind]
        write(project, "platformio.ini", "[env:esp32]\nplatform = espressif32\n" + extra)
        write(project, "src/main.cpp", "int main(){}\n")
        tool = "pio"
    else:
        idf_project(project)
        top = "cmake_minimum_required(VERSION 3.16)\n"
        if kind == "cmake-include":
            top += f"include({out / 'extra.cmake'})\n"
        if kind == "extra-component-dirs":
            top += f"set(EXTRA_COMPONENT_DIRS {out / 'comps'})\n"
        if kind == "add-subdirectory":
            top += f"add_subdirectory({out / 'comps' / 'c1'} c1)\n"
        top += "include($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(app)\n"
        write(project, "CMakeLists.txt", top)
        if kind == "embed-abs":
            write(project, "main/CMakeLists.txt",
                  f'idf_component_register(SRCS "main.c" EMBED_FILES "{out / "blob.bin"}")\n')
        tool = "idf.py"
    run_hook(project, env=fake.env)
    _outside(tmp_path, "extra.cmake", "set(X 2)\n")          # 專案外的東西改了,專案內一個字沒動
    _outside(tmp_path, "comps/c1/c1.c", "int c1 = 2;\n")
    _outside(tmp_path, "lib/L/L.h", "#pragma once\n#define V 2\n")
    _outside(tmp_path, "blob.bin", "y")
    run_hook(project, env=fake.env)
    assert fake.runs(tool) == 2, kind


@pytest.mark.parametrize("cmake", ["execute_process(COMMAND python gen.py)\n",
                                   "add_custom_command(OUTPUT x.h COMMAND gen)\n"])
def test_arbitrary_build_commands_are_never_cached(project, fake, cmake):
    fake.make("idf.py")
    idf_project(project)
    write(project, "main/CMakeLists.txt",
          'idf_component_register(SRCS "main.c")\n' + cmake)
    run_hook(project, env=fake.env)
    run_hook(project, env=fake.env)                            # 什麼都沒改
    assert fake.runs("idf.py") == 2


def test_platformio_extra_scripts_are_never_cached(project, fake):
    fake.make("pio")
    pio_project(project)
    write(project, "platformio.ini", "[env:esp32]\nplatform = espressif32\nextra_scripts = pre:gen.py\n")
    write(project, "gen.py", "print('gen')\n")
    run_hook(project, env=fake.env)
    run_hook(project, env=fake.env)
    assert fake.runs("pio") == 2


def test_resolvable_cmake_variables_keep_precise_tracking(project, fake):
    # ${CMAKE_CURRENT_SOURCE_DIR} 之類能解析的變數,不該讓整個專案退化成每次都重建。
    fake.make("idf.py")
    idf_project(project)
    write(project, "main/CMakeLists.txt",
          'idf_component_register(SRCS "main.c" EMBED_TXTFILES "${CMAKE_CURRENT_SOURCE_DIR}/../web/index.html")\n')
    write(project, "data/a.json", "{}")
    run_hook(project, env=fake.env)
    write(project, "data/a.json", '{"v": 2}')
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 1
    write(project, "web/index.html", "<html>v2</html>\n")
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 2


# ================================================================ 7. 補充:修復時新增的保護

@needs_npm
def test_dependency_dir_too_large_to_track_runs_every_time(project, fake):
    node_project(project)
    for i in range(10):
        write(project, f"node_modules/pkg{i}/index.js", "x")
    configure(project, dep_scan_cap=3)          # 上限 3 個檔案 → node_modules 追不到
    run_hook(project, env=fake.env)
    run_hook(project, env=fake.env)
    assert reverified(project, "node")
    assert "always" in plan_of(project, "node")["why"]


def test_crash_on_first_attempt_retries_without_cache(hook, project, monkeypatch, capsys):
    import io
    write(project, "bad.json", BAD)

    def broken_cache(*_a, **_k):
        raise RuntimeError("cache reader exploded")
    monkeypatch.setattr(hook, "load_cache", broken_cache)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    with pytest.raises(SystemExit):
        hook.entry()
    out = json.loads(capsys.readouterr().out)
    assert out.get("decision") == "block" and "bad.json" in out["reason"]   # 沒有因為崩潰而放行
    assert "完整驗證" in out.get("systemMessage", "")


def _path_without(tool, dirs, scratch):
    """PATH 目錄清單,但找不到 tool:含有 tool 的目錄換成「其他檔案都連回原處」的鏡像目錄。
    CI 的 ubuntu runner 把 shellcheck 和 sh、git 一起放在 /usr/bin(第一次 CI 實測),
    整個拿掉 /usr/bin 會連其他工具一起拿掉。"""
    out = []
    for i, d in enumerate(dirs):
        if shutil.which(tool, path=d) is None:
            out.append(d)
            continue
        mirror = scratch / f"path-without-{tool}-{i}"
        mirror.mkdir()
        for entry in os.scandir(d):
            if os.path.splitext(entry.name)[0].lower() != tool:
                (mirror / entry.name).symlink_to(entry.path)
        out.append(str(mirror))
    return out


def test_installing_a_missing_tool_counts_as_a_change_for_the_loop_guard(project, fake, tmp_path):
    # Stage 1 遺留疑點:Claude 只是補裝工具、沒改專案檔 → 舊版防迴圈當成「沒變」直接放行。
    write(project, "a.sh", "#!/bin/sh\necho ok\n")
    configure(project, on_missing="block")
    dirs = [str(fake.bindir), os.path.dirname(sys.executable)]
    if not POSIX:
        dirs.append(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"))
    else:
        dirs += ["/usr/bin", "/bin"]
    dirs = _path_without("shellcheck", dirs, tmp_path)
    env = dict(fake.env, PATH=os.pathsep.join(dirs))  # 確定沒有真的 shellcheck
    assert shutil.which("shellcheck", path=env["PATH"]) is None
    first = run_hook(project, {"session_id": "s1", "stop_hook_active": False}, env=env)
    assert first.blocked and "工具缺失" in first.reason
    fake.make("shellcheck")                    # 補裝了 shellcheck
    second = run_hook(project, {"session_id": "s1", "stop_hook_active": True}, env=env)
    assert "沒有變更" not in second.system
    assert reverified(project, "shell") and not second.blocked


def test_included_project_cmake_file_is_tracked_precisely(project, fake):
    fake.make("idf.py")
    idf_project(project)
    write(project, "cmake/opts.cmake", "set(OPT 1)\n")
    top = (project / "CMakeLists.txt").read_text(encoding="utf-8")
    write(project, "CMakeLists.txt", top.replace("project(app)", "include(cmake/opts.cmake)\nproject(app)"))
    write(project, "data/a.json", "{}")
    run_hook(project, env=fake.env)
    write(project, "data/a.json", '{"v": 2}')
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 1             # 無關的 JSON:不重建
    write(project, "cmake/opts.cmake", "set(OPT 2)\n")
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 2             # 被 include 的 .cmake 改了:重建


def test_host_compiler_upgrade_rebuilds(project, fake):
    # PlatformIO native 平台、部分 Arduino 核心用的是系統編譯器:它換了也要重建。
    fake.make("pio")
    gcc = fake.make("gcc")
    pio_project(project)
    green_twice(project, fake.env)
    fake.bump(gcc)
    run_hook(project, env=fake.env)
    assert fake.runs("pio") == 2
