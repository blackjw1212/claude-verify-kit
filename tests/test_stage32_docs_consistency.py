"""Stage 3.2 審查 — 文件裡寫的預設值與常數,必須等於程式裡的值。

審查時查到幾處「程式改了、說明沒跟上」:BIG_FILE 的註解仍寫「超過就不讀內容」、模組說明的優先序還是
Stage 1–3 的行為、README 的效能數字是舊預設值量的。數字會不會過時沒辦法用測試擋,但「預設值是什麼」可以。
這裡只比對 README 與程式,不比對任何量測數字。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
HOOK_SRC = (ROOT / ".claude" / "hooks" / "verify.py").read_text(encoding="utf-8")
CI_REQ = (ROOT / ".github" / "ci-requirements.txt").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")


def test_readme_default_values_match_the_code(hook):
    d = hook.DEFAULT_CONFIG
    assert d["trust_mtime"] is False and "`false`（預設）" in README and "預設 `false`" in README
    assert d["custom_verify_mode"] == "augment" and '`"augment"`（預設）' in README
    assert d["max_consecutive_blocks"] == 5 and "`max_consecutive_blocks` 次（預設 5）" in README
    assert d["max_heavy"] == 1 and "`max_heavy` 個（預設 1）" in README
    assert d["on_timeout"] == "block" and "**逾時**：預設也擋" in README
    assert d["on_missing"] == "warn" and "**工具不存在**：預設只警告" in README
    assert d["override_cache"] is False and "預設每次都跑（`override_cache: false`）" in README
    assert d["dep_scan_cap"] == hook.DEP_SCAN_CAP == 300_000 and "`dep_scan_cap`（預設 30 萬個檔）" in README
    assert hook.CACHE_FORMAT == 2 and "格式版本（目前 2）" in README
    assert hook.BIG_FILE == 64 * 1024 * 1024 and "超過 64 MB 的大檔" in README
    assert hook.PATH_SIG_CONTENT_MAX == 1024 * 1024 and "1 MiB 以內" in README


def test_readme_pytest_pins_match_ci_requirements():
    pins = dict(re.findall(r'^pytest==(\S+); python_version (<|>=) "3\.10"', CI_REQ, re.M))
    assert set(pins.values()) == {"<", ">="}, pins
    old = next(v for v, op in pins.items() if op == "<")
    new = next(v for v, op in pins.items() if op == ">=")
    assert f"3.8 用 {old}" in README and f"Python 3.10 以上用 {new}" in README
    assert f"pytest {new}" in CI_REQ and f"pytest {old}" in CI_REQ          # 檔案自己的說明也要跟著


def test_readme_skip_policy_matches_junit_summary():
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("junit_summary", ROOT / ".github" / "scripts" / "junit_summary.py")
    js = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(js)
    assert js.EXPECTED == {"linux": {"windows-only", "fs-mount"},
                           "macos": {"windows-only", "linux-only", "fs-mount"},
                           "windows": {"posix-only", "linux-only", "fs-mount"}}
    assert "Linux 只能跳過 Windows 專屬；macOS 另可跳過 Linux 專屬（`/proc`）；Windows 可跳過 POSIX 與 Linux 專屬" in README
    assert "--expect-cells 12" in WORKFLOW and "12 格齊全" in README


def test_filesystems_listed_in_readme_are_the_ones_the_tests_mount():
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("mounts", ROOT / ".github" / "scripts" / "mount_test_filesystems.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    labels = [s[0] for s in m.SPECS]
    assert labels == ["ext4", "tmpfs", "xfs", "btrfs", "overlay", "ntfs-fuse", "fat32-fuse", "exfat-fuse"]
    row = next(ln for ln in README.splitlines() if ln.startswith("| 檔案系統 |"))
    for word in ("ext4", "xfs", "btrfs", "tmpfs", "overlay", "ntfs-3g", "fusefat", "exfat-fuse"):
        assert word in row, word
    assert f"這 {len(labels)} 種" in row


@pytest.mark.parametrize("stale", [
    "POSIX 的 ctime 在寫入時一定更新",                       # Stage 3.1 推翻的假設
    "超過就不讀內容,只比 stat(大小、mtime、ctime、inode)",   # BIG_FILE 的舊註解:預設 false 之後大檔也讀內容
    "→ 只跑它,exit code 即裁決",                            # 模組說明的舊優先序:augment 之後內建檢查照跑
])
def test_statements_the_audit_found_stale_do_not_come_back(stale):
    assert stale not in HOOK_SRC
    assert stale not in README
