#!/usr/bin/env python3
"""把 pytest 的 JUnit XML 整理成 Markdown,並擋下假綠燈(只用標準函式庫,Python 3.8+)。

  junit_summary.py cell   JUNIT                    一格的摘要:總數、各測試檔的結果、跳過與已知限制的原因
  junit_summary.py check  JUNIT --os OS [--no-skip] [--allow-reason 文字 ...]
        有失敗 / 錯誤、沒有任何測試、或有不是 OS 預期的跳過 → exit 1。
        --no-skip:任何跳過都算問題(專門跑某一組測試的 job 用)。
  junit_summary.py matrix 目錄 [--expect-cells N]
        整個矩陣(檔名 junit-<os>-py<ver>.xml)。有 --expect-cells 時,下列任一項 → exit 1:
        格數不對;各格收集到的測試總數不一致;同一個作業系統的各個 Python 版本跳過的測試不同。

任何一個檔案讀不到或解析失敗 → exit 1(摘要本身失敗不可以被當成沒事)。
跳過不等於通過,已知限制(strict xfail)也不等於通過:三者分開計,每一條都列出原因。

跳過的政策(跳過原因 → 類別 → 哪些平台可以跳過):
  「Windows 專屬」          windows-only   Linux、macOS 可以跳過
  「/proc」                linux-only     macOS、Windows 可以跳過
  「POSIX」「sh 腳本」       posix-only     Windows 可以跳過
  「需要測試用檔案系統掛載」  fs-mount       一般的矩陣格都可以跳過;專門的檔案系統 job 用 --no-skip,不可以
  其他(工具沒裝、找不到第二個 Python…) unexpected   哪裡都不可以 —— 那種跳過會安靜地降低覆蓋率
已知限制:xfail 的原因必須以「已知限制」開頭,否則算問題。
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path

STATUSES = ("passed", "failed", "error", "skipped", "xfailed")
KNOWN_LIMIT_PREFIX = "已知限制"


class JUnitError(Exception):
    pass


def parse(path):
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError) as e:
        raise JUnitError(f"讀不到或解析失敗:{path}:{e}") from None
    cases = []
    for tc in root.iter("testcase"):
        name = tc.get("name", "?")
        mod = (tc.get("classname") or "?").split(".")[-1]
        status, detail = "passed", ""
        for tag in ("failure", "error", "skipped"):
            el = tc.find(tag)
            if el is not None:
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                if tag == "skipped" and el.get("type") == "pytest.xfail":
                    status = "xfailed"
                first = (el.get("message") or (el.text or "")).strip().splitlines()[0:1]
                detail = first[0][:200] if first else ""
                break
        cases.append((mod, name, status, detail))
    return cases


def counts(cases):
    c = OrderedDict((k, 0) for k in STATUSES)
    for _, _, st, _ in cases:
        c[st] += 1
    return c


def cell(path, cases=None):
    cases = parse(path) if cases is None else cases
    c = counts(cases)
    out = [f"### {Path(path).stem}", "",
           f"總數 {len(cases)}:通過 {c['passed']}、失敗 {c['failed']}、錯誤 {c['error']}、"
           f"**跳過 {c['skipped']}**、**已知限制 {c['xfailed']}**", "",
           "| 測試檔 | 通過 | 失敗 | 錯誤 | 跳過 | 已知限制 |", "|---|---|---|---|---|---|"]
    mods = OrderedDict()
    for mod, _, st, _ in cases:
        mods.setdefault(mod, counts([]))[st] += 1
    for mod, m in mods.items():
        out.append(f"| {mod} | {m['passed']} | {m['failed']} | {m['error']} | {m['skipped']} | {m['xfailed']} |")
    for label, want in (("跳過(不算通過)", "skipped"), ("已知限制(strict xfail,不算通過)", "xfailed"),
                        ("失敗", "failed"), ("錯誤", "error")):
        rows = [(mod, n, d) for mod, n, st, d in cases if st == want]
        if rows:
            out += ["", f"**{label}**", "", "| 測試 | 原因 |", "|---|---|"]
            out += [f"| {mod}::{n} | {d.replace('|', '/')} |" for mod, n, d in rows]
    return "\n".join(out) + "\n"


def skip_kind(reason):
    if "Windows 專屬" in reason:
        return "windows-only"
    if "/proc" in reason:
        return "linux-only"
    if "POSIX" in reason or "sh 腳本" in reason:
        return "posix-only"
    if "需要測試用檔案系統掛載" in reason:
        return "fs-mount"
    return "unexpected"


EXPECTED = {   # 每個平台「本來就該跳過」的類別
    "linux": {"windows-only", "fs-mount"},
    "macos": {"windows-only", "linux-only", "fs-mount"},
    "windows": {"posix-only", "linux-only", "fs-mount"},
}


def check(path, os_name, allow=(), no_skip=False):
    cases = parse(path)
    c = counts(cases)
    problems, allowed_seen = [], []
    if c["failed"] or c["error"]:
        problems.append(f"失敗 {c['failed']}、錯誤 {c['error']}")
    if not cases:
        problems.append("沒有任何測試結果")
    for mod, name, st, reason in cases:
        if st == "xfailed":
            if not reason.startswith(KNOWN_LIMIT_PREFIX):
                problems.append(f"{mod}::{name}:xfail 的原因沒有以「{KNOWN_LIMIT_PREFIX}」開頭:{reason}")
            continue
        if st != "skipped":
            continue
        kind = skip_kind(reason)
        if not no_skip and kind in EXPECTED[os_name]:
            continue
        if any(a in reason for a in allow):
            allowed_seen.append(f"{mod}::{name}({reason})")
            continue
        if no_skip:
            why = "這個 job 不允許任何跳過"
        elif os_name == "windows" and kind == "windows-only":
            why = "Windows 專屬的測試在 Windows 上被跳過"
        elif os_name != "windows" and kind == "posix-only":
            why = "POSIX 專屬的測試在 POSIX 上被跳過"
        elif os_name == "linux" and kind == "linux-only":
            why = "Linux 專屬的測試在 Linux 上被跳過"
        else:
            why = "不是這個平台預期的跳過原因"
        problems.append(f"{mod}::{name}:{why}:{reason}")
    mode = " --no-skip" if no_skip else ""
    out = [f"check {Path(path).name} --os {os_name}{mode}:總數 {len(cases)}、通過 {c['passed']}、"
           f"跳過 {c['skipped']}、已知限制 {c['xfailed']}"]
    out += [f"  已知限制(不算通過):{m}::{n}" for m, n, st, _ in cases if st == "xfailed"]
    out += [f"  明列放行的跳過:{x}" for x in allowed_seen]
    out += [f"  ✗ {x}" for x in problems] or ["  ✓ 沒有假綠燈的跡象"]
    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
    return 1 if problems else 0


def _os_of(stem):
    """junit-ubuntu-latest-py3.12 → ubuntu-latest"""
    core = stem[len("junit-"):] if stem.startswith("junit-") else stem
    return core.rsplit("-py", 1)[0]


def matrix(directory, expect_cells=None):
    files = sorted(Path(directory).rglob("junit-*.xml"))
    parsed = OrderedDict((f, parse(f)) for f in files)
    out = ["## 跨平台矩陣結果", "",
           "| 格 | 總數 | 通過 | 失敗 | 錯誤 | 跳過 | 已知限制 |", "|---|---|---|---|---|---|---|"]
    for f, cases in parsed.items():
        c = counts(cases)
        out.append(f"| {f.stem[len('junit-'):]} | {len(cases)} | {c['passed']} | {c['failed']} | "
                   f"{c['error']} | {c['skipped']} | {c['xfailed']} |")
    if not files:
        out.append("| (沒有任何 JUnit 檔:測試 job 沒有產出結果) | | | | | | |")
    out += ["", "跳過與已知限制都不計入通過;每格的原因見下方與各 job 的摘要。", ""]
    problems = []
    if expect_cells is not None:
        if len(files) != expect_cells:
            problems.append(f"預期 {expect_cells} 格,拿到 {len(files)} 格")
        totals = {f.stem: len(c) for f, c in parsed.items()}
        if len(set(totals.values())) > 1:
            problems.append("各格收集到的測試總數不一致:" + "、".join(f"{k}={v}" for k, v in totals.items()))
        by_os = OrderedDict()
        for f, cases in parsed.items():
            skipped = frozenset(f"{m}::{n}" for m, n, st, _ in cases if st in ("skipped", "xfailed"))
            by_os.setdefault(_os_of(f.stem), OrderedDict())[f.stem] = skipped
        for os_name, cells in by_os.items():
            sets = list(cells.values())
            if any(s != sets[0] for s in sets[1:]):
                union = frozenset().union(*sets)
                diff = sorted(t for t in union if not all(t in s for s in sets))
                where = "; ".join(f"{t}(跳過於:{'、'.join(k for k, s in cells.items() if t in s)})"
                                  for t in diff[:10])
                problems.append(f"{os_name} 的各個 Python 版本跳過的測試不同:{where}")
    for f, cases in parsed.items():
        out.append(cell(f, cases))
    if problems:
        out += ["", "**矩陣不完整或不一致:**", ""] + [f"- {x}" for x in problems]
    return "\n".join(out) + "\n", problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("cell")
    p.add_argument("junit")
    p = sub.add_parser("check")
    p.add_argument("junit")
    p.add_argument("--os", required=True, choices=sorted(EXPECTED))
    p.add_argument("--no-skip", action="store_true")
    p.add_argument("--allow-reason", action="append", default=[])
    p = sub.add_parser("matrix")
    p.add_argument("directory")
    p.add_argument("--expect-cells", type=int)
    args = ap.parse_args(argv[1:])
    if not args.cmd:
        ap.print_help(sys.stderr)
        return 2
    try:
        if args.cmd == "cell":
            sys.stdout.buffer.write(cell(args.junit).encode("utf-8"))
            return 0
        if args.cmd == "check":
            return check(args.junit, args.os, args.allow_reason, args.no_skip)
        text, problems = matrix(args.directory, args.expect_cells)
        sys.stdout.buffer.write(text.encode("utf-8"))
        return 1 if problems else 0
    except JUnitError as e:
        sys.stderr.buffer.write(f"junit_summary:{e}\n".encode("utf-8"))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
