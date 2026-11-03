#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify.py 的效能基準:同一個合成專案、同樣的修改,量不同版本的 Stop hook 總耗時。

情境(每個版本各自從冷啟動開始,之後依序重複):
  cold   第一次執行(沒有任何快取 / 標記)
  s1     無變更:上一輪全綠之後什麼都沒改
  s2     只改一個 JSON
  s3     改了一個韌體原始碼(.cpp)

合成專案:PlatformIO 韌體(src/*.cpp、include/*.h)+ data/*.json + scripts/*.sh
+ tools/*.py 與 tests/(pytest)+ docs/*.md。
韌體建置用「假 pio」代替(固定 sleep --build-seconds 秒,不吃 CPU):本機沒有 PlatformIO,
而這裡要量的是「hook 有沒有去觸發建置」,不是建置本身多快。shellcheck、pytest、
Python 語法、JSON 檢查都是真的在跑。

用法(在 repo 根目錄):
  python3 bench/benchmark.py                      # 預設比較 bf696de、opt/stage-1-correctness、工作樹
  python3 bench/benchmark.py --revs bf696de,WORKTREE --repeat 3 --build-seconds 2
  python3 bench/benchmark.py --out bench/result.md
只用標準函式庫。每次執行都在暫存目錄,不會動到 repo。
"""

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_REL = ".claude/hooks/verify.py"


def hook_source(rev):
    if rev == "WORKTREE":
        return (ROOT / HOOK_REL).read_text(encoding="utf-8")
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:{HOOK_REL}"],
                          check=True, capture_output=True, text=True, encoding="utf-8").stdout


def make_project(dst, sizes):
    def w(rel, text):
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    w("platformio.ini", "[env:esp32dev]\nplatform = espressif32\nboard = esp32dev\nframework = arduino\n")
    for i in range(sizes["cpp"]):
        w(f"src/mod{i:03d}.cpp", f'#include "mod{i % sizes["h"]:03d}.h"\nint f{i}(int x) {{ return x + {i}; }}\n')
    for i in range(sizes["h"]):
        w(f"include/mod{i:03d}.h", f"#pragma once\nint f{i}(int x);\n")
    for i in range(sizes["json"]):
        w(f"data/item{i:03d}.json", json.dumps({"id": i, "name": f"item {i}", "tags": ["a", "b"],
                                                "values": list(range(20))}))
    w("data/big.json", json.dumps({"rows": [{"i": i, "s": "x" * 40} for i in range(8000)]}))
    for i in range(sizes["sh"]):
        w(f"scripts/task{i:02d}.sh", f'#!/bin/sh\nset -eu\nname="task{i}"\necho "$name"\n')
    for i in range(sizes["py"]):
        w(f"tools/util{i:03d}.py", f"def util{i}(x):\n    return x * {i}\n")
    w("tools/__init__.py", "")
    w("pytest.ini", "[pytest]\npythonpath = .\n")
    for i in range(sizes["tests"]):
        body = "".join(f"\ndef test_{i}_{k}():\n    assert util{i}({k}) == {k} * {i}\n" for k in range(5))
        w(f"tests/test_util{i:03d}.py", f"from tools.util{i:03d} import util{i}\n{body}")
    for i in range(sizes["md"]):
        w(f"docs/page{i:02d}.md", f"# Page {i}\n\nText.\n")


def make_fake_pio(bindir, seconds):
    bindir.mkdir(parents=True, exist_ok=True)
    pio = bindir / "pio"
    pio.write_text(f"#!/bin/sh\nsleep {seconds}\necho 'fake pio: build ok'\nexit 0\n", encoding="utf-8")
    pio.chmod(0o755)


def run_once(hook, project, env):
    t0 = time.perf_counter()
    cp = subprocess.run([sys.executable, str(hook)], input=b"{}", capture_output=True,
                        env=env, cwd=str(project), timeout=900)
    dt = time.perf_counter() - t0
    out = cp.stdout.decode("utf-8", "replace").strip()
    blocked = cp.returncode == 2 or ('"decision": "block"' in out)
    return dt, blocked, out


def bench_rev(rev, template, workdir, args, env):
    proj = workdir / f"proj-{rev.replace('/', '_')}"
    shutil.copytree(template, proj)
    hook = workdir / f"verify-{rev.replace('/', '_')}.py"
    hook.write_text(hook_source(rev), encoding="utf-8")
    e = dict(env, CLAUDE_PROJECT_DIR=str(proj))
    times = {"cold": [], "s1": [], "s2": [], "s3": []}
    problems = []

    def go(tag):
        dt, blocked, out = run_once(hook, proj, e)
        times[tag].append(dt)
        if blocked:
            problems.append(f"{tag}: blocked — {out[:200]}")

    go("cold")
    for k in range(args.repeat):
        go("s1")
        (proj / "data" / "item007.json").write_text(json.dumps({"id": 7, "rev": k + 1}), encoding="utf-8")
        go("s2")
        p = proj / "src" / "mod005.cpp"
        p.write_text(p.read_text(encoding="utf-8") + f"// edit {k}\n", encoding="utf-8")
        go("s3")
    return times, problems


def fmt(xs):
    if not xs:
        return "—"
    if len(xs) == 1:
        return f"{xs[0]:.2f}"
    return f"{statistics.median(xs):.2f}({min(xs):.2f}–{max(xs):.2f})"


def tool_version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        ver = [ln for ln in out if "version" in ln.lower()]
        return (ver or out or ["?"])[0].strip()
    except Exception:
        return "未安裝"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--revs", default="bf696de,opt/stage-1-correctness,WORKTREE")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--build-seconds", type=float, default=3.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--small", action="store_true", help="小專案(給快速自測用)")
    args = ap.parse_args()
    sizes = (dict(cpp=10, h=4, json=20, sh=3, py=5, tests=3, md=3) if args.small else
             dict(cpp=120, h=40, json=400, sh=25, py=60, tests=30, md=40))
    revs = [r.strip() for r in args.revs.split(",") if r.strip()]

    with tempfile.TemporaryDirectory(prefix="verify-bench-") as tmp:
        tmp = Path(tmp)
        template = tmp / "template"
        make_project(template, sizes)
        make_fake_pio(tmp / "bin", args.build_seconds)
        env = dict(os.environ)
        env["PATH"] = f"{tmp / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        env.pop("CLAUDE_VERIFY_DEBUG", None)
        nfiles = sum(1 for p in template.rglob("*") if p.is_file())
        results = {}
        for rev in revs:
            print(f"… {rev}", file=sys.stderr, flush=True)
            results[rev] = bench_rev(rev, template, tmp, args, env)

    lines = [
        "# verify.py benchmark",
        "",
        f"- 環境:{platform.platform()},Python {platform.python_version()},CPU {os.cpu_count()} 核",
        f"- 工具:shellcheck `{tool_version(['shellcheck', '--version'])[:60]}`;"
        f"pytest `{tool_version([sys.executable, '-m', 'pytest', '--version'])}`;"
        f"pio = 假的(sleep {args.build_seconds:g}s)",
        f"- 合成專案 {nfiles} 個檔案:{sizes}",
        f"- 每格:中位數(最小–最大),單位秒;cold 跑 1 次,其餘各 {args.repeat} 次",
        "",
        "| 情境 | " + " | ".join(revs) + " |",
        "|---|" + "---|" * len(revs),
    ]
    names = {"cold": "冷啟動(第一次)", "s1": "1. 無變更", "s2": "2. 只改一個 JSON", "s3": "3. 改韌體原始碼"}
    for tag in ("s1", "s2", "s3", "cold"):
        lines.append(f"| {names[tag]} | " + " | ".join(fmt(results[r][0][tag]) for r in revs) + " |")
    probs = [(r, p) for r in revs for p in results[r][1]]
    lines.append("")
    if probs:
        lines.append("**有執行被擋下(計時可能不代表正常路徑):**")
        lines.extend(f"- {r}: {p}" for r, p in probs)
    else:
        lines.append("所有執行都是放行(沒有任何一次被擋),計時都是正常路徑。")
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
