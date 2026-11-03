#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兩個吃滿 CPU 的建置同時跑 vs 依序跑:max_heavy=1 與 max_heavy=2 的實測比較。

「建置」用真的 gcc -O2 編譯產生出來的 C 程式碼(make -j<CPU 數>),所以 CPU 負載是真的;
但它不是 PlatformIO / ESP-IDF —— 本機沒有那些工具鏈,也連不到 PlatformIO registry。
韌體驗證器(假 pio)與 Rust 驗證器(假 cargo)各自觸發一次完整重編。

用法:python3 bench/cpu_contention.py [--files 60] [--repeat 3] [--out result.md]
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "verify.py"


def make_sources(d, nfiles, stmts):
    d.mkdir(parents=True, exist_ok=True)
    for i in range(nfiles):
        body = []
        for f in range(10):
            lines = "\n".join(f"    x = x * {k + 3} + ((x >> 3) ^ {k * 7 + f});" for k in range(stmts))
            body.append(f"unsigned f{i}_{f}(unsigned x) {{\n{lines}\n    return x;\n}}\n")
        (d / f"m{i:03d}.c").write_text("".join(body), encoding="utf-8")
    (d / "Makefile").write_text(
        "SRC := $(wildcard *.c)\nOBJ := $(SRC:.c=.o)\nall: $(OBJ)\n%.o: %.c\n\tgcc -O2 -c $< -o $@\n",
        encoding="utf-8")


def make_tool(bindir, name, srcdir, log):
    s = bindir / name
    s.write_text(f'#!/bin/sh\necho "{name} start $(date +%s.%N)" >> "{log}"\n'
                 f'make -s -B -j"$(nproc)" -C "{srcdir}" >/dev/null || exit 1\n'
                 f'echo "{name} end $(date +%s.%N)" >> "{log}"\n', encoding="utf-8")
    s.chmod(0o755)


def run_hook(project, env):
    t0 = time.perf_counter()
    cp = subprocess.run([sys.executable, str(HOOK)], input=b"{}", capture_output=True, env=env,
                        cwd=str(project), timeout=900)
    return time.perf_counter() - t0, cp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=60)
    ap.add_argument("--stmts", type=int, default=300)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if not shutil.which("gcc") or not shutil.which("make"):
        print("需要 gcc 與 make")
        return 2
    with tempfile.TemporaryDirectory(prefix="verify-cpu-") as tmp:
        tmp = Path(tmp)
        bindir, log = tmp / "bin", tmp / "calls.log"
        bindir.mkdir()
        make_sources(tmp / "fw", args.files, args.stmts)
        make_sources(tmp / "rs", args.files, args.stmts)
        make_tool(bindir, "pio", tmp / "fw", log)
        make_tool(bindir, "cargo", tmp / "rs", log)
        env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
        env.pop("CLAUDE_VERIFY_DEBUG", None)

        t0 = time.perf_counter()
        subprocess.run(["make", "-s", "-B", f"-j{os.cpu_count()}", "-C", str(tmp / "fw")], check=True,
                       stdout=subprocess.DEVNULL)
        single = time.perf_counter() - t0

        rows = {}
        for heavy in (1, 2):
            walls, overlaps = [], []
            for k in range(args.repeat):
                proj = tmp / f"p{heavy}-{k}"
                proj.mkdir()
                (proj / "platformio.ini").write_text("[env:x]\n", encoding="utf-8")
                (proj / "src").mkdir()
                (proj / "src" / "main.cpp").write_text("int x;\n", encoding="utf-8")
                (proj / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
                (proj / ".claude").mkdir()
                (proj / ".claude" / "verify.json").write_text(json.dumps({"max_heavy": heavy}), encoding="utf-8")
                log.write_text("", encoding="utf-8")
                dt, cp = run_hook(proj, dict(env, CLAUDE_PROJECT_DIR=str(proj)))
                if b'"decision": "block"' in cp.stdout:
                    print(f"警告:max_heavy={heavy} 第 {k + 1} 次被擋:{cp.stdout[:300]!r}")
                ev = {}
                for ln in log.read_text(encoding="utf-8").split("\n"):
                    if ln.strip():
                        tool, kind, ts = ln.split()
                        ev[(tool, kind)] = float(ts)
                a = (ev[("pio", "start")], ev[("pio", "end")])
                b = (ev[("cargo", "start")], ev[("cargo", "end")])
                walls.append(dt)
                overlaps.append(a[0] < b[1] and b[0] < a[1])
            rows[heavy] = (walls, overlaps)

    def f(xs):
        return f"{statistics.median(xs):.2f}({min(xs):.2f}–{max(xs):.2f})"
    lines = [
        "# 兩個 CPU 密集建置:依序 vs 同時",
        "",
        f"- CPU {os.cpu_count()} 核;每個「建置」= gcc -O2 編 {args.files} 個產生的 C 檔(make -j{os.cpu_count()})",
        f"- 單一建置單獨執行:{single:.2f} s",
        "- 這是真的 gcc 編譯負載,**不是 PlatformIO / ESP-IDF**(本機沒有工具鏈,也連不到 PlatformIO registry)",
        "",
        "| 設定 | hook 總耗時(秒,中位數(最小–最大)) | 兩個建置是否重疊 |",
        "|---|---|---|",
        f"| max_heavy=1(預設,依序) | {f(rows[1][0])} | {sum(rows[1][1])}/{len(rows[1][1])} 次重疊 |",
        f"| max_heavy=2(同時) | {f(rows[2][0])} | {sum(rows[2][1])}/{len(rows[2][1])} 次重疊 |",
    ]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
