"""Stage 3.2 — shellcheck 讀的專案外設定必須算進工具識別。

實測重現(修正前):用 SHELLCHECK_OPTS、家目錄的 .shellcheckrc、或專案上層目錄的 .shellcheckrc 把警告
關掉跑一次 → 通過並寫進快取;拿掉那個設定再跑 → shellcheck 此時會報問題,hook 卻沿用快取的「通過」。
這裡用真的 shellcheck 端對端驗證(CI 的必要工具檢查保證它在)。
"""

import os
import shutil

import pytest

from conftest import POSIX, run_hook, write

needs_shellcheck = pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck 未安裝")
BAD = "#!/bin/sh\necho $a\n"                   # SC2086 + SC2154
HIDE = "disable=SC2086,SC2154\n"


def _proj(tmp_path):
    proj = tmp_path / "outer" / "proj"
    # 給真的 shellcheck 讀的檔案一律寫位元組:文字模式在 Windows 會寫成 CRLF,shellcheck 改報 SC1017
    # (第一次跨平台 CI 實測),測試就變成在驗換行而不是驗設定。
    write(proj, "a.sh", BAD.encode("utf-8"))
    home = tmp_path / "home"
    home.mkdir()
    return proj, home


def _hook(proj, home, **env):
    e = {"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
         "SHELLCHECK_OPTS": ""}
    e.update(env)
    return run_hook(proj, env=e)


@needs_shellcheck
def test_baseline_the_script_really_has_warnings(tmp_path):
    proj, home = _proj(tmp_path)
    r = _hook(proj, home)
    assert r.blocked and "a.sh" in r.reason and "SC2086" in r.reason, r.reason
    assert "SC1017" not in r.reason, r.reason        # CRLF 時 SC2086 照樣會出現,要另外確認輸入真的是 LF


@needs_shellcheck
def test_removing_shellcheck_opts_reverifies(tmp_path):
    proj, home = _proj(tmp_path)
    assert not _hook(proj, home, SHELLCHECK_OPTS="-e SC2086,SC2154").blocked
    r = _hook(proj, home)
    assert r.blocked and "a.sh" in r.reason and "SC2086" in r.reason, r.reason


@needs_shellcheck
def test_removing_a_parent_directory_shellcheckrc_reverifies(tmp_path):
    proj, home = _proj(tmp_path)
    rc = tmp_path / "outer" / ".shellcheckrc"           # 專案外、專案的上一層
    rc.write_bytes(HIDE.encode("utf-8"))
    assert not _hook(proj, home).blocked
    rc.unlink()
    r = _hook(proj, home)
    assert r.blocked and "a.sh" in r.reason and "SC2086" in r.reason, r.reason


@needs_shellcheck
@pytest.mark.skipif(not POSIX, reason="POSIX 專屬:shellcheck 在 Windows 上讀使用者設定的位置未確認")
def test_removing_a_home_shellcheckrc_reverifies(tmp_path):
    proj, home = _proj(tmp_path)
    rc = home / ".shellcheckrc"
    rc.write_bytes(HIDE.encode("utf-8"))
    assert not _hook(proj, home).blocked
    rc.unlink()
    r = _hook(proj, home)
    assert r.blocked and "a.sh" in r.reason and "SC2086" in r.reason, r.reason


@needs_shellcheck
@pytest.mark.skipif(not POSIX, reason="POSIX 專屬:XDG_CONFIG_HOME 是 POSIX 的慣例")
def test_removing_an_xdg_shellcheckrc_reverifies(tmp_path):
    proj, home = _proj(tmp_path)
    (home / ".config").mkdir()
    rc = home / ".config" / "shellcheckrc"
    rc.write_bytes(HIDE.encode("utf-8"))
    assert not _hook(proj, home).blocked
    rc.unlink()
    r = _hook(proj, home)
    assert r.blocked and "a.sh" in r.reason and "SC2086" in r.reason, r.reason


def test_external_config_is_part_of_the_shell_identity(hook, tmp_path, monkeypatch):
    # 不需要真的 shellcheck:直接看識別字串會不會跟著變
    proj = tmp_path / "outer" / "proj"
    write(proj, "a.sh", BAD)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("SHELLCHECK_OPTS", raising=False)
    ctx = hook.Ctx(proj, dict(hook.DEFAULT_CONFIG), 0)
    sig = hook._shellcheck_config_sig
    base = sig(ctx)
    monkeypatch.setenv("SHELLCHECK_OPTS", "-S error")
    assert sig(ctx) != base
    monkeypatch.delenv("SHELLCHECK_OPTS")
    for path in (home / ".shellcheckrc", tmp_path / "outer" / ".shellcheckrc", tmp_path / "outer" / "shellcheckrc"):
        path.write_text(HIDE, encoding="utf-8")
        assert sig(ctx) != base, path
        path.unlink()
    assert sig(ctx) == base
    os.makedirs(home / ".config")
    (home / ".config" / "shellcheckrc").write_text(HIDE, encoding="utf-8")
    assert sig(ctx) != base
