#!/usr/bin/env python3
"""Stage 3-6 效能與穩定性:同一份合成專案、同樣的修改、同一個環境,比較兩個版本的 Stop hook。

專案:約 5 萬個專案檔 + 4 萬檔 node_modules(node 驗證器也會啟用)。
情境(每個版本從冷啟動開始,之後每輪依序做一次,共 --repeat 輪):
  s1     無變更
  single 改一個 JSON
  multi  每種驗證器各改一個檔(JSON、shell、Python、韌體、測試):多個驗證器並行
  tool   工具版本變更(假 pio 的執行檔換掉,專案檔沒動)
  full   全失效(刪掉快取與狀態檔):完整驗證的成本
  cold   冷啟動(只跑一次)

每一次都由一個包裝程序執行 hook,取包裝程序的 RUSAGE_CHILDREN:
  CPU = hook 與它等待過的所有子孫程序的 user + sys;峰值記憶體 = 其中最大的單一程序 RSS。
子程序數:執行前後 /proc/loadavg 最後配發的 PID 差(近似值:同時間系統其他活動也算進去)。
快取大小:執行後 .claude/.verify_cache.json 的位元組數。

只能在 POSIX 上跑(假 pio 是 sh 腳本、resource 模組)。
用法:python3 bench/stage3_perf.py --revs 2f94628,WORKTREE --repeat 20 --out result.md
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark import hook_source, make_fake_pio, make_project  # noqa: E402

WRAPPER = r"""
import json, resource, subprocess, sys, time
hook, proj = sys.argv[1], sys.argv[2]
t0 = time.perf_counter()
cp = subprocess.run([sys.executable, hook], input=b"{}", capture_output=True, cwd=proj, timeout=1800)
wall = time.perf_counter() - t0
r = resource.getrusage(resource.RUSAGE_CHILDREN)
out = cp.stdout.decode("utf-8", "replace")
print(json.dumps({"wall": wall, "cpu": r.ru_utime + r.ru_stime, "maxrss_kb": r.ru_maxrss,
                  "blocked": cp.returncode == 2 or '"decision": "block"' in out, "out": out[:300]}))
"""

TAGS = ("cold", "s1", "single", "multi", "tool", "full")
NAMES = {"cold": "冷啟動(第一次)", "s1": "無變更", "single": "改一個 JSON", "multi": "多檔(5 種驗證器並行)",
         "tool": "工具版本變更(只換 pio)", "full": "全失效(刪快取與狀態檔)"}


def last_pid():
    try:
        return int(Path("/proc/loadavg").read_text().split()[-1])
    except Exception:
        return None


def measure(hook, proj, env):
    p0 = last_pid()
    cp = subprocess.run([sys.executable, "-c", WRAPPER, str(hook), str(proj)], capture_output=True, env=env,
                        timeout=1900)
    p1 = last_pid()
    rec = json.loads(cp.stdout.decode("utf-8").strip().splitlines()[-1])
    rec["pids"] = (p1 - p0 - 2) if (p0 is not None and p1 is not None and p1 > p0) else None   # 扣掉包裝程序與 hook
    c = proj / ".claude" / ".verify_cache.json"
    rec["cache_bytes"] = c.stat().st_size if c.exists() else 0
    return rec


def bench_rev(rev, template, workdir, args, env):
    slug = rev.replace("/", "_")
    proj = workdir / f"proj-{slug}"
    shutil.copytree(template, proj, symlinks=True)
    hook = workdir / f"verify-{slug}.py"
    hook.write_text(hook_source(rev), encoding="utf-8")
    bindir = workdir / f"bin-{slug}"
    counter = workdir / f"pio-calls-{slug}"
    counter.write_text("", encoding="utf-8")
    make_fake_pio(bindir, args.build_seconds, counter)
    e = dict(env, CLAUDE_PROJECT_DIR=str(proj), PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}")
    data = {t: [] for t in TAGS}
    problems = []

    def go(tag):
        before = len(counter.read_text(encoding="utf-8"))
        rec = measure(hook, proj, e)
        rec["rebuilt"] = len(counter.read_text(encoding="utf-8")) > before
        data[tag].append(rec)
        if rec["blocked"]:
            problems.append(f"{tag}: blocked — {rec['out'][:200]}")

    go("cold")
    for k in range(args.repeat):
        go("s1")
        (proj / "data" / "item007.json").write_text(json.dumps({"id": 7, "rev": k + 1}), encoding="utf-8")
        go("single")
        (proj / "data" / "item008.json").write_text(json.dumps({"id": 8, "rev": k + 1}), encoding="utf-8")
        (proj / "scripts" / "task03.sh").write_text(f'#!/bin/sh\nset -eu\necho "rev {k}"\n', encoding="utf-8")
        (proj / "tools" / "util005.py").write_text(f"def util5(x):\n    return x * 5  # rev {k}\n", encoding="utf-8")
        p = proj / "src" / "mod005.cpp"
        p.write_text(p.read_text(encoding="utf-8") + f"// edit {k}\n", encoding="utf-8")
        go("multi")
        make_fake_pio(bindir, args.build_seconds, counter, version=f"2.{k}")
        go("tool")
        for name in (".verify_cache.json", ".verify_state.json", ".last_verify_ok"):
            f = proj / ".claude" / name
            if f.exists():
                f.unlink()
        go("full")
    return data, problems


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def summarize(recs, key, scale=1.0, nd=2):
    xs = [r[key] * scale for r in recs if r.get(key) is not None]
    if not xs:
        return "—"
    if len(xs) == 1:
        return f"{xs[0]:.{nd}f}"
    return f"{statistics.median(xs):.{nd}f} / {p95(xs):.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--revs", default="2f94628,WORKTREE")
    ap.add_argument("--repeat", type=int, default=20)
    ap.add_argument("--build-seconds", type=float, default=0.5)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--project-files", type=int, default=50000)
    ap.add_argument("--node-modules", type=int, default=40000)
    args = ap.parse_args()
    base = dict(cpp=2000, h=500, json=6000, sh=100, py=500, tests=100, md=0, node_modules=args.node_modules)
    fixed = sum(v for k, v in base.items() if k not in ("node_modules", "md")) + 4 * 1 + base["tests"]
    base["md"] = max(0, args.project_files - fixed)
    revs = [r.strip() for r in args.revs.split(",") if r.strip()]
    with tempfile.TemporaryDirectory(prefix="verify-perf3-") as tmp:
        tmp = Path(tmp)
        template = tmp / "template"
        make_project(template, base)
        env = dict(os.environ)
        env.pop("CLAUDE_VERIFY_DEBUG", None)
        nproj = sum(1 for p in template.rglob("*") if p.is_file() and "node_modules" not in p.parts)
        nmod = sum(1 for p in (template / "node_modules").rglob("*") if p.is_file())
        results = {}
        for rev in revs:
            print(f"… {rev}", file=sys.stderr, flush=True)
            results[rev] = bench_rev(rev, template, tmp, args, env)
    lines = [
        "# Stage 3-6 效能量測", "",
        f"- 環境:{platform.platform()},Python {platform.python_version()},CPU {os.cpu_count()} 核",
        f"- 合成專案:{nproj} 個專案檔 + node_modules {nmod} 檔;pio = 假的(sleep {args.build_seconds:g}s,不吃 CPU)",
        f"- 每格「中位數 / P95」;cold 只跑 1 次,其餘各 {args.repeat} 次。兩個版本用同一份專案範本、同一個環境、依序執行",
        "- 子程序數是 /proc/loadavg 最後配發 PID 的前後差,含同時間系統其他活動,是近似值",
        "",
    ]
    for metric, key, scale, unit, nd in (("耗時(秒)", "wall", 1, "", 2), ("CPU 時間(秒,user+sys,含子孫)", "cpu", 1, "", 2),
                                         ("峰值記憶體(MB,最大的單一程序)", "maxrss_kb", 1 / 1024, "", 1),
                                         ("子程序數(近似)", "pids", 1, "", 0),
                                         ("快取檔大小(MB)", "cache_bytes", 1 / 1e6, "", 2)):
        lines += [f"## {metric}", "", "| 情境 | " + " | ".join(revs) + " |", "|---|" + "---|" * len(revs)]
        for tag in TAGS:
            lines.append(f"| {NAMES[tag]} | " + " | ".join(summarize(results[r][0][tag], key, scale, nd)
                                                          for r in revs) + " |")
        lines.append("")
    lines += ["## CPU 使用率(CPU 時間 ÷ 耗時,中位數)", "", "| 情境 | " + " | ".join(revs) + " |",
              "|---|" + "---|" * len(revs)]
    for tag in TAGS:
        cells = []
        for r in revs:
            xs = [x["cpu"] / x["wall"] for x in results[r][0][tag] if x["wall"] > 0]
            cells.append(f"{statistics.median(xs) * 100:.0f}%" if xs else "—")
        lines.append(f"| {NAMES[tag]} | " + " | ".join(cells) + " |")
    lines += ["", "## 有沒有真的重建(假 pio 被呼叫的比例)", "", "| 情境 | " + " | ".join(revs) + " |",
              "|---|" + "---|" * len(revs)]
    for tag in TAGS:
        lines.append(f"| {NAMES[tag]} | " + " | ".join(
            f"{sum(x['rebuilt'] for x in results[r][0][tag])}/{len(results[r][0][tag])}" for r in revs) + " |")
    probs = [(r, p) for r in revs for p in results[r][1]]
    lines.append("")
    lines += (["**有執行被擋下:**"] + [f"- {r}: {p}" for r, p in probs]) if probs else \
        ["所有執行都是放行(沒有任何一次被擋),計時都是正常路徑。"]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps({r: results[r][0] for r in revs}, ensure_ascii=False), encoding="utf-8")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
