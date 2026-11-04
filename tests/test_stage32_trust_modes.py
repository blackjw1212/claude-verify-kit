"""Stage 3.2 — trust_mtime 三種模式各自的行為(所有平台都跑)。

預設由 "auto" 改成 False 之後,原本靠預設值走過「信任 stat」那條路的端對端測試,現在走的是重算那條路。
"auto" 與 true 仍是支援的設定,所以這裡把它們明確設定出來,各自釘住:
  - 預設(false)真的每次重算每個檔案(不是只說說而已:看紀錄裡的 hashed 數)
  - "auto" 在 POSIX 真的信任 stat(hashed = 0),在 Windows 重算;兩者都擋得下同大小、還原 mtime 的改寫
    —— POSIX 靠 ctime(只在 ctime 會隨寫入更新的檔案系統上成立,FAT32 / exFAT 見 test_stage32_fs_semantics.py)
  - true 在所有平台都信任 stat
  - 切換模式 → 設定雜湊改變 → 快取作廢、整份重驗
"""

import json
import os
import time

from conftest import WINDOWS, configure, run_hook, write

GOOD, BAD = '{"a": 1}', '{"a": x}'          # 一樣長
FILES = 6


def _age(path, seconds=600):
    st = os.stat(path)
    t = st.st_mtime_ns - seconds * 10 ** 9
    os.utime(path, ns=(t, t))
    return t


def _log(project):
    lines = (project / ".claude/.verify.log").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def _hook(project):
    return run_hook(project, env={"CLAUDE_VERIFY_DEBUG": "1"})


def _primed(project, **cfg):
    """FILES 個合法的 JSON,mtime 拉回過去、等過 racy 的 2 秒,跑一次全綠。"""
    if cfg:
        configure(project, **cfg)
    paths = [write(project, f"d/{i}.json", GOOD) for i in range(FILES)]
    mts = [_age(p) for p in paths]
    time.sleep(2.1)
    r = _hook(project)
    assert not r.blocked, r
    return paths, mts


def _hashed_on_unchanged_run(project):
    r = _hook(project)
    assert not r.blocked, r
    rec = _log(project)
    assert rec["decision"] == "allow-unchanged-green", rec
    return rec["changes"]["hashed"]


# ---------------------------------------------------------------- 每種模式到底有沒有重算

def test_default_rehashes_every_file_on_every_stop(project):
    _primed(project)
    assert _hashed_on_unchanged_run(project) >= FILES


def test_explicit_false_rehashes_every_file_on_every_stop(project):
    _primed(project, trust_mtime=False)
    assert _hashed_on_unchanged_run(project) >= FILES


def test_explicit_auto_trusts_stat_on_posix_and_rehashes_on_windows(project):
    _primed(project, trust_mtime="auto")
    hashed = _hashed_on_unchanged_run(project)
    if WINDOWS:
        assert hashed >= FILES
    else:
        assert hashed == 0


def test_explicit_true_trusts_stat_everywhere(project):
    _primed(project, trust_mtime=True)
    assert _hashed_on_unchanged_run(project) == 0


# ---------------------------------------------------------------- "auto" 仍然擋得下的情境(原本由預設值覆蓋)

def test_explicit_auto_detects_same_size_rewrite_with_restored_mtime(project):
    paths, mts = _primed(project, trust_mtime="auto")
    hashed = _hashed_on_unchanged_run(project)
    # 前提:POSIX 上這次真的走了「信任 stat」那條路,接下來擋下靠的才是 ctime / inode 而不是重算
    assert (hashed >= FILES) if WINDOWS else (hashed == 0), hashed
    paths[3].write_text(BAD, encoding="utf-8")
    os.utime(paths[3], ns=(mts[3], mts[3]))
    r = _hook(project)
    assert r.blocked and "d/3.json:1" in r.reason, r


def test_explicit_auto_detects_a_replaced_file_with_restored_mtime(project, tmp_path):
    paths, mts = _primed(project, trust_mtime="auto")
    tmp = tmp_path / "replacement.json"
    tmp.write_text(BAD, encoding="utf-8")
    os.utime(tmp, ns=(mts[1], mts[1]))
    os.replace(str(tmp), str(paths[1]))
    r = _hook(project)
    assert r.blocked and "d/1.json:1" in r.reason, r


def test_explicit_auto_in_a_chinese_path(tmp_path):
    proj = tmp_path / "專案 目錄"
    proj.mkdir()
    configure(proj, trust_mtime="auto")
    f = write(proj, "設定/參數.json", GOOD)
    mt = _age(f)
    time.sleep(2.1)
    assert not _hook(proj).blocked
    f.write_text(BAD, encoding="utf-8")
    os.utime(f, ns=(mt, mt))
    r = _hook(proj)
    assert r.blocked and "設定/參數.json:1" in r.reason, r


def test_explicit_auto_racy_edit_with_same_size_and_mtime(project):
    # 剛改完就被掃描(2 秒內)→ 下次一律重讀;同大小、同 mtime 的第二次修改也看得到
    configure(project, trust_mtime="auto")
    f = write(project, "a.json", GOOD)
    mt = f.stat().st_mtime_ns
    assert not _hook(project).blocked
    f.write_text(BAD, encoding="utf-8")
    os.utime(f, ns=(mt, mt))
    r = _hook(project)
    assert r.blocked and "a.json" in r.reason, r


# ---------------------------------------------------------------- 設定與快取

def test_switching_trust_mode_invalidates_the_cache(project):
    _primed(project, trust_mtime="auto")
    _hashed_on_unchanged_run(project)                       # 前提:快速路徑成立(函式裡斷言了)
    configure(project, trust_mtime=False)
    r = _hook(project)
    assert not r.blocked, r
    assert _log(project)["decision"] != "allow-unchanged-green"   # 設定雜湊變了 → 不沿用


def test_unrecognised_trust_value_is_reported_and_the_default_is_used(hook, tmp_path):
    proj = tmp_path / "p"
    write(proj, ".claude/verify.json", json.dumps({"trust_mtime": "false"}))
    notes = []
    cfg = hook.load_config(proj, notes)
    assert cfg["trust_mtime"] is False
    assert hook.manifest_trust(cfg) is False
    assert any("trust_mtime" in n for n in notes)


# ---------------------------------------------------------------- 安全的預設值也落到每一個入口

def test_every_entry_point_defaults_to_not_trusting_stat(tmp_path):
    from conftest import load_hook  # noqa: PLC0415
    fresh = load_hook()                                    # 新載入:_RUN 沒被其他測試動過
    assert fresh._RUN["trust"] is False
    assert fresh.Manifest(tmp_path).trust is False
    assert fresh.Ctx(tmp_path, dict(fresh.DEFAULT_CONFIG), 0).manifest.trust is False


# ---------------------------------------------------------------- [模擬] 另外兩條原本只看 stat 的路徑
#
# 真的檔案系統上的版本在 test_stage32_fs_semantics.py(FAT32 / exFAT,需要掛載,只在專門的 CI job 跑)。
# 這裡用「ctime 固定不變」的 stat 替身(FAT 與 Windows 的語意)在所有平台上驗同一件事,
# 範圍限定在 tmp_path 底下,pytest 自己的 os.stat 不受影響。

class _PinnedCtime:
    def __init__(self, st, ctime_ns):
        self._st, self.st_ctime_ns, self.st_ctime = st, ctime_ns, ctime_ns / 1e9

    def __getattr__(self, name):
        return getattr(self._st, name)


def _pin_ctime(monkeypatch, hook, under):
    real, pinned = os.stat, {}

    def fake(path, *a, **k):
        st = real(path, *a, **k)
        key = os.path.abspath(str(path))
        if not key.startswith(str(under)):
            return st
        pinned.setdefault(key, st.st_ctime_ns)
        return _PinnedCtime(st, pinned[key])
    monkeypatch.setattr(hook.os, "stat", fake)


def test_sim_untrusted_config_file_signature_reads_content(hook, monkeypatch, tmp_path):
    f = tmp_path / "settings.toml"
    f.write_bytes(b'toolchain = "1.85"\n')
    mt = _age(f)
    _pin_ctime(monkeypatch, hook, tmp_path)
    monkeypatch.setitem(hook._RUN, "trust", False)
    before = hook.path_sig(f)
    f.write_bytes(b'toolchain = "1.86"\n')
    os.utime(f, ns=(mt, mt))
    assert hook.path_sig(f) != before
    monkeypatch.setitem(hook._RUN, "trust", True)              # 對照:信任 stat 時看不出來(文件寫明的取捨)
    trusted = hook.path_sig(f)
    f.write_bytes(b'toolchain = "1.87"\n')
    os.utime(f, ns=(mt, mt))
    assert hook.path_sig(f) == trusted


def test_sim_untrusted_big_file_outside_the_manifest_is_content_hashed(hook, monkeypatch, tmp_path):
    f = tmp_path / "big.bin"
    with open(f, "wb") as fh:
        fh.truncate(hook.BIG_FILE + 1024 * 1024)
    mt = _age(f)
    _pin_ctime(monkeypatch, hook, tmp_path)
    m = hook.Manifest(tmp_path, None, False)
    before = m._outside_hash("big.bin")
    with open(f, "r+b") as fh:
        fh.seek(hook.BIG_FILE // 2)
        fh.write(b"x")
    os.utime(f, ns=(mt, mt))
    assert m._outside_hash("big.bin") != before


# ---------------------------------------------------------------- 明確設 "auto" 時的相依目錄簽章
#
# 預設改成 false 之後,相依目錄的深度簽章在預設下會讀內容,原本那幾條端對端測試
# (test_stage3_cache_security.py、test_stage2_audit.py)走的是重算那條路,
# 守不住「信任 stat 時,簽章裡的大小 / mtime / ctime / inode」—— 反向測試 A2、S4-3 因此 MISSED。
# 這兩條明確設 "auto":POSIX 上簽章只看 stat,擋下靠的就是那幾個欄位;Windows 上 auto 會讀內容。

def _npm_project(project, fake):
    import json as _json  # noqa: PLC0415
    fake.make("npm")
    configure(project, trust_mtime="auto")
    write(project, "package.json", _json.dumps({"scripts": {"test": "x"}}))
    dep = write(project, "node_modules/left-pad/index.js", "module.exports = 1;\n")
    mt = _age(dep)
    time.sleep(2.1)
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    assert fake.runs("npm") == 1                            # 前提:第二次走了快取
    return dep, mt


def test_explicit_auto_dependency_tree_same_size_rewrite_with_restored_mtime(project, fake):
    dep, mt = _npm_project(project, fake)
    dep.write_text("module.exports = 2;\n", encoding="utf-8")
    os.utime(dep, ns=(mt, mt))
    run_hook(project, env=fake.env)
    assert fake.runs("npm") == 2


def test_explicit_auto_dependency_tree_size_change(project, fake):
    dep, mt = _npm_project(project, fake)
    dep.write_text("module.exports = 1; // longer\n", encoding="utf-8")
    os.utime(dep, ns=(mt, mt))
    run_hook(project, env=fake.env)
    assert fake.runs("npm") == 2
