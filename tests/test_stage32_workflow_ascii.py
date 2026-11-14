"""第二輪 CI(run 43548784611)的 C1:workflow 交給 python -c 的程式只能用 ASCII。

Windows runner 的 stdout 是 cp1252(第一、二輪 CI 實測)。「環境紀錄」裡一行 print('碼頁:ACP', ...) 在那裡
UnicodeEncodeError,Windows 四格在安裝 pytest 之前就停了,測試一條都沒跑。這是 workflow 診斷程式本身的錯誤,
不是 hook 的缺陷。那一段只在 Windows 執行,在 Linux 上重放 workflow 走不到,所以要在這裡靜態檢查:
不看行號、不看是哪個平台的分支,workflow 裡每一個 python -c 都檢查。
原始碼是 ASCII、但跳脫序列(\\u78bc 之類)會印出非 ASCII 的寫法,一樣會在 cp1252 下出錯,也擋。
"""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"

_PY = r'python3?(?:\.\d+)?\)?"?\s+-c\s+'                  # python、python3、python3.12、"$(command -v python)"
_PY_C = re.compile(_PY + r'(["\'])')
_ESCAPES = re.compile(r'\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|N\{[^}]*\}|x[89a-fA-F][0-9a-fA-F])')


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def python_c_snippets(text):
    """[(行號, 程式內容)]:每一個 python -c 後面那對引號裡的內容。雙引號裡的 \\x 成對保留;可以跨行。"""
    out = []
    for m in _PY_C.finditer(text):
        quote, i, buf = m.group(1), m.end(), []
        while True:
            if i >= len(text):
                raise AssertionError(f"第 {_line_of(text, m.start())} 行的 python -c 引號沒有收尾")
            c = text[i]
            if c == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i:i + 2])
                i += 2
                continue
            if c == quote:
                break
            buf.append(c)
            i += 1
        out.append((_line_of(text, m.start()), "".join(buf)))
    return out


def non_ascii_problems(text):
    problems = []
    for line, code in python_c_snippets(text):
        chars = list(dict.fromkeys(c for c in code if ord(c) > 127))
        if chars:
            problems.append(f"第 {line} 行:python -c 裡有非 ASCII 字元 {''.join(chars)!r}")
        escapes = _ESCAPES.findall(code)
        if escapes:
            problems.append(f"第 {line} 行:python -c 裡有會印出非 ASCII 的跳脫序列 {escapes}")
    return problems


def test_every_python_c_in_the_workflow_is_ascii():
    text = WORKFLOW.read_text(encoding="utf-8")
    snippets = python_c_snippets(text)
    # 抽到的片段數必須等於 workflow 裡出現 python -c 的次數:沒有引號、或引號寫法沒處理到的會在這裡現形
    assert snippets and len(snippets) == len(re.findall(_PY, text)), snippets
    assert non_ascii_problems(text) == []


# 第二輪 CI 時 workflow 裡的兩行(原樣),以及同一類錯誤的其他寫法
PRE_FIX_ENV_RECORD = (
    '          if [ "$RUNNER_OS" = Windows ]; then\n'
    "            python -c \"import ctypes; k = ctypes.windll.kernel32; print('碼頁:ACP', k.GetACP(), 'OEMCP', "
    "k.GetOEMCP(), 'ConsoleCP', k.GetConsoleCP(), 'ConsoleOutputCP', k.GetConsoleOutputCP())\"\n"
    "          fi\n")
PRE_FIX_SECOND_PYTHON = (
    '          if [ "$RUNNER_OS" != "Windows" ]; then\n'
    "            python -c \"import os, shutil, sys; o = [p for p in (shutil.which('python3.11'), "
    "shutil.which('python3')) if p]; print('第二個 Python:', o[:1]); sys.exit(0 if o else 1)\" \\\n"
    '              || { echo "::error::找不到第二個 Python 直譯器"; missing=1; }\n'
    "          fi\n")


@pytest.mark.parametrize("fragment", [
    PRE_FIX_ENV_RECORD,
    PRE_FIX_SECOND_PYTHON,
    "      - run: python3 -c 'print(\"é\")'\n",                                    # 單引號、python3、拉丁字母
    "        run: sudo \"$(command -v python)\" -c \"print('標記')\"\n",            # 找直譯器的寫法
    "        run: python3.12 -c \"import sys; print(sys.version, '版本')\"\n",
    "        run: python -c \"print('\\u78bc\\u9801')\"\n",                         # 原始碼是 ASCII,輸出不是
    "        run: python -c \"print('\\N{CJK UNIFIED IDEOGRAPH-78BC}')\"\n",
    "        run: |\n          python -c \"import sys\n          print('第二行')\"\n",   # 跨行
    "        run: python -c \"print(\\\"x\\\"); print('ü')\"\n",                    # 引號裡有跳脫的雙引號
], ids=["pre-fix-env-record", "pre-fix-second-python", "single-quotes", "command-v", "python3.12",
        "u-escape", "N-escape", "multi-line", "escaped-quote"])
def test_the_check_catches_non_ascii_however_it_is_written(fragment):
    assert len(python_c_snippets(fragment)) == 1, fragment
    assert non_ascii_problems(fragment), fragment


def test_ascii_snippets_and_non_ascii_outside_python_c_are_fine():
    ok = ("          echo \"runner 映像:${ImageOS:-?}\"\n"                          # bash 的 echo 寫位元組,不經過 cp1252
          "          # 中文註解\n"
          "          python -c \"import ctypes; k = ctypes.windll.kernel32; print('codepages: ACP', k.GetACP())\"\n"
          "          python -c \"print('second python:', [r'C:\\\\x'])\"\n")
    assert len(python_c_snippets(ok)) == 2
    assert non_ascii_problems(ok) == []


def test_an_unterminated_snippet_is_reported_not_skipped():
    with pytest.raises(AssertionError, match="引號沒有收尾"):
        python_c_snippets("        run: python -c \"print('x')\n")
