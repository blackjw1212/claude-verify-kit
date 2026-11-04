"""Stage 3.2 — 在真的檔案系統上驗證「內容變了一定看得到」。

Stage 3.1 實測:預設的 trust_mtime "auto" 在 FAT32 與 exFAT 的 FUSE 實作上,會放過「同大小、還原
mtime」的改寫(那兩個實作的 ctime 恆等於 mtime)。這裡把同一套情境做成測試,在每一種掛載上跑。

需要事先掛好的檔案系統(Linux、root):
    python3 .github/scripts/mount_test_filesystems.py up DIR
    CVK_FS_MOUNTS=DIR/mounts.json python3 -m pytest tests/test_stage32_fs_semantics.py
沒有 CVK_FS_MOUNTS 時整個檔案跳過(原因寫明「需要測試用檔案系統掛載」);CI 有一個專門的 job 掛好再跑,
而且那個 job 不允許任何跳過。

範圍要講清楚:FAT32 / exFAT 只測了 FUSE 實作(fusefat、exfat-fuse)。Linux 核心的 vfat / exfat
驅動、實體隨身碟、macOS / Windows 上的同名檔案系統都沒有測過,這裡的結果不能推廣到它們。
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from conftest import configure, run_hook

LABELS = ["ext4", "tmpfs", "xfs", "btrfs", "overlay", "ntfs-fuse", "fat32-fuse", "exfat-fuse"]
# 標籤對應的 mountinfo fstype。測試自己核對,不信任掛載腳本給的標籤。
FSTYPES = {
    "ext4": {"ext4"}, "tmpfs": {"tmpfs"}, "xfs": {"xfs"}, "btrfs": {"btrfs"}, "overlay": {"overlay"},
    "ntfs-fuse": {"fuse", "fuseblk", "fuse.ntfs-3g"},
    "fat32-fuse": {"fuse.fusefat"},
    "exfat-fuse": {"fuseblk", "fuse.exfat", "fuse.exfat-fuse"},
}
NO_CTIME = {"fat32-fuse", "exfat-fuse"}       # Stage 3.1 實測:改寫後 ctime 不變
REMOUNTABLE = [lb for lb in LABELS if lb != "tmpfs"]
# fusefat 拒絕非 ASCII 檔名(實測 mkdir「中文」、「é」都是 EPERM;「a b」可以)——那是這個 FUSE 實作
# 存不了這種檔名,不是 hook 的行為,所以中文路徑那一條不在 fusefat 上跑。exFAT(exfat-fuse)有跑。
UNICODE_NAMES = [lb for lb in LABELS if lb != "fat32-fuse"]
GOOD, BAD = b'{"a": 1}', b'{"a": x}'           # 一樣長
KNOWN_AUTO_MISS = "已知限制(S31-1):trust_mtime \"auto\" 在 FAT32 / exFAT 的 FUSE 實作上漏判同大小、還原 mtime 的改寫"


def _mounts():
    path = os.environ.get("CVK_FS_MOUNTS")
    if not path:
        pytest.skip("需要測試用檔案系統掛載(CVK_FS_MOUNTS 未設定)")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _mount_of(path):
    best = None
    with open("/proc/self/mountinfo", encoding="utf-8") as fh:
        for line in fh:
            left, _, right = line.partition(" - ")
            mnt = left.split()[4].replace("\\040", " ")
            if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and (best is None or len(mnt) > len(best[0])):
                best = (mnt, right.split()[0])
    return best


def _mount(label):
    m = _mounts()
    if label not in m:
        pytest.skip(f"需要測試用檔案系統掛載(CVK_FS_MOUNTS 沒有提供 {label})")
    path = m[label]["path"]
    hit = _mount_of(path)
    assert hit is not None and hit[0] == path, f"{path} 不是掛載點:{hit}"
    assert hit[1] in FSTYPES[label], f"{label} 的掛載點實際是 {hit[1]}"
    return m[label]


def _project(label, name=None):
    root = _mount(label)["path"]
    proj = os.path.join(root, name or f"t-{uuid.uuid4().hex[:8]}")
    os.makedirs(proj)
    return proj


def _past():
    t = time.time_ns() - 600 * 10 ** 9
    return t - t % (2 * 10 ** 9)                 # FAT 的 mtime 只有 2 秒的粒度


def _write(path, data, mtime):
    with open(path, "wb") as fh:
        fh.write(data)
    os.utime(path, ns=(mtime, mtime))


def _rewrite_same_size(path, data, mtime):
    with open(path, "r+b") as fh:                # 就地改寫:同一個檔案、同樣的大小
        fh.write(data)
    os.utime(path, ns=(mtime, mtime))


def _hook(proj):
    return run_hook(proj, env={"CLAUDE_VERIFY_DEBUG": "1"})


def _last_log(proj):
    with open(os.path.join(proj, ".claude", ".verify.log"), encoding="utf-8") as fh:
        return json.loads(fh.read().splitlines()[-1])


def _green_twice(proj):
    """寫好檔案、把 mtime 拉回過去,等過 racy 的 2 秒,跑兩次都綠(第二次是信任快取的那條路)。"""
    mt = _past()
    _write(os.path.join(proj, "a.json"), GOOD, mt)
    time.sleep(2.2)
    r1 = _hook(proj)
    assert not r1.blocked, r1
    time.sleep(2.2)
    r2 = _hook(proj)
    assert not r2.blocked, r2
    return mt


def _remount(label):
    for argv in _mount(label)["remount"]:
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path = _mount(label)["path"]
    t0 = time.monotonic()
    while not os.path.ismount(path):
        assert time.monotonic() - t0 < 10, f"{path} 重新掛載後沒有出現"
        time.sleep(0.1)


@pytest.fixture
def cleanup():
    made = []
    yield made
    for p in made:
        shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------- 預設值

@pytest.mark.parametrize("label", LABELS)
def test_default_detects_same_size_rewrite_with_restored_mtime(label, cleanup):
    proj = _project(label)
    cleanup.append(proj)
    mt = _green_twice(proj)
    _rewrite_same_size(os.path.join(proj, "a.json"), BAD, mt)
    r = _hook(proj)
    assert r.blocked and "a.json" in r.reason, r


@pytest.mark.parametrize("label", LABELS)
def test_default_still_reuses_the_cache_when_nothing_changed(label, cleanup):
    # 重算 SHA-256 不等於每次重跑驗證器:內容沒變 → 雜湊一樣 → 走快取
    proj = _project(label)
    cleanup.append(proj)
    _green_twice(proj)
    assert _last_log(proj)["decision"] == "allow-unchanged-green"


@pytest.mark.parametrize("label", UNICODE_NAMES)
def test_default_in_a_chinese_path_with_spaces(label, cleanup):
    proj = _project(label, f"中文 專案-{uuid.uuid4().hex[:6]}")
    cleanup.append(proj)
    mt = _green_twice(proj)
    _rewrite_same_size(os.path.join(proj, "a.json"), BAD, mt)
    r = _hook(proj)
    assert r.blocked and "a.json" in r.reason, r


@pytest.mark.parametrize("label", REMOUNTABLE)
def test_default_across_a_remount(label, cleanup):
    # 掛載變更:重新掛載後 inode 可能重新編號(FUSE 的 FAT / exFAT 實測會),結果仍要對
    proj = _project(label)
    cleanup.append(proj)
    mt = _green_twice(proj)
    _remount(label)
    r = _hook(proj)
    assert not r.blocked, r                       # 內容沒變:不能誤擋
    _rewrite_same_size(os.path.join(proj, "a.json"), BAD, mt)
    _remount(label)
    r = _hook(proj)
    assert r.blocked and "a.json" in r.reason, r


@pytest.mark.parametrize("label", sorted(NO_CTIME))
def test_unrecognised_trust_value_falls_back_to_the_safe_default(label, cleanup):
    # 打錯字(字串 "false")會被忽略並改用預設值 —— 預設值本身必須是安全的那一邊
    proj = _project(label)
    cleanup.append(proj)
    configure(proj, trust_mtime="false")
    mt = _green_twice(proj)
    _rewrite_same_size(os.path.join(proj, "a.json"), BAD, mt)
    r = _hook(proj)
    assert "trust_mtime" in r.system, r
    assert r.blocked and "a.json" in r.reason, r


# ---------------------------------------------------------------- 明確設定 "auto"(舊預設)

@pytest.mark.parametrize("label", [
    pytest.param(lb, marks=pytest.mark.xfail(strict=True, reason=KNOWN_AUTO_MISS)) if lb in NO_CTIME else lb
    for lb in LABELS])
def test_explicit_auto(label, cleanup):
    # 信任 stat 的模式:ctime 會隨寫入更新的檔案系統擋得下;FAT32 / exFAT 擋不下(strict xfail:
    # 哪天 auto 改成會辨識檔案系統、這裡開始擋得下,strict 會讓它紅,提醒更新文件)
    proj = _project(label)
    cleanup.append(proj)
    configure(proj, trust_mtime="auto")
    mt = _green_twice(proj)
    _rewrite_same_size(os.path.join(proj, "a.json"), BAD, mt)
    r = _hook(proj)
    assert r.blocked and "a.json" in r.reason, r


# ---------------------------------------------------------------- 只看 stat 的其他路徑(不信任 stat 時要讀內容)

@pytest.mark.parametrize("label", sorted(NO_CTIME))
def test_untrusted_big_file_outside_the_manifest_is_content_hashed(hook, label, cleanup):
    # 被建置設定引用、但位於略過目錄裡的檔案(_outside_hash);超過 64 MB
    proj = _project(label)
    cleanup.append(proj)
    path = os.path.join(proj, "big.bin")
    mt = _past()
    with open(path, "wb") as fh:                 # 真的寫出來:fusefat 不支援用 truncate 把檔案撐大
        chunk = b"\0" * (1024 * 1024)
        for _ in range(hook.BIG_FILE // len(chunk) + 1):
            fh.write(chunk)
    os.utime(path, ns=(mt, mt))
    m = hook.Manifest(Path(proj), None, False)
    before = m._outside_hash("big.bin")
    with open(path, "r+b") as fh:
        fh.seek(hook.BIG_FILE // 2)
        fh.write(b"x")
    os.utime(path, ns=(mt, mt))
    assert m._outside_hash("big.bin") != before


@pytest.mark.parametrize("label", sorted(NO_CTIME))
def test_untrusted_config_file_signature_reads_content(hook, label, cleanup):
    # path_sig:專案外的小設定檔(~/.npmrc、rustup 的 settings.toml、pyvenv.cfg…)
    proj = _project(label)
    cleanup.append(proj)
    path = os.path.join(proj, "settings.toml")
    mt = _past()
    _write(path, b'toolchain = "1.85"\n', mt)
    saved = hook._RUN["trust"]
    hook._RUN["trust"] = False
    try:
        before = hook.path_sig(path)
        _rewrite_same_size(path, b'toolchain = "1.86"\n', mt)
        assert hook.path_sig(path) != before
    finally:
        hook._RUN["trust"] = saved
