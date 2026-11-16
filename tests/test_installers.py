"""安裝腳本的回歸測試(2026-11-16 安裝實測找到的三個缺陷)。

1. 三支腳本在 git 裡沒有執行權限(100644):照 README 執行 ./install-global.sh 在乾淨副本上 exit 126。
2. scan-and-apply.sh --apply 套用失敗、自己的驗收也印出 ❌,結束碼卻是 0。
3. install-global.sh 直接覆蓋使用者既有的 ~/.claude/CLAUDE.md,沒有備份。

實際執行腳本的測試只在 POSIX 上跑(Windows 的 bash 在 runner 上可能解析成 WSL,不可靠)。
HOME 一律指向暫存目錄,不碰真的 ~/.claude;git 的使用者身分與全域設定也指向暫存值。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import POSIX

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("apply-to-repo.sh", "install-global.sh", "scan-and-apply.sh")
HOOKS = ("verify", "plan-review", "loop-gate")

posix_only = pytest.mark.skipif(not POSIX, reason="POSIX 專屬:直接執行 bash 安裝腳本(Windows runner 的 bash 可能是 WSL)")


def _env(tmp_path, home, path_prefix=None):
    gitcfg = tmp_path / "empty.gitconfig"
    gitcfg.write_text("", encoding="utf-8")
    env = dict(os.environ, HOME=str(home), GIT_CONFIG_GLOBAL=str(gitcfg), GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="installer-test", GIT_AUTHOR_EMAIL="installer-test@example.invalid",
               GIT_COMMITTER_NAME="installer-test", GIT_COMMITTER_EMAIL="installer-test@example.invalid")
    if path_prefix:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return env


def _run(cmd, env, cwd):
    return subprocess.run([str(c) for c in cmd], env=env, cwd=str(cwd), capture_output=True, timeout=300)


def _git_toplevel():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(ROOT), capture_output=True, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


def test_installer_scripts_are_executable():
    if _git_toplevel() == ROOT:
        # git 工作樹:看 index 的模式。Windows 的檔案系統沒有執行位元,但 clone 到 Linux/macOS 時照 index 設定
        out = subprocess.run(["git", "ls-files", "-s", "--", *SCRIPTS], cwd=str(ROOT), capture_output=True,
                             text=True, check=True).stdout.splitlines()
        modes = {line.split("\t", 1)[1]: line.split()[0] for line in out}
        assert modes == {s: "100755" for s in SCRIPTS}, modes
    elif POSIX:
        # 不是 git 工作樹(git archive 匯出、解開的壓縮檔):git archive 與 tar 會保留執行位元,看檔案系統
        modes = {s: oct((ROOT / s).stat().st_mode & 0o777) for s in SCRIPTS}
        assert all((ROOT / s).stat().st_mode & 0o111 == 0o111 for s in SCRIPTS), modes
    else:
        pytest.skip("POSIX 專屬:不在 git 工作樹時要看檔案系統的執行位元,Windows 沒有")


@posix_only
def test_install_global_backs_up_an_existing_different_claude_md(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    mine = "# 使用者自己的全域契約\n不可遺失\n".encode("utf-8")
    (home / ".claude" / "CLAUDE.md").write_bytes(mine)
    # 照 README 的方式直接執行(./install-global.sh),同時驗證執行權限
    r = _run([ROOT / "install-global.sh"], _env(tmp_path, home), tmp_path)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    backups = sorted((home / ".claude").glob("CLAUDE.md.bak.*"))
    assert len(backups) == 1 and backups[0].read_bytes() == mine, backups
    assert (home / ".claude" / "CLAUDE.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()


@posix_only
def test_install_global_makes_no_claude_md_backup_when_absent_or_identical(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _env(tmp_path, home)
    for _ in range(2):                    # 第一次:原本沒有;第二次:內容已與 kit 相同
        r = _run([ROOT / "install-global.sh"], env, tmp_path)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        assert not list((home / ".claude").glob("CLAUDE.md.bak.*"))
    for h in HOOKS:
        assert (home / ".claude" / "hooks" / f"{h}.py").read_bytes() == (ROOT / ".claude" / "hooks" / f"{h}.py").read_bytes()


def _scan_fixture(tmp_path, apply_body=None):
    """暫存的 kit 副本 + 一個舊版(STALE)的 repo。apply_body 給定時,把 apply-to-repo.sh 換成這段內容。"""
    kit = tmp_path / "kit"
    (kit / ".claude" / "hooks").mkdir(parents=True)
    for s in SCRIPTS:
        shutil.copy2(ROOT / s, kit / s)
    for h in HOOKS:
        shutil.copy2(ROOT / ".claude" / "hooks" / f"{h}.py", kit / ".claude" / "hooks" / f"{h}.py")
    for rel in ("CLAUDE.md", ".claude/settings.json", ".claude/loop.example.json", ".claude/skills/codex-review/SKILL.md"):
        (kit / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, kit / rel)
    if apply_body is not None:
        (kit / "apply-to-repo.sh").write_text("#!/usr/bin/env bash\n" + apply_body + "\n", encoding="utf-8")
        (kit / "apply-to-repo.sh").chmod(0o755)
    root = tmp_path / "scan"
    repo = root / "stale"
    (repo / ".claude" / "hooks").mkdir(parents=True)
    for h in HOOKS:
        (repo / ".claude" / "hooks" / f"{h}.py").write_text(f"# 舊版 {h}\n", encoding="utf-8")
    shim = tmp_path / "shim"
    shim.mkdir()
    if shutil.which("md5sum") is None:    # macOS 預設沒有 md5sum;給一個輸出格式相同的替身
        (shim / "md5sum").write_text(
            f"#!{sys.executable}\nimport hashlib, sys\n"
            "for p in sys.argv[1:]:\n    print(hashlib.md5(open(p, 'rb').read()).hexdigest() + '  ' + p)\n",
            encoding="utf-8")
        (shim / "md5sum").chmod(0o755)
    env = _env(tmp_path, tmp_path / "home", shim)
    (tmp_path / "home").mkdir()
    for args in (["init", "-q", str(repo)], ["-C", str(repo), "add", "-A"], ["-C", str(repo), "commit", "-q", "-m", "old kit"]):
        subprocess.run(["git", *args], env=env, check=True, capture_output=True)
    return kit, root, repo, env


@posix_only
@pytest.mark.parametrize("apply_body, why", [
    ("exit 3", "apply-to-repo.sh 結束碼 3"),          # apply 本身失敗
    ("exit 0", "hooks:verify"),                         # apply 回 0 但什麼都沒套:靠驗收抓到
], ids=["apply-fails", "apply-does-nothing"])
def test_scan_and_apply_exits_nonzero_when_verification_fails(tmp_path, apply_body, why):
    kit, root, _repo, env = _scan_fixture(tmp_path, apply_body)
    r = _run([kit / "scan-and-apply.sh", "--root", root, "--apply"], env, tmp_path)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 1, out
    assert why in out and "有項目未通過驗收" in out, out


@posix_only
def test_scan_and_apply_exits_zero_when_everything_is_applied(tmp_path):
    kit, root, repo, env = _scan_fixture(tmp_path)
    r = _run([kit / "scan-and-apply.sh", "--root", root, "--apply"], env, tmp_path)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0 and "驗收全過" in out, out + r.stderr.decode("utf-8", "replace")
    for h in HOOKS:
        assert (repo / ".claude" / "hooks" / f"{h}.py").read_bytes() == (ROOT / ".claude" / "hooks" / f"{h}.py").read_bytes()
