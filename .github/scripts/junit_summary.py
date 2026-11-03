#!/usr/bin/env python3
"""把 pytest 的 JUnit XML 整理成 Markdown(只用標準函式庫,Python 3.8+)。

  junit_summary.py cell   junit.xml   一格的摘要:總數、各測試檔的通過/失敗/跳過、跳過原因、失敗訊息
  junit_summary.py matrix 目錄         整個矩陣:每格一列(檔名 junit-<os>-py<ver>.xml)

跳過不等於通過:兩者分開計,跳過的每一條都列出原因。
"""

import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path


def parse(path):
    root = ET.parse(str(path)).getroot()
    cases = []
    for tc in root.iter("testcase"):
        name = tc.get("name", "?")
        mod = (tc.get("classname") or "?").split(".")[-1]
        status, detail = "passed", ""
        for tag in ("failure", "error", "skipped"):
            el = tc.find(tag)
            if el is not None:
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                detail = (el.get("message") or (el.text or "")).strip().splitlines()[0:1]
                detail = detail[0][:200] if detail else ""
                break
        cases.append((mod, name, status, detail))
    return cases


def counts(cases):
    c = OrderedDict((k, 0) for k in ("passed", "failed", "error", "skipped"))
    for _, _, st, _ in cases:
        c[st] += 1
    return c


def cell(path):
    cases = parse(path)
    c = counts(cases)
    out = [f"### {Path(path).stem}", "",
           f"總數 {len(cases)}:通過 {c['passed']}、失敗 {c['failed']}、錯誤 {c['error']}、**跳過 {c['skipped']}**", "",
           "| 測試檔 | 通過 | 失敗 | 錯誤 | 跳過 |", "|---|---|---|---|---|"]
    mods = OrderedDict()
    for mod, _, st, _ in cases:
        mods.setdefault(mod, counts([]))[st] += 1
    for mod, m in mods.items():
        out.append(f"| {mod} | {m['passed']} | {m['failed']} | {m['error']} | {m['skipped']} |")
    for label, want in (("跳過(不算通過)", "skipped"), ("失敗", "failed"), ("錯誤", "error")):
        rows = [(mod, n, d) for mod, n, st, d in cases if st == want]
        if rows:
            out += ["", f"**{label}**", "", "| 測試 | 原因 |", "|---|---|"]
            out += [f"| {mod}::{n} | {d.replace('|', '/')} |" for mod, n, d in rows]
    return "\n".join(out) + "\n"


def matrix(directory):
    files = sorted(Path(directory).rglob("junit-*.xml"))
    out = ["## 跨平台矩陣結果", "", "| 格 | 總數 | 通過 | 失敗 | 錯誤 | 跳過 |", "|---|---|---|---|---|---|"]
    for f in files:
        c = counts(parse(f))
        out.append(f"| {f.stem[len('junit-'):]} | {sum(c.values())} | {c['passed']} | {c['failed']} | "
                   f"{c['error']} | {c['skipped']} |")
    if not files:
        out.append("| (沒有任何 JUnit 檔:測試 job 沒有產出結果) | | | | | |")
    out += ["", "跳過的測試不計入通過;每格的跳過原因見各 job 的摘要。", ""]
    for f in files:
        out.append(cell(f))
    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) != 3 or argv[1] not in ("cell", "matrix"):
        print(__doc__, file=sys.stderr)
        return 2
    text = cell(argv[2]) if argv[1] == "cell" else matrix(argv[2])
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
