"""Stage 2:只驗變更的部分、內容雜湊快取、平行執行。

外部工具(pio、idf.py、cargo、shellcheck、plutil)在這裡用「假工具」代替:
它們把每次被呼叫的起訖時間寫進專案外的紀錄檔,測試靠那份紀錄判斷「有沒有跑、跑了幾次、
有沒有同時跑」。這驗的是排程邏輯,不是那些工具本身。
"""

import json
import os
import subprocess
import time

import pytest

from conftest import configure, run_hook, write

POSIX = os.name != "nt"
# 假工具(fake fixture)在 conftest:Python 實作、依平台產生 sh / .cmd 啟動器,所以這些測試在 Windows 也會跑。


def last_log(project):
    return json.loads((project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()[-1])


def plan_of(project, vid):
    rec = last_log(project)
    return next((p for p in rec.get("plan", []) if p["id"] == vid), None)


def touch_later(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))


# ---------------------------------------------------------------- 逐檔驗證器:只驗變更的檔案

def test_only_changed_json_files_are_rechecked(project, fake):
    for i in range(3):
        write(project, f"d/{i}.json", '{"i": %d}' % i)
    assert not run_hook(project, env=fake.env).blocked
    assert plan_of(project, "json")["action"] == "run"
    write(project, "d/1.json", '{"i": 100}')
    assert not run_hook(project, env=fake.env).blocked
    step = plan_of(project, "json")
    assert step["action"] == "run" and step["files"] == ["d/1.json"]
    res = next(r for r in last_log(project)["results"] if r["id"] == "json")
    assert res["checked"] == 1 and res["cached"] == 2


def test_cached_failure_is_still_reported_without_rechecking(project, fake):
    write(project, "bad.json", '{"a": }')
    write(project, "ok.json", "{}")
    assert run_hook(project, env=fake.env).blocked
    write(project, "ok.json", '{"b": 1}')                 # 只改了好的那個
    r = run_hook(project, env=fake.env)
    assert r.blocked and "bad.json:1:7" in r.reason        # 壞的那個沿用上次結果,仍然擋
    res = next(x for x in last_log(project)["results"] if x["id"] == "json")
    assert res["checked"] == 1 and res["cached"] == 1


def test_fixed_file_is_rechecked_and_passes(project, fake):
    write(project, "bad.json", '{"a": }')
    assert run_hook(project, env=fake.env).blocked
    write(project, "bad.json", '{"a": 1}')
    assert not run_hook(project, env=fake.env).blocked


def test_deleted_failing_file_is_no_longer_reported(project, fake):
    write(project, "bad.json", '{"a": }')
    write(project, "ok.json", "{}")
    assert run_hook(project, env=fake.env).blocked
    (project / "bad.json").unlink()
    assert not run_hook(project, env=fake.env).blocked


def test_touch_without_content_change_is_not_a_change(project, fake):
    write(project, "a.json", "{}")
    assert not run_hook(project, env=fake.env).blocked
    touch_later(project / "a.json")
    run_hook(project, env=fake.env)
    assert last_log(project)["decision"] == "allow-unchanged-green"


def test_racy_edit_with_same_size_and_mtime_is_detected(project, fake):
    # 剛改完就被掃描(2 秒內)的檔案,下次一律重讀內容;同大小、同 mtime 的第二次修改也看得到。
    f = write(project, "a.json", '{"a": 1}')
    mt = f.stat().st_mtime_ns
    assert not run_hook(project, env=fake.env).blocked
    f.write_text('{"a": }}', encoding="utf-8")             # 同長度、內容壞掉
    os.utime(f, ns=(mt, mt))                               # mtime 改回去
    r = run_hook(project, env=fake.env)
    assert r.blocked and "a.json" in r.reason


OLD = 1_000_000_000 * 10 ** 9  # 2001 年:不在 racy 範圍內


def _same_size_same_mtime_edit(project, fake, **cfg):
    if cfg:
        configure(project, **cfg)
    f = write(project, "a.json", '{"a": 1}')
    os.utime(f, ns=(OLD, OLD))
    assert not run_hook(project, env=fake.env).blocked
    f.write_text('{"a": }}', encoding="utf-8")
    os.utime(f, ns=(OLD, OLD))
    return run_hook(project, env=fake.env)


def test_trust_mtime_false_catches_same_size_same_mtime_edit(project, fake):
    r = _same_size_same_mtime_edit(project, fake, trust_mtime=False)
    assert r.blocked and "a.json" in r.reason


def test_known_limitation_trust_mtime_misses_old_same_size_same_mtime_edit(project, fake):
    # Stage 2 時的已知限制(與 git index 相同):內容改了但大小與 mtime 都被刻意還原成「很久以前」,
    # 預設模式看不到。Stage 3-4 起 manifest 多比 ctime 與 inode:POSIX 的 ctime 在寫入時一定會更新、
    # 使用者改不回去,所以 POSIX 上這個限制已經消除,這裡改成斷言「擋得下」。
    # Windows 的 st_ctime 是建立時間,限制仍在 —— 照舊把它寫成文件(要嚴格請設 trust_mtime=false)。
    r = _same_size_same_mtime_edit(project, fake)
    if os.name == "nt":
        assert not r.blocked
    else:
        assert r.blocked and "a.json" in r.reason


def test_tool_upgrade_invalidates_per_file_cache(project, fake):
    fake.make("shellcheck")
    write(project, "a.sh", "#!/bin/sh\necho ok\n")
    assert not run_hook(project, env=fake.env).blocked
    assert fake.runs("shellcheck") == 1
    write(project, "b.json", "{}")                         # 只改 JSON:shell 不該重跑
    run_hook(project, env=fake.env)
    assert fake.runs("shellcheck") == 1
    fake.bump(fake.launcher("shellcheck"))                   # 「升級」:同名但內容不同
    write(project, "b.json", '{"v": 2}')
    run_hook(project, env=fake.env)
    assert fake.runs("shellcheck") == 2


def test_shellcheckrc_change_invalidates_shell_cache(project, fake):
    fake.make("shellcheck")
    write(project, "a.sh", "#!/bin/sh\necho ok\n")
    run_hook(project, env=fake.env)
    write(project, ".shellcheckrc", "disable=SC2086\n")
    run_hook(project, env=fake.env)
    assert fake.runs("shellcheck") == 2


def test_cache_false_rechecks_everything(project, fake):
    configure(project, cache=False)
    for i in range(3):
        write(project, f"{i}.json", "{}")
    run_hook(project, env=fake.env)
    write(project, "0.json", '{"x": 1}')
    run_hook(project, env=fake.env)
    res = next(r for r in last_log(project)["results"] if r["id"] == "json")
    # 3 個專案 JSON + .claude/verify.json(Final 起 .claude/*.json 也檢查)
    assert res["checked"] == 4 and res["cached"] == 0


def test_corrupt_cache_is_ignored(project, fake):
    write(project, "bad.json", '{"a": }')
    write(project, ".claude/.verify_cache.json", "{not json")
    r = run_hook(project, env=fake.env)
    assert r.blocked and "bad.json" in r.reason


def test_kit_version_change_discards_cached_results(project, fake):
    write(project, "a.json", "{}")
    run_hook(project, env=fake.env)
    cpath = project / ".claude/.verify_cache.json"
    c = json.loads(cpath.read_text(encoding="utf-8"))
    c["kit"] = "0.0.0-old"
    cpath.write_text(json.dumps(c), encoding="utf-8")
    write(project, "b.json", "{}")
    run_hook(project, env=fake.env)
    res = next(r for r in last_log(project)["results"] if r["id"] == "json")
    assert res["checked"] == 2 and res["cached"] == 0


# ---------------------------------------------------------------- 韌體:只有相關檔案變更才重建

def pio_project(project):
    write(project, "platformio.ini", "[env:esp32]\nplatform = espressif32\nboard = esp32dev\n")
    write(project, "src/main.cpp", "int main(){return 0;}\n")
    write(project, "include/app.h", "#pragma once\n")
    write(project, "data/config.json", '{"wifi": "x"}')
    write(project, "docs/readme.md", "# doc\n")


def test_firmware_not_rebuilt_for_json_or_docs_changes(project, fake):
    fake.make("pio", sleep=0.2)
    pio_project(project)
    assert not run_hook(project, env=fake.env).blocked
    assert fake.runs("pio") == 1
    write(project, "data/config.json", '{"wifi": "y"}')
    assert not run_hook(project, env=fake.env).blocked
    write(project, "docs/readme.md", "# doc v2\n")
    assert not run_hook(project, env=fake.env).blocked
    assert fake.runs("pio") == 1
    assert plan_of(project, "firmware")["action"] == "cached"


@pytest.mark.parametrize("rel", ["src/main.cpp", "include/app.h", "platformio.ini", "partitions.csv"])
def test_firmware_rebuilt_for_source_or_config_changes(project, fake, rel):
    fake.make("pio")
    pio_project(project)
    run_hook(project, env=fake.env)
    p = project / rel
    write(project, rel, (p.read_text(encoding="utf-8") if p.exists() else "") + "\n// edit\n")
    run_hook(project, env=fake.env)
    assert fake.runs("pio") == 2


def test_any_deletion_rebuilds_firmware(project, fake):
    fake.make("pio")
    pio_project(project)
    run_hook(project, env=fake.env)
    (project / "docs/readme.md").unlink()   # 不在輸入清單裡,但刪檔可能讓建置找不到東西
    run_hook(project, env=fake.env)
    assert fake.runs("pio") == 2
    assert "刪除" in plan_of(project, "firmware")["why"]


def test_firmware_failure_is_not_cached(project, fake):
    fake.make("pio", rc=1, output="src/main.cpp:1:5: error: boom")
    pio_project(project)
    assert run_hook(project, env=fake.env).blocked
    assert run_hook(project, env=fake.env).blocked          # 沒改檔也要重跑:失敗可能是環境問題
    assert fake.runs("pio") == 2


def idf_project(project):
    write(project, "CMakeLists.txt",
          "cmake_minimum_required(VERSION 3.16)\ninclude($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(app)\n")
    write(project, "sdkconfig", "CONFIG_X=y\n")
    write(project, "main/CMakeLists.txt",
          'idf_component_register(SRCS "main.c"\n    INCLUDE_DIRS "."\n'
          '    EMBED_TXTFILES "../web/index.html"\n    REQUIRES nvs_flash)\n')
    write(project, "main/main.c", "void app_main(void){}\n")
    write(project, "web/index.html", "<html></html>\n")
    write(project, "web/unused.html", "<html></html>\n")


def test_idf_embedded_file_change_rebuilds(project, fake):
    fake.make("idf.py")
    idf_project(project)
    run_hook(project, env=fake.env)
    write(project, "web/unused.html", "<html>2</html>\n")    # 沒被嵌入
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 1
    write(project, "web/index.html", "<html>2</html>\n")     # 被 EMBED_TXTFILES 嵌入
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 2


@pytest.mark.parametrize("setup", [
    ("platformio.ini", "[env:x]\nplatform = espressif32\nextra_scripts = pre:gen.py\n"),
    ("CMakeLists.txt", "project(app)\nfile(GLOB SRCS src/*.c)\n"),
    ("main/CMakeLists.txt", 'idf_component_register(SRCS "main.c" EMBED_FILES ${WEB_DIR}/a.bin)\n'),
])
def test_dynamic_build_inputs_fall_back_to_any_change(project, fake, setup):
    fake.make("pio")
    fake.make("idf.py")
    if setup[0] != "platformio.ini":
        write(project, "sdkconfig", "CONFIG_X=y\n")
        write(project, "CMakeLists.txt", "project(app)\n")
    write(project, setup[0], setup[1])
    write(project, "data/config.json", '{"v": 1}')
    run_hook(project, env=fake.env)
    write(project, "data/config.json", '{"v": 2}')
    run_hook(project, env=fake.env)
    assert fake.runs("pio") + fake.runs("idf.py") == 2
    why = plan_of(project, "firmware")["why"]
    assert "any:" in why or "always:" in why   # 深度驗收後:任意程式 / 未知變數 → always(更嚴格)


# ---------------------------------------------------------------- 測試類:任何變更都重跑,沒變更才沿用

def pytest_project(project, counter, body="assert True"):
    write(project, "tests/test_x.py",
          "import os\n\ndef test_x():\n"
          f"    with open(os.environ['COUNTER'], 'a') as fh:\n        fh.write('x')\n    {body}\n")
    write(project, "data/a.json", '{"v": 1}')


def test_pytest_reruns_on_any_change_but_not_when_unchanged(project, fake, tmp_path):
    counter = tmp_path / "counter"
    env = dict(fake.env, COUNTER=str(counter))
    pytest_project(project, counter)
    run_hook(project, env=env)
    run_hook(project, env=env)                               # 沒變更:不跑
    assert counter.read_text() == "x"
    write(project, "data/a.json", '{"v": 2}')                # 測試可能讀資料 → 要重跑
    run_hook(project, env=env)
    assert counter.read_text() == "xx"


def test_failing_tests_rerun_even_without_changes(project, fake, tmp_path):
    counter = tmp_path / "counter"
    env = dict(fake.env, COUNTER=str(counter))
    pytest_project(project, counter, body="assert False")
    assert run_hook(project, env=env).blocked
    assert run_hook(project, env=env).blocked
    assert counter.read_text() == "xx"


# ---------------------------------------------------------------- 自訂驗證(override)

def test_override_cached_when_unchanged_and_rerun_on_change(project, fake, tmp_path):
    counter = tmp_path / "counter"
    # Stage 3-1:改用 Python 自訂驗證,Windows 上也能跑(原本是 sh)
    write(project, ".claude/verify.py", f"open({str(counter)!r}, 'a').write('x')\n")
    configure(project, override_cache=True)   # 深度驗收後預設改為 false(自訂腳本的相依無從得知)
    write(project, "a.txt", "1")
    run_hook(project, env=fake.env)
    run_hook(project, env=fake.env)
    assert counter.read_text() == "x"
    write(project, "a.txt", "2")
    run_hook(project, env=fake.env)
    assert counter.read_text() == "xx"


def test_override_cache_false_runs_every_time(project, fake, tmp_path):
    counter = tmp_path / "counter"
    write(project, ".claude/verify.py", f"open({str(counter)!r}, 'a').write('x')\n")
    configure(project, override_cache=False)
    for _ in range(3):
        run_hook(project, env=fake.env)
    assert counter.read_text() == "xxx"


# ---------------------------------------------------------------- 變更來源:內容,不是 transcript / git

def test_change_made_outside_any_tool_call_is_still_verified(project, fake, tmp_path):
    # transcript 裡沒有任何 Edit/Write(例如是 Bash 的 sed -i 改的),一樣要驗。
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "done"}]}}) + "\n", encoding="utf-8")
    write(project, "a.json", "{}")
    run_hook(project, {"transcript_path": str(transcript)}, env=fake.env)
    (project / "a.json").write_text('{"a": }', encoding="utf-8")
    r = run_hook(project, {"transcript_path": str(transcript)}, env=fake.env)
    assert r.blocked and "a.json" in r.reason


def test_gitignored_files_are_still_verified(project, fake):
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    write(project, ".gitignore", "gen/\n")
    write(project, "gen/out.json", "{}")
    run_hook(project, env=fake.env)
    write(project, "gen/out.json", '{"a": }')
    r = run_hook(project, env=fake.env)
    assert r.blocked and "gen/out.json" in r.reason


# ---------------------------------------------------------------- 平行執行

def intervals(calls, name):
    starts = [t for k, t in calls.get(name, []) if k == "start"]
    ends = [t for k, t in calls.get(name, []) if k == "end"]
    return list(zip(starts, ends))


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def light_pair(project, fake):
    fake.make("shellcheck", sleep=1.5)
    fake.make("plutil", sleep=1.5)
    write(project, "a.sh", "#!/bin/sh\n")
    write(project, "a.plist", "<plist/>\n")


def test_independent_validators_run_in_parallel(project, fake):
    light_pair(project, fake)
    r = run_hook(project, env=fake.env)
    c = fake.calls()
    assert overlap(intervals(c, "shellcheck")[0], intervals(c, "plutil")[0]), c
    assert r.elapsed < 2.9, r.elapsed


def test_max_parallel_1_runs_sequentially(project, fake):
    light_pair(project, fake)
    configure(project, max_parallel=1)
    run_hook(project, env=fake.env)
    c = fake.calls()
    assert not overlap(intervals(c, "shellcheck")[0], intervals(c, "plutil")[0]), c


def heavy_pair(project, fake):
    fake.make("pio", sleep=1.5)
    fake.make("cargo", sleep=1.5)
    write(project, "platformio.ini", "[env:x]\n")
    write(project, "src/main.cpp", "int x;\n")
    write(project, "Cargo.toml", "[package]\nname='x'\n")


def test_heavy_validators_do_not_overlap_by_default(project, fake):
    heavy_pair(project, fake)
    run_hook(project, env=fake.env)
    c = fake.calls()
    assert not overlap(intervals(c, "pio")[0], intervals(c, "cargo")[0]), c


def test_max_heavy_2_lets_heavy_validators_overlap(project, fake):
    heavy_pair(project, fake)
    configure(project, max_heavy=2)
    run_hook(project, env=fake.env)
    c = fake.calls()
    assert overlap(intervals(c, "pio")[0], intervals(c, "cargo")[0]), c


def test_light_validators_run_alongside_a_heavy_build(project, fake):
    heavy_pair(project, fake)
    fake.make("shellcheck", sleep=1.0)
    write(project, "a.sh", "#!/bin/sh\n")
    run_hook(project, env=fake.env)
    c = fake.calls()
    heavy = intervals(c, "pio")[0]
    assert overlap(intervals(c, "shellcheck")[0], heavy), c


def test_results_are_reported_in_fixed_order_regardless_of_finish_time(project, fake):
    fake.make("pio", sleep=1.0, rc=1, output="src/main.cpp:1:1: error: slow")
    fake.make("shellcheck", rc=1, output="")
    write(project, "platformio.ini", "[env:x]\n")
    write(project, "src/main.cpp", "int x;\n")
    write(project, "bad.json", '{"a": }')
    r = run_hook(project, env=fake.env)
    assert r.reason.index("[firmware]") < r.reason.index("[json]")


def test_budget_still_bounds_parallel_runs(project, fake):
    fake.make("pio", sleep=30)
    fake.make("cargo", sleep=30)
    write(project, "platformio.ini", "[env:x]\n")
    write(project, "Cargo.toml", "[package]\n")
    configure(project, budget_seconds=6, max_heavy=2)
    r = run_hook(project, env=fake.env)
    assert r.elapsed < 15, r
    assert r.blocked and "逾時" in r.reason


# ---------------------------------------------------------------- debug 紀錄

def test_debug_records_why_each_validator_was_skipped_or_run(project, fake):
    fake.make("pio")
    pio_project(project)
    run_hook(project, env=fake.env)
    write(project, "data/config.json", '{"wifi": "z"}')
    run_hook(project, env=fake.env)
    rec = last_log(project)
    plan = {p["id"]: p for p in rec["plan"]}
    assert plan["firmware"]["action"] == "cached" and "precise" in plan["firmware"]["why"]
    assert plan["json"]["action"] == "run" and plan["json"]["files"] == ["data/config.json"]
    assert rec["changes"]["modified"] == 1


def test_no_plan_in_log_without_debug(project, fake):
    env = {k: v for k, v in fake.env.items() if k != "CLAUDE_VERIFY_DEBUG"}
    write(project, "a.json", "{}")
    run_hook(project, env=env)
    assert "plan" not in last_log(project)
