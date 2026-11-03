"""Stage 3-2 真實工具鏈驗收抓到的缺陷:override_cache=true 時,自訂驗證的快取沒有追蹤環境變數。

真實重現(bench/real_toolchains.py 的 cmake 套組):同一份 CMake 專案,gcc 編得過、clang 編不過
(GNU 巢狀函式)。只改 CC=clang、檔案不動 → 舊版走全綠快速路徑放行。
這裡用 Python 自訂驗證重現同一件事,所以各平台都能跑。
"""

import json

from conftest import configure, run_hook, write

SCRIPT = ("import os, sys\n"
          "mode = os.environ.get('BUILD_MODE', 'ok')\n"
          "print('src/main.c:3:1: error: only builds with BUILD_MODE=ok' if mode != 'ok' else 'ok')\n"
          "sys.exit(0 if mode == 'ok' else 1)\n")


def test_env_var_change_reverifies_cached_override(project):
    write(project, ".claude/verify.py", SCRIPT)
    configure(project, override_cache=True)
    assert not run_hook(project).blocked
    assert not run_hook(project).blocked                       # 無變更:可以沿用
    r = run_hook(project, env={"BUILD_MODE": "strict"})        # 只改環境變數
    assert r.blocked and "src/main.c:3:1" in r.reason


def test_compiler_env_vars_are_tracked(project):
    write(project, ".claude/verify.py",
          "import os, sys\nsys.exit(0 if os.environ.get('CC', 'gcc') == 'gcc' else 1)\n")
    configure(project, override_cache=True)
    assert not run_hook(project).blocked
    for var, val in (("CC", "clang"),):
        assert run_hook(project, env={var: val}).blocked


def test_volatile_shell_vars_do_not_invalidate(project):
    write(project, ".claude/verify.py", "print('ok')\n")
    configure(project, override_cache=True)
    assert not run_hook(project, env={"SHLVL": "1", "OLDPWD": "/a"}).blocked
    run_hook(project, env={"SHLVL": "7", "OLDPWD": "/b", "CLAUDE_VERIFY_DEBUG": "1"})
    rec = json.loads((project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["decision"] == "allow-unchanged-green"          # 殼層層數、上一個目錄不影響建置


def test_environment_values_never_reach_disk(project):
    # 整份環境都進簽章,但只能以雜湊的形式:快取檔與紀錄檔不可以出現環境變數的值(可能是金鑰)
    secret = "sk-test-DO-NOT-LEAK-4f9c2e"
    write(project, ".claude/verify.py", "print('ok')\n")
    configure(project, override_cache=True)
    run_hook(project, env={"MY_API_TOKEN": secret, "CLAUDE_VERIFY_DEBUG": "1"})
    for name in (".verify_cache.json", ".verify.log", ".verify_state.json"):
        p = project / ".claude" / name
        if p.exists():
            assert secret not in p.read_text(encoding="utf-8"), name


# ---------------------------------------------------------------- 專案外、但屬於建置輸入的框架原始碼
#
# 真實重現(bench/real_toolchains.py 的 esp_idf 套組):在 ESP-IDF 本身的 components/log/include/esp_log.h
# 塞一行 #error,專案檔一個都沒動 → 舊版走全綠快速路徑放行,實際上已經編不過。
# 舊版的 idf_sig 只看 version.txt / version.cmake / 編譯器;PlatformIO 只看各套件的 package.json /
# platform.json;Arduino 只列目錄到第 4 層 —— 就地修改框架原始碼(本機修補 IDF、改平台建置腳本)都看不到。

from test_stage2_scope_cache import idf_project, pio_project  # noqa: E402


def _green_twice(project, env, fake, tool):
    assert not run_hook(project, env=env).blocked
    assert not run_hook(project, env=env).blocked
    assert fake.runs(tool) == 1


def test_editing_esp_idf_component_source_rebuilds(project, fake, tmp_path):
    idf = tmp_path / "esp-idf"
    hdr = write(idf, "components/log/include/esp_log.h", "#pragma once\n")
    write(idf, "tools/cmake/version.cmake", "set(IDF_VERSION_MAJOR 5)\n")
    env = dict(fake.env, IDF_PATH=str(idf))
    fake.make("idf.py")
    idf_project(project)
    _green_twice(project, env, fake, "idf.py")
    hdr.write_text("#pragma once\n#error patched\n", encoding="utf-8")
    run_hook(project, env=env)
    assert fake.runs("idf.py") == 2


def test_editing_esp_idf_build_scripts_rebuilds(project, fake, tmp_path):
    idf = tmp_path / "esp-idf"
    ld = write(idf, "tools/ldgen/ldgen.py", "x = 1\n")
    write(idf, "components/log/include/esp_log.h", "#pragma once\n")
    env = dict(fake.env, IDF_PATH=str(idf))
    fake.make("idf.py")
    idf_project(project)
    _green_twice(project, env, fake, "idf.py")
    ld.write_text("x = 2  # patched\n", encoding="utf-8")
    run_hook(project, env=env)
    assert fake.runs("idf.py") == 2


def test_editing_platformio_platform_builder_rebuilds(project, fake, tmp_path):
    core = tmp_path / "piocore"
    write(core, "platforms/espressif32/platform.json", '{"version": "6.5.0"}')
    builder = write(core, "platforms/espressif32/builder/main.py", "env = 1\n")
    env = dict(fake.env, PLATFORMIO_CORE_DIR=str(core))
    fake.make("pio")
    pio_project(project)
    _green_twice(project, env, fake, "pio")
    builder.write_text("env = 2  # patched\n", encoding="utf-8")       # platform.json 沒變
    run_hook(project, env=env)
    assert fake.runs("pio") == 2


def test_editing_platformio_framework_source_rebuilds(project, fake, tmp_path):
    core = tmp_path / "piocore"
    write(core, "packages/framework-arduinoespressif32/package.json", '{"version": "3.0.7"}')
    src = write(core, "packages/framework-arduinoespressif32/cores/esp32/main.cpp", "int x;\n")
    env = dict(fake.env, PLATFORMIO_CORE_DIR=str(core))
    fake.make("pio")
    pio_project(project)
    _green_twice(project, env, fake, "pio")
    src.write_text("int x = ;\n", encoding="utf-8")                    # package.json 沒變
    run_hook(project, env=env)
    assert fake.runs("pio") == 2


def test_editing_arduino_core_source_rebuilds(project, fake, tmp_path):
    data = tmp_path / "arduino15"
    core = write(data, "packages/esp32/hardware/esp32/3.0.7/cores/esp32/Arduino.h", "#pragma once\n")
    env = dict(fake.env, ARDUINO_DIRECTORIES_DATA=str(data), ARDUINO_DIRECTORIES_USER=str(tmp_path / "sketchbook"))
    fake.make("arduino-cli")
    write(project, "blink/blink.ino", "void setup(){}\nvoid loop(){}\n")
    write(project, ".claude/fqbn", "esp32:esp32:esp32\n")
    _green_twice(project, env, fake, "arduino-cli")
    core.write_text("#pragma once\n#error patched\n", encoding="utf-8")
    run_hook(project, env=env)
    assert fake.runs("arduino-cli") == 2


# ---------------------------------------------------------------- 原因要指到真正的錯誤行
#
# 真實重現(ESP-IDF v5.4.4,注入 IDF 本身的 #error):原因列了 8 行 ninja 的「FAILED: xxx.c.obj」,
# 真正的錯誤行被擠進「…另 30 行省略」。模型知道失敗了,卻看不到失敗在哪。

NINJA_OUTPUT = "\n".join(
    [f"[{i}/1012] Building C object esp-idf/log/CMakeFiles/__idf_log.dir/src/x{i}.c.obj" for i in range(40)]
    + [f"FAILED: esp-idf/mbedtls/CMakeFiles/mbedcrypto.dir/opt/esp-idf/components/mbedtls/port/f{i}.c.obj"
       for i in range(10)]
    + ["/opt/esp-idf/components/log/include/esp_log.h:58:2: error: #error injected",
       "   58 | #error injected", "      |  ^~~~~",
       "In file included from /opt/esp-idf/components/mbedtls/port/sha/esp_sha.c:12:",
       "ninja: build stopped: subcommand failed.", "ninja failed with exit code 1"])


def test_compiler_error_line_outranks_ninja_failed_lines(hook, tmp_path):
    out = hook.summarize(NINJA_OUTPUT, 8, tmp_path)
    assert any("esp_log.h:58:2: error: #error injected" in ln for ln in out), out


def test_project_compile_error_still_first(hook, tmp_path):
    text = f"FAILED: build/main.c.obj\n{tmp_path}/main/app.c:3:9: error: expected ';' before '}}' token\n"
    out = hook.summarize(text, 8, tmp_path)
    assert out[0].startswith("FAILED") or "main/app.c:3:9: error" in out[0]
    assert any(ln.startswith("main/app.c:3:9: error") for ln in out), out


def test_pytest_failed_lines_keep_top_rank(hook, tmp_path):
    text = "\n".join(["noise"] * 3 + [f"FAILED tests/test_a.py::test_{i} - AssertionError" for i in range(3)])
    out = hook.summarize(text, 8, tmp_path)
    assert out == [f"FAILED tests/test_a.py::test_{i} - AssertionError" for i in range(3)]


def test_esp_idf_python_env_package_change_rebuilds(project, fake, tmp_path):
    # esp-idf-kconfig、esptool 都裝在 IDF 自己的 venv(IDF_PYTHON_ENV_PATH):升級它們會改變建置結果
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415
    from conftest import venv_python  # noqa: PLC0415
    venv = tmp_path / "idf-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    site = subprocess.run([str(venv_python(venv)), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, check=True).stdout.strip()
    env = dict(fake.env, IDF_PYTHON_ENV_PATH=str(venv))
    fake.make("idf.py")
    idf_project(project)
    _green_twice(project, env, fake, "idf.py")
    write(site, "esp_idf_kconfig/__init__.py", "__version__ = '2.6.0'\n")    # 等同 pip install -U
    run_hook(project, env=env)
    assert fake.runs("idf.py") == 2


def test_env_signature_itself_contains_no_values(hook, monkeypatch):
    # 快取鍵會再雜湊一次,所以整合測試看不出簽章裡有沒有原文;這裡直接釘住函式的契約:
    # 以後有人把簽章寫進 debug 紀錄,也不會把金鑰帶出去。
    secret = "sk-test-DO-NOT-LEAK-91ab"
    monkeypatch.setenv("MY_API_TOKEN", secret)
    sig = hook.env_all_sig()
    assert secret not in sig and "MY_API_TOKEN" not in sig
    assert len(sig) < 100
