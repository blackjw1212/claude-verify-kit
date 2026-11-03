"""Stage 3-4 快取安全:任何不可信的快取 → 重新驗證,不可以沿用。

已由 Stage 2 深度驗收(test_stage2_audit.py)涵蓋、這裡不重寫的情境:
  截斷/空檔/非 JSON/缺欄位/未知格式/頂層型別錯(test_damaged_cache_forces_full_verification)、
  手動修改(test_hand_edited_cache_cannot_turn_a_failure_into_a_pass)、
  複製到其他目錄(test_cache_copied_from_another_directory_is_not_trusted)、
  設定變更(test_config_change_invalidates_cached_results)、
  跨分支(test_git_branch_switch_never_reuses_the_other_branch_result)、
  兩個 hook 同時寫入(test_concurrent_runs_agree_*、test_concurrent_stress_*)、
  執行中被終止(test_killed_mid_run_*,以及 Stage 3-3 的 L1)、
  工具版本與套件就地更新(test_*_upgrade_*、test_*_package_*)。

這裡補的是 Stage 3-4 找到的缺口。
"""

import hashlib
import json
import os
import shutil
import time

import pytest

from conftest import WINDOWS, configure, run_hook, write
from test_stage2_scope_cache import idf_project

CACHE = ".claude/.verify_cache.json"
DEBUG = {"CLAUDE_VERIFY_DEBUG": "1"}
BAD = '{"a": x}'           # 跟 GOOD 一樣長
GOOD = '{"a": 1}'


def log(project):
    return [json.loads(x) for x in (project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()]


def _age(path, seconds=600):
    """把 mtime 拉到過去,然後等過 racy 的 2 秒,讓下一次觀察真的走「信任 stat」那條路。

    要等:os.utime 本身會把 ctime 更新成現在,而 racy 判斷看的是 max(mtime, ctime)。不等的話
    每次都會因為 racy 而重新雜湊,ctime / inode 比對有沒有都測不出來(反向測試 S4-2 一開始就是這樣漏抓)。"""
    st = os.stat(path)
    t = st.st_mtime_ns - seconds * 10 ** 9
    os.utime(path, ns=(t, t))
    time.sleep(2.1)
    return t


# ---------------------------------------------------------------- SHA-256

def test_manifest_hashes_are_sha256_of_the_content(project):
    f = write(project, "a.json", GOOD)
    run_hook(project)
    entry = json.loads((project / CACHE).read_text(encoding="utf-8"))["manifest"]["a.json"]
    assert entry[-1] == hashlib.sha256(f.read_bytes()).hexdigest()


def test_cache_from_the_previous_format_is_not_trusted(hook, project):
    # Stage 2 的格式(format 1、manifest 每筆 4 欄、blake2b-128):即使校驗碼正確也要整個重驗
    write(project, "a.json", BAD)
    write(project, "b.json", GOOD)
    run_hook(project)
    c = json.loads((project / CACHE).read_text(encoding="utf-8"))
    c["format"] = 1
    c["manifest"] = {k: [v[0], v[1], v[-2], "0" * 32] for k, v in c["manifest"].items()}
    c["checksum"] = hook._checksum({k: c.get(k) for k in hook.CACHE_FIELDS})
    (project / CACHE).write_text(json.dumps(c), encoding="utf-8")
    r = run_hook(project, env=DEBUG)
    assert r.blocked
    res = next(x for x in log(project)[-1]["results"] if x["id"] == "json")
    assert res["cached"] == 0 and res["checked"] == 2


# ---------------------------------------------------------------- 同大小、還原 mtime 的就地改寫

@pytest.mark.xfail(WINDOWS, strict=True,
                   reason="已知限制:Windows 的 os.stat 沒有「變更時間」(st_ctime 是建立時間),"
                          "同大小又還原 mtime 的就地改寫看不出來;要完全保險請設 trust_mtime=false")
def test_same_size_rewrite_with_restored_mtime_is_detected(project):
    f = write(project, "a.json", GOOD)
    mt = _age(f)
    assert not run_hook(project).blocked
    assert not run_hook(project).blocked
    time.sleep(0.05)
    f.write_text(BAD, encoding="utf-8")          # 內容變了、大小一樣
    os.utime(f, ns=(mt, mt))                     # mtime 也還原(touch -r / cp -p / tar 解壓)
    r = run_hook(project)
    assert r.blocked and "a.json:1" in r.reason


def test_trust_mtime_false_detects_it_on_every_platform(project):
    f = write(project, "a.json", GOOD)
    configure(project, trust_mtime=False)
    mt = _age(f)
    assert not run_hook(project).blocked
    f.write_text(BAD, encoding="utf-8")
    os.utime(f, ns=(mt, mt))
    assert run_hook(project).blocked


def test_dependency_tree_rewrite_with_restored_mtime_is_detected(project, fake):
    # node_modules 這類相依目錄的深度簽章也一樣:套件檔被同大小改寫並還原 mtime
    if WINDOWS:
        pytest.xfail("同上:Windows 沒有變更時間")
    fake.make("npm")
    write(project, "package.json", json.dumps({"scripts": {"test": "x"}}))
    dep = write(project, "node_modules/left-pad/index.js", "module.exports = 1;\n")
    mt = _age(dep)
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    assert fake.runs("npm") == 1
    dep.write_text("module.exports = 2;\n", encoding="utf-8")
    os.utime(dep, ns=(mt, mt))
    run_hook(project, env=fake.env)
    assert fake.runs("npm") == 2


# ---------------------------------------------------------------- 依賴目錄被刪除

def test_deleting_the_dependency_directory_reverifies(project, fake):
    fake.make("npm")
    write(project, "package.json", json.dumps({"scripts": {"test": "x"}}))
    write(project, "node_modules/left-pad/index.js", "module.exports = 1;\n")
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    shutil.rmtree(project / "node_modules")
    run_hook(project, env=fake.env)
    assert fake.runs("npm") == 2


def test_deleting_the_project_venv_reverifies(project, fake):
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")], check=True)
    write(project, "pkg/a.py", "X = 1\n")
    write(project, "tests/test_a.py", "def test_a():\n    assert True\n")
    run_hook(project, env=DEBUG)
    run_hook(project, env=DEBUG)
    shutil.rmtree(project / ".venv")
    run_hook(project, env=DEBUG)
    p = next(x for x in log(project)[-1].get("plan", []) if x["id"] == "pytest")
    assert p["action"] == "run"


# ---------------------------------------------------------------- 被略過的目錄裡、但被建置設定引用的檔案

def test_file_in_a_skipped_directory_referenced_by_the_build_is_tracked(project, fake):
    # dist/ 在走訪時整棵略過(通常是產物)。但 CMake 明確引用了它底下的檔案 → 那就是輸入。
    fake.make("idf.py")
    idf_project(project)
    write(project, "main/CMakeLists.txt",
          'idf_component_register(SRCS "main.c"\n    INCLUDE_DIRS "."\n'
          '    EMBED_TXTFILES "../dist/index.html")\n')
    page = write(project, "dist/index.html", "<html>v1</html>\n")
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    assert fake.runs("idf.py") == 1
    page.write_text("<html>v2</html>\n", encoding="utf-8")
    run_hook(project, env=fake.env)
    assert fake.runs("idf.py") == 2


def test_truncated_cache_at_any_point_is_not_trusted(project):
    write(project, "a.json", BAD)
    run_hook(project)
    raw = (project / CACHE).read_bytes()
    for cut in (1, len(raw) // 3, len(raw) - 1):
        (project / CACHE).write_bytes(raw[:cut])
        r = run_hook(project)
        assert r.blocked and "a.json:1" in r.reason, cut
