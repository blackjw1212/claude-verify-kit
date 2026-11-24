"""建立 / 拆除 Stage 3.2 檔案系統語意測試用的掛載點(只支援 Linux,需要 root)。

    python3 mount_test_filesystems.py up   WORKDIR   → 掛好,寫出 WORKDIR/mounts.json 並印出它的路徑
    python3 mount_test_filesystems.py down WORKDIR   → 依 mounts.json 卸載、釋放 loop 裝置、刪除映像檔

mounts.json 的格式:{"<label>": {"path": 掛載點, "remount": [[argv], ...] 或 null}, "_loops": [...]}
測試透過環境變數 CVK_FS_MOUNTS=<mounts.json 的路徑> 讀取。檔案系統種類由測試自己對
/proc/self/mountinfo 核對,不信任這份檔案的標籤(Stage 3.1 有一次 exFAT 掛載失敗,探測其實跑在
底下的 ext4 上 —— 標籤是錯的,結果也就是錯的)。

映像檔都是稀疏檔,實際佔用的空間只有寫進去的資料。
"""
import json
import os
import subprocess
import sys
import time

# label → (映像大小 MiB 或 None, 建立指令, 掛載指令);{img} {mnt} {dev} {root} 會被代換
SPECS = [
    ("ext4", 64, [["mkfs.ext4", "-q", "-F", "{img}"]], [["mount", "-o", "loop", "{img}", "{mnt}"]]),
    ("tmpfs", None, [], [["mount", "-t", "tmpfs", "-o", "size=256m", "tmpfs", "{mnt}"]]),
    ("xfs", 320, [["mkfs.xfs", "-q", "-f", "{img}"]], [["mount", "-o", "loop", "{img}", "{mnt}"]]),
    ("btrfs", 128, [["mkfs.btrfs", "-q", "-f", "{img}"]], [["mount", "-o", "loop", "{img}", "{mnt}"]]),
    # overlay 的上下層直接放在工作目錄(底下的檔案系統)裡;放進 ext4 那個掛載點的話,
    # 重新掛載 ext4 時會因為 overlay 還壓在上面而 busy
    ("overlay", None, [], [["mount", "-t", "overlay", "overlay", "-o",
                            "lowerdir={root}/ovl/lower,upperdir={root}/ovl/upper,"
                            "workdir={root}/ovl/work", "{mnt}"]]),
    ("ntfs-fuse", 64, [["mkntfs", "-q", "-F", "-f", "{img}"]], [["ntfs-3g", "{img}", "{mnt}"]]),
    ("fat32-fuse", 160, [["mkfs.vfat", "-F", "32", "{img}"]], [["fusefat", "-o", "rw+", "{img}", "{mnt}"]]),
    # exfat-fuse 只吃區塊裝置,映像檔要先接上 loop
    ("exfat-fuse", 160, [["mkfs.exfat", "{img}"]], [["mount.exfat-fuse", "{dev}", "{mnt}"]]),
]
NO_REMOUNT = {"tmpfs"}          # 卸載就清空,沒有「重新掛載後內容還在」這回事


def sh(argv, **kw):
    cp = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
    if cp.returncode:
        raise SystemExit(f"失敗(exit {cp.returncode}):{' '.join(argv)}\n{cp.stdout}")
    return cp.stdout


def wait_mounted(mnt, timeout=10):
    t0 = time.monotonic()
    while not os.path.ismount(mnt):
        if time.monotonic() - t0 > timeout:
            raise SystemExit(f"{mnt} 沒有掛上")
        time.sleep(0.1)


def _save(root, out, loops):
    path = os.path.join(root, "mounts.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dict(out, _loops=loops), fh, ensure_ascii=False, indent=1)
    return path


def up(root):
    if os.geteuid() != 0:
        raise SystemExit("需要 root(CI 上用 sudo 執行)")
    os.makedirs(root, exist_ok=True)
    out, loops = {}, []
    _save(root, out, loops)                  # 每掛好一個就存一次:中途失敗時 down 仍拆得乾淨
    for label, size, mkfs, mount in SPECS:
        img = os.path.join(root, f"{label}.img")
        mnt = os.path.join(root, f"{label}-m")
        os.makedirs(mnt, exist_ok=True)
        dev = ""
        if size:
            with open(img, "wb") as fh:
                fh.truncate(size * 1024 * 1024)
            for argv in mkfs:
                sh([a.format(img=img) for a in argv])
        if label == "exfat-fuse":
            dev = sh(["losetup", "-f", "--show", img]).strip()
            loops.append(dev)
            _save(root, out, loops)
        if label == "overlay":
            for d in ("lower", "upper", "work"):
                os.makedirs(os.path.join(root, "ovl", d), exist_ok=True)
        cmds = [[a.format(img=img, mnt=mnt, dev=dev, root=root) for a in argv] for argv in mount]
        for argv in cmds:
            sh(argv)
        wait_mounted(mnt)
        remount = None if label in NO_REMOUNT else [["umount", mnt]] + cmds
        out[label] = {"path": mnt, "remount": remount}
        _save(root, out, loops)
    print(_save(root, out, loops))


def down(root):
    path = os.path.join(root, "mounts.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    labels = [k for k in data if not k.startswith("_")]
    for label in reversed(labels):
        mnt = data[label]["path"]
        if os.path.ismount(mnt):
            sh(["umount", mnt])
    for dev in data.get("_loops", []):
        subprocess.run(["losetup", "-d", dev], check=False)
    for label in labels:
        img = os.path.join(root, f"{label}.img")
        if os.path.exists(img):
            os.remove(img)
    print("已卸載:" + "、".join(labels))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("up", "down"):
        raise SystemExit(__doc__)
    (up if sys.argv[1] == "up" else down)(os.path.abspath(sys.argv[2]))
