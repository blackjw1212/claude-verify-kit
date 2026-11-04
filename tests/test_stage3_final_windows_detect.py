"""Stage 3 Final Acceptance — 任務二:無法可靠判斷內容有沒有變時,一律重新計算 SHA-256。

trust_mtime(這個檔案寫於 Stage 3 Final,當時預設是 "auto";Stage 3.2 起預設改為 false):
  false        預設。所有平台每次重算,連超過 64 MB 的大檔、相依目錄、專案外的小設定檔也讀內容
  "auto"       POSIX 信任 stat(大小、mtime、ctime、inode 四項都相同才沿用雜湊)、Windows 重算。
               只看平台:ctime 會不會隨寫入更新其實取決於檔案系統,FAT32 / exFAT 的 FUSE 實作不會
               (test_stage32_fs_semantics.py)。Windows 的 st_ctime 是建立時間。
  true         明確接受風險的快速模式(Windows 與 FAT32 / exFAT 上同大小、還原 mtime 的改寫會漏判)
以下名稱裡的「default」指的是當下的預設值:現在是 false,Windows 上的結果與 auto 相同。

測試分三類,報告裡分開計:
  [模擬]   在 Linux 上把 os.stat 換成 Windows 語意(ctime 不隨寫入改變)。證明的是判斷邏輯,不是 Windows 實機。
  [Windows] 只在 Windows 跑;其他平台記為未驗證。
  [跨平台] 所有平台都跑。
"""

import json
import os
import shutil
import time
import types

import pytest

from conftest import WINDOWS, configure, run_hook, write

windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows 專屬(在其他平台記為未驗證)")
GOOD, BAD = '{"a": 1}', '{"a": x}'          # 一樣長


def _age(path, seconds=600):
    st = os.stat(path)
    t = st.st_mtime_ns - seconds * 10 ** 9
    os.utime(path, ns=(t, t))
    time.sleep(2.1)                          # 等過 racy 的 2 秒,讓下一次觀察走「信任 stat」那條路
    return t


# ---------------------------------------------------------------- [模擬] 判斷邏輯

class _PinnedCtime:
    """真的 stat 結果,只有 ctime 換成第一次看到時的值(Windows 語意:st_ctime 是建立時間,寫入不會變)。"""

    def __init__(self, st, ctime_ns):
        self._st, self.st_ctime_ns, self.st_ctime = st, ctime_ns, ctime_ns / 1e9

    def __getattr__(self, name):
        return getattr(self._st, name)


def _windows_like_stat(real_stat, pinned, under=None):
    """os.stat / os.lstat 的替身。hook.os 就是全域的 os 模組,所以只改 under 底下的路徑;
    其他路徑(pytest 自己產生錯誤報告時也會呼叫 os.stat)一律回傳真的結果。
    第一版沒有這層保護:測試一失敗,pytest 的報告就因為替身缺欄位而內部崩潰,看起來像「沒選到測試」。"""
    def fake(path, *a, **k):
        st = real_stat(path, *a, **k)
        key = os.path.abspath(str(path))
        if under is not None and not key.startswith(str(under)):
            return st
        pinned.setdefault(key, st.st_ctime_ns)
        return _PinnedCtime(st, pinned[key])
    return fake


def _scan_after_rewrite(hook, monkeypatch, tmp_path, trust):
    f = tmp_path / "a.json"
    f.write_text(GOOD, encoding="utf-8")
    mt = _age(f)
    pinned = {}
    monkeypatch.setattr(hook.os, "stat", _windows_like_stat(os.stat, pinned, tmp_path))
    first = hook.Manifest(tmp_path, None, trust).scan(["a.json"])
    time.sleep(0.05)
    f.write_text(BAD, encoding="utf-8")
    os.utime(f, ns=(mt, mt))
    second = hook.Manifest(tmp_path, first.entries, trust).scan(["a.json"])
    return second.modified


def test_sim_windows_default_rehashes_and_sees_the_change(hook, monkeypatch, tmp_path):
    # [模擬] 預設設定在 Windows 上:不信任 stat → 重算 → 看得到
    trust = hook.manifest_trust(hook.DEFAULT_CONFIG, is_win=True)
    assert trust is False
    assert _scan_after_rewrite(hook, monkeypatch, tmp_path, trust) == ["a.json"]


def test_sim_windows_explicit_true_is_the_documented_risk(hook, monkeypatch, tmp_path):
    # [模擬] 明確設 trust_mtime=true:在 Windows 語意下漏判 —— 這是使用者明確接受的風險,寫成文件
    trust = hook.manifest_trust(dict(hook.DEFAULT_CONFIG, trust_mtime=True), is_win=True)
    assert trust is True
    assert _scan_after_rewrite(hook, monkeypatch, tmp_path, trust) == []


def test_trust_resolution_table(hook):
    t = hook.manifest_trust
    d = hook.DEFAULT_CONFIG
    # Stage 3.2:預設由 "auto" 改為 False(auto 在 FAT32 / exFAT 的 FUSE 實作上實測漏判,S31-1)
    assert d["trust_mtime"] is False
    assert t(dict(d, trust_mtime="auto"), is_win=False) is True       # auto 的行為不變:POSIX 信任 stat
    assert t(dict(d, trust_mtime="auto"), is_win=True) is False       # Windows:沒有可靠的變更時間
    for win in (True, False):
        assert t(dict(d, trust_mtime=True), is_win=win) is True
        assert t(dict(d, trust_mtime=False), is_win=win) is False
        assert t(d, is_win=win) is False                               # 預設:所有平台都重算
        assert t({}, is_win=win) is False                              # 沒有這個鍵:安全的那一邊
        for odd in ("false", "true", "AUTO", "auto ", 1, 0, None, [], {}):
            assert t(dict(d, trust_mtime=odd), is_win=win) is False, odd   # 不認得的值一律不信任


def test_trust_mtime_accepts_auto_true_false_only(project):
    write(project, "a.json", "{}")
    configure(project, trust_mtime="sometimes")
    r = run_hook(project)
    assert "trust_mtime" in r.system                                   # 不認得的值要說出來,並用預設


# ---------------------------------------------------------------- [跨平台] 端對端

def test_default_detects_same_size_rewrite_with_restored_mtime(project):
    f = write(project, "a.json", GOOD)
    mt = _age(f)
    assert not run_hook(project).blocked
    assert not run_hook(project).blocked
    f.write_text(BAD, encoding="utf-8")
    os.utime(f, ns=(mt, mt))
    r = run_hook(project)
    assert r.blocked and "a.json:1" in r.reason


def test_default_detects_a_replaced_file_with_restored_mtime(project, tmp_path):
    # 「寫暫存檔再改名」的替換(多數編輯器、rsync、同步工具):大小一樣、mtime 也還原
    f = write(project, "a.json", GOOD)
    mt = _age(f)
    assert not run_hook(project).blocked
    tmp = tmp_path / "replacement.json"
    tmp.write_text(BAD, encoding="utf-8")
    os.utime(tmp, ns=(mt, mt))
    os.replace(str(tmp), str(f))
    r = run_hook(project)
    assert r.blocked and "a.json:1" in r.reason


def test_default_detects_it_in_a_chinese_path(tmp_path):
    proj = tmp_path / "專案 目錄"
    proj.mkdir()
    f = write(proj, "設定/參數.json", GOOD)
    mt = _age(f)
    assert not run_hook(proj).blocked
    f.write_text(BAD, encoding="utf-8")
    os.utime(f, ns=(mt, mt))
    r = run_hook(proj)
    assert r.blocked and "設定/參數.json:1" in r.reason


def test_trust_false_rehashes_big_files_too(project):
    # 超過 64 MB 的檔案平常只比 stat;trust_mtime=false 時也要真的重算內容
    configure(project, trust_mtime=False)
    big = project / "firmware.bin"
    block = b"\0" * (1 << 20)
    with open(big, "wb") as fh:
        for _ in range(65):
            fh.write(block)
    write(project, "a.json", "{}")
    mt = _age(big)
    run_hook(project, env={"CLAUDE_VERIFY_DEBUG": "1"})
    with open(big, "r+b") as fh:                  # 同大小、改中間一個位元組
        fh.seek(30 << 20)
        fh.write(b"\1")
    os.utime(big, ns=(mt, mt))
    run_hook(project, env={"CLAUDE_VERIFY_DEBUG": "1"})
    rec = json.loads((project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["changes"]["modified"] == 1 and "firmware.bin" in rec["changes"]["sample"]


def test_default_detects_dependency_tree_rewrite_on_every_platform(project, fake):
    fake.make("npm")
    write(project, "package.json", json.dumps({"scripts": {"test": "x"}}))
    dep = write(project, "node_modules/left-pad/index.js", "module.exports = 1;\n")
    mt = _age(dep)
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    dep.write_text("module.exports = 2;\n", encoding="utf-8")
    os.utime(dep, ns=(mt, mt))
    run_hook(project, env=fake.env)
    assert fake.runs("npm") == 2


# ---------------------------------------------------------------- [Windows] 實機

@windows_only
def test_windows_explicit_trust_true_still_misses_it(project):
    # 明確選快速模式的代價:Windows 上同大小、還原 mtime 的就地改寫看不到。寫成文件,不是期望行為。
    configure(project, trust_mtime=True)
    f = write(project, "a.json", GOOD)
    mt = _age(f)
    assert not run_hook(project).blocked
    f.write_text(BAD, encoding="utf-8")
    os.utime(f, ns=(mt, mt))
    assert not run_hook(project).blocked


@windows_only
def test_windows_st_ctime_does_not_change_on_write(tmp_path):
    # 這整件事的前提:在 NTFS 上,寫入不會改變 os.stat 的 st_ctime_ns(建立時間)
    f = tmp_path / "a.txt"
    f.write_text("a", encoding="utf-8")
    c0 = os.stat(f).st_ctime_ns
    time.sleep(0.05)
    f.write_text("b", encoding="utf-8")
    assert os.stat(f).st_ctime_ns == c0


@windows_only
def test_windows_concurrent_hooks_agree_under_rehash(project):
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415
    from conftest import HOOK  # noqa: PLC0415
    for i in range(20):
        write(project, f"d/{i}.json", "{}")
    write(project, "d/7.json", BAD)
    e = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    procs = [subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              env=e, cwd=str(project)) for _ in range(3)]
    outs = [p.communicate(b"{}", timeout=120)[0] for p in procs]
    assert all(b'"decision": "block"' in o for o in outs)
    assert run_hook(project).blocked
    shutil.rmtree(project / "d")


# ---------------------------------------------------------------- [模擬] 只有 Windows 才走得到的兩條路徑
#
# 在 Linux 上,這兩條路徑的變更會先被 ctime 抓到,所以端對端測試分辨不出「有沒有真的讀內容」。
# 這裡用 Windows 語意的 stat(ctime 固定)把 ctime 那層拿掉,直接驗證內容雜湊本身。

def test_sim_untrusted_big_file_is_content_hashed(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "BIG_FILE", 4)                 # 讓 8 位元組的檔案算「大檔」
    pinned = {}
    monkeypatch.setattr(hook.os, "stat", _windows_like_stat(os.stat, pinned, tmp_path))
    f = tmp_path / "fw.bin"
    f.write_bytes(b"AAAAAAAA")
    mt = os.stat(f).st_mtime_ns
    first = hook.Manifest(tmp_path, None, False).scan(["fw.bin"])
    f.write_bytes(b"AAAABAAA")
    os.utime(f, ns=(mt, mt))
    second = hook.Manifest(tmp_path, first.entries, False).scan(["fw.bin"])
    assert second.modified == ["fw.bin"]


def test_sim_untrusted_dependency_tree_is_content_hashed(hook, monkeypatch, tmp_path):
    d = tmp_path / "node_modules" / "left-pad"
    d.mkdir(parents=True)
    f = d / "index.js"
    f.write_text("module.exports = 1;\n", encoding="utf-8")
    mt = os.stat(f).st_mtime_ns
    pinned = {}
    real_lstat = os.lstat
    monkeypatch.setattr(hook.os, "lstat", _windows_like_stat(real_lstat, pinned, tmp_path))
    monkeypatch.setitem(hook._RUN, "trust", False)
    hook._TREE_MEMO.clear()
    before = hook.tree_sig(tmp_path / "node_modules")
    f.write_text("module.exports = 2;\n", encoding="utf-8")
    os.utime(f, ns=(mt, mt))
    hook._TREE_MEMO.clear()
    after = hook.tree_sig(tmp_path / "node_modules")
    hook._TREE_MEMO.clear()
    assert before != after
