"""Stage 3.2 — CI 假綠燈防線(.github/scripts/junit_summary.py)本身的測試。

Stage 3.1 重現過:工具沒裝 → 測試跳過 → pytest exit 0;平台判斷壞掉 → Windows 專屬的測試在 Windows 上
也跳過 → 摘要 exit 0;少了幾格 → 摘要 exit 0。這裡用合成的 JUnit XML 把每一種情況釘住,
而且是以子程序執行、看 exit code —— workflow 靠的就是 exit code。
"""

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "junit_summary.py"

WIN = "Windows 專屬(在其他平台記為未驗證)"
POSIX = "POSIX 專屬:被訊號終止(負的 returncode)"
SH = "用 sh 腳本模擬長時間工具"
PROC = "用 /proc 找看門程序(Linux)"
FSM = "需要測試用檔案系統掛載(CVK_FS_MOUNTS 未設定)"
KNOWN = "已知限制(S31-1):trust_mtime \"auto\" 在 FAT32 / exFAT 的 FUSE 實作上漏判"


def junit(path, cases):
    """cases:[(name, status, reason)],status ∈ passed / failed / error / skipped / xfailed"""
    rows = []
    for name, status, reason in cases:
        inner = {"passed": "",
                 "failed": f"<failure message={quoteattr(reason or 'assert')}/>",
                 "error": f"<error message={quoteattr(reason or 'boom')}/>",
                 "skipped": f"<skipped type=\"pytest.skip\" message={quoteattr(reason)}/>",
                 "xfailed": f"<skipped type=\"pytest.xfail\" message={quoteattr(reason)}/>"}[status]
        rows.append(f"<testcase classname=\"tests.test_x\" name={quoteattr(name)}>{inner}</testcase>")
    Path(path).write_text("<?xml version=\"1.0\" encoding=\"utf-8\"?><testsuites><testsuite name=\"pytest\">"
                          + "".join(rows) + "</testsuite></testsuites>", encoding="utf-8")
    return path


def run(*args):
    cp = subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return cp.returncode, cp.stdout.decode("utf-8") + cp.stderr.decode("utf-8", "replace")


BASE = [("t_ok", "passed", ""), ("t_win", "skipped", WIN)]


# ---------------------------------------------------------------- check:每一格

def test_check_passes_a_normal_linux_cell(tmp_path):
    assert run("check", junit(tmp_path / "j.xml", BASE), "--os", "linux")[0] == 0


@pytest.mark.parametrize("reason", ["shellcheck 未安裝", "需要 npm", "npm 未安裝", "找不到第二個 Python 直譯器"])
def test_check_fails_when_a_tool_was_missing(tmp_path, reason):
    rc, out = run("check", junit(tmp_path / "j.xml", BASE + [("t_tool", "skipped", reason)]), "--os", "linux")
    assert rc == 1 and reason in out


def test_check_fails_when_windows_only_tests_were_skipped_on_windows(tmp_path):
    rc, out = run("check", junit(tmp_path / "j.xml", BASE), "--os", "windows")
    assert rc == 1 and "Windows 專屬的測試在 Windows 上被跳過" in out


@pytest.mark.parametrize("reason", [POSIX, SH])
def test_check_fails_when_posix_only_tests_were_skipped_on_posix(tmp_path, reason):
    for os_name in ("linux", "macos"):
        rc, out = run("check", junit(tmp_path / "j.xml", BASE + [("t_p", "skipped", reason)]), "--os", os_name)
        assert rc == 1 and "POSIX 專屬的測試在 POSIX 上被跳過" in out
    assert run("check", junit(tmp_path / "w.xml", [("t_p", "skipped", reason)]), "--os", "windows")[0] == 0


def test_check_linux_only_policy(tmp_path):
    j = junit(tmp_path / "j.xml", [("t_ok", "passed", ""), ("t_proc", "skipped", PROC)])
    assert run("check", j, "--os", "macos")[0] == 0
    assert run("check", j, "--os", "windows")[0] == 0
    rc, out = run("check", j, "--os", "linux")
    assert rc == 1 and "Linux 專屬的測試在 Linux 上被跳過" in out


def test_check_fs_mount_skips_are_expected_in_the_matrix_but_not_in_the_fs_job(tmp_path):
    j = junit(tmp_path / "j.xml", [("t_ok", "passed", ""), ("t_fs", "skipped", FSM)])
    for os_name in ("linux", "macos", "windows"):
        assert run("check", j, "--os", os_name)[0] == 0
    rc, out = run("check", j, "--os", "linux", "--no-skip")
    assert rc == 1 and "不允許任何跳過" in out


def test_check_known_limitation_xfail_is_listed_not_counted_as_pass(tmp_path):
    j = junit(tmp_path / "j.xml", [("t_ok", "passed", ""), ("t_auto[fat32]", "xfailed", KNOWN)])
    rc, out = run("check", j, "--os", "linux", "--no-skip")
    assert rc == 0 and "已知限制(不算通過):test_x::t_auto[fat32]" in out and "已知限制 1" in out


def test_check_xfail_without_a_known_limitation_reason_fails(tmp_path):
    rc, out = run("check", junit(tmp_path / "j.xml", [("t_ok", "passed", ""), ("t_x", "xfailed", "flaky")]),
                  "--os", "linux")
    assert rc == 1 and "已知限制" in out


@pytest.mark.parametrize("status", ["failed", "error"])
def test_check_fails_on_failures_and_errors(tmp_path, status):
    assert run("check", junit(tmp_path / "j.xml", BASE + [("t_bad", status, "x")]), "--os", "linux")[0] == 1


def test_check_fails_when_there_are_no_tests(tmp_path):
    rc, out = run("check", junit(tmp_path / "j.xml", []), "--os", "linux")
    assert rc == 1 and "沒有任何測試結果" in out


def test_allow_reason_is_explicit_and_printed(tmp_path):
    j = junit(tmp_path / "j.xml", BASE + [("t_tool", "skipped", "shellcheck 未安裝")])
    rc, out = run("check", j, "--os", "linux", "--allow-reason", "shellcheck 未安裝")
    assert rc == 0 and "明列放行的跳過:test_x::t_tool(shellcheck 未安裝)" in out


@pytest.mark.parametrize("sub", ["cell", "check"])
def test_unreadable_or_malformed_junit_fails(tmp_path, sub):
    extra = ["--os", "linux"] if sub == "check" else []
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites><testsuite>", encoding="utf-8")       # 被截斷
    for path in (bad, tmp_path / "missing.xml"):
        rc, out = run(sub, path, *extra)
        assert rc == 1 and "讀不到或解析失敗" in out, (path, out)


# ---------------------------------------------------------------- matrix:整個矩陣

OSES = ("ubuntu-latest", "windows-latest", "macos-latest")
PYS = ("3.8", "3.11", "3.12", "3.13")
GOOD = {"ubuntu-latest": BASE, "macos-latest": BASE,
        "windows-latest": [("t_ok", "passed", ""), ("t_win", "passed", "")]}


def full_matrix(d, override=None, skip=()):
    d.mkdir(exist_ok=True)
    for os_name in OSES:
        for py in PYS:
            if (os_name, py) in skip:
                continue
            cases = (override or {}).get((os_name, py), GOOD[os_name])
            junit(d / f"junit-{os_name}-py{py}.xml", cases)
    return d


def test_matrix_passes_when_complete_and_consistent(tmp_path):
    rc, out = run("matrix", full_matrix(tmp_path / "m"), "--expect-cells", 12)
    assert rc == 0, out
    assert "| ubuntu-latest-py3.8 | 2 | 1 | 0 | 0 | 1 | 0 |" in out        # 數量都有記錄


def test_matrix_fails_when_a_cell_is_missing(tmp_path):
    rc, out = run("matrix", full_matrix(tmp_path / "m", skip={("windows-latest", "3.13")}), "--expect-cells", 12)
    assert rc == 1 and "預期 12 格,拿到 11 格" in out


def test_matrix_fails_when_totals_differ(tmp_path):
    rc, out = run("matrix", full_matrix(tmp_path / "m", {("macos-latest", "3.12"): BASE[:1]}), "--expect-cells", 12)
    assert rc == 1 and "總數不一致" in out


def test_matrix_fails_when_skips_differ_within_one_os(tmp_path):
    # 總數一樣,但 3.11 那格多跳過一條 —— 同一個作業系統的各版本應該跳過同一批
    odd = [("t_ok", "skipped", WIN), ("t_win", "skipped", WIN)]
    rc, out = run("matrix", full_matrix(tmp_path / "m", {("ubuntu-latest", "3.11"): odd}), "--expect-cells", 12)
    assert rc == 1 and "ubuntu-latest 的各個 Python 版本跳過的測試不同" in out and "test_x::t_ok" in out


def test_matrix_allows_different_skips_across_operating_systems(tmp_path):
    rc, _ = run("matrix", full_matrix(tmp_path / "m"), "--expect-cells", 12)   # Windows 不跳過 t_win、其他跳過
    assert rc == 0


def test_matrix_fails_when_one_file_is_malformed(tmp_path):
    d = full_matrix(tmp_path / "m")
    (d / "junit-macos-latest-py3.8.xml").write_text("<testsuites>", encoding="utf-8")
    rc, out = run("matrix", d, "--expect-cells", 12)
    assert rc == 1 and "讀不到或解析失敗" in out


def test_matrix_with_no_files_fails(tmp_path):
    (tmp_path / "empty").mkdir()
    rc, out = run("matrix", tmp_path / "empty", "--expect-cells", 12)
    assert rc == 1 and "拿到 0 格" in out


# ---------------------------------------------------------------- 跳過原因的政策表

@pytest.mark.parametrize("reason,kind", [
    (WIN, "windows-only"), (PROC, "linux-only"), (POSIX, "posix-only"), (SH, "posix-only"),
    (FSM, "fs-mount"), ("shellcheck 未安裝", "unexpected"), ("需要 npm", "unexpected"),
    ("找不到第二個 Python 直譯器", "unexpected"),
])
def test_skip_policy_table(reason, kind):
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("junit_summary", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.skip_kind(reason) == kind
