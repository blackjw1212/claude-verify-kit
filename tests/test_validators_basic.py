"""內建驗證器的正確性回歸:每一條都是舊版實測會誤擋或漏擋的情境。"""

import codecs
import shutil
import subprocess
import sys

import pytest

from conftest import WINDOWS, run_hook, write


def test_pytest_no_tests_collected_is_not_a_failure(project):
    # 舊版:tests/ 目錄存在但沒有測試 → pytest exit 5 → 誤擋。
    (project / "tests").mkdir()
    write(project, "tool.py", "print(1)\n")
    r = run_hook(project)
    assert not r.blocked, r


def test_jsonc_config_files_are_accepted(project):
    # 舊版:tsconfig.app.json 不在排除名單 → 合法的註解被當成損毀而擋下。
    write(project, "tsconfig.app.json", '{\n  // app\n  "compilerOptions": {"strict": true,},\n}\n')
    write(project, "tsconfig.json", '{ /* base */ "files": [] }')
    r = run_hook(project)
    assert not r.blocked, r


def test_broken_jsonc_config_is_still_caught(project):
    write(project, "tsconfig.json", '{\n  // ok\n  "compilerOptions": {\n}\n')
    r = run_hook(project)
    assert r.blocked and "tsconfig.json:" in r.reason


def test_comments_in_plain_json_are_still_errors(project):
    write(project, "data.json", '{\n  // not allowed here\n  "a": 1\n}\n')
    r = run_hook(project)
    assert r.blocked and "data.json:2:3" in r.reason


def test_deleted_module_is_detected(project):
    # 舊版的 mtime 快速路徑看不到刪檔:刪掉被 import 的模組後照樣放行。
    write(project, "pkg/__init__.py", "")
    write(project, "pkg/helper.py", "X = 1\n")
    write(project, "main.py", "from pkg import helper\n")
    write(project, "pytest.ini", "[pytest]\npythonpath = .\n")
    write(project, "tests/test_m.py", "import main\n\ndef test_x():\n    assert main.helper.X == 1\n")
    assert not run_hook(project).blocked
    (project / "pkg" / "helper.py").unlink()
    r = run_hook(project)
    assert r.blocked
    assert "[pytest]" in r.reason and "helper" in r.reason


def test_python_syntax_error_has_file_and_line(project):
    write(project, "a.py", "x = 1\ndef f(:\n    pass\n")
    r = run_hook(project)
    assert r.blocked and "a.py:2:" in r.reason and "SyntaxError" in r.reason


def test_python_check_does_not_write_pycache_into_project(project):
    write(project, "a.py", "x = 1\n")
    assert not run_hook(project).blocked
    assert not list(project.rglob("__pycache__"))


def test_xml_error_has_file_line_col(project):
    write(project, "filters.xml", "<feed>\n  <entry>\n</feed>\n")
    r = run_hook(project)
    assert r.blocked and "filters.xml:3:2" in r.reason


def test_paths_with_spaces_and_cjk(tmp_path):
    proj = tmp_path / "我的 專案 (測試)"
    proj.mkdir()
    write(proj, "子目錄/資料 檔.json", '{"a": }')
    write(proj, "腳本.py", "print('ok')\n")
    r = run_hook(proj)
    assert r.blocked
    assert "子目錄/資料 檔.json:1:7" in r.reason
    assert "腳本.py" not in r.reason  # 語法正確的不該出現
    write(proj, "子目錄/資料 檔.json", '{"a": 1}')
    assert not run_hook(proj).blocked


# ---------------------------------------------------------------- shellcheck 與路徑(B4)
#
# 第一次跨平台 CI(run 43137519055):Windows 的 runner 碼頁是 cp1252,shellcheck 要把「殼層 腳本\部署.sh」
# 寫到輸出時自己崩潰(hPutChar: invalid argument (invalid character)),四格都一樣。hook 有擋下,
# 但訊息不是 shellcheck 的檢查結果。這是外部工具在那種碼頁下的限制,只在下面三個條件同時成立時
# 才算「已知限制」(strict xfail,raises 限定那一個例外):
#   1. Windows,而且 ANSI / OEM / 主控台輸入 / 主控台輸出四個碼頁(非 0 的)都編不出這個測試路徑
#   2. 擋下的原因是 shellcheck,而且是那一句 hPutChar 錯誤、沒有任何 SC 檢查結果
#   3. 其他失敗(別的 shellcheck 錯誤、hook 錯誤、沒有擋下)照樣是失敗
# 測試因故通過 → XPASS(strict) → 失敗,提醒把這個標記拿掉。ASCII 路徑的對照組在每個平台都必須報出 SC2086。

CJK_DIR, CJK_SCRIPT = "殼層 腳本", "部署.sh"
SHELLCHECK_HPUTCHAR = "hPutChar: invalid argument (invalid character)"
SCRIPT_WITH_SC2086 = b"#!/bin/sh\necho $1\n"      # 位元組:Windows 的文字模式會寫成 CRLF(見 B5)


def _windows_codepage_numbers():
    """{角色: 碼頁}。shellcheck(GHC)實際用哪一個沒有確認,所以四個都列:ANSI、OEM、主控台輸入、主控台輸出。
    沒有主控台時後兩個是 0。"""
    import ctypes  # noqa: PLC0415
    k32 = ctypes.windll.kernel32
    return {"ACP": k32.GetACP(), "OEMCP": k32.GetOEMCP(),
            "ConsoleCP": k32.GetConsoleCP(), "ConsoleOutputCP": k32.GetConsoleOutputCP()}


def _windows_codepages():
    """上面每一個非 0 碼頁對應的 Python 編解碼器名稱;有一個不認得就回 None(證明不了「編不出」)。
    條件是「全部都編不出」:多列一個候選只會讓已知限制更難成立,不會更寬。"""
    names = []
    for cp in sorted(set(_windows_codepage_numbers().values())):
        if not cp:
            continue
        try:
            names.append(codecs.lookup(f"cp{cp}").name)
        except LookupError:
            return None
    return names or None


def _no_codepage_can_encode(text):
    if not WINDOWS:
        return False
    names = _windows_codepages()
    if not names:
        return False
    for name in names:
        try:
            text.encode(name)
            return False
        except UnicodeEncodeError:
            pass
    return True


CODEPAGE_CANNOT_ENCODE_CJK_PATH = _no_codepage_can_encode(CJK_DIR + "\\" + CJK_SCRIPT)


class ShellcheckCodepageLimit(Exception):
    """只有這個例外會被算成已知限制;它只在 _is_shellcheck_codepage_crash 成立時丟出。"""


def _is_shellcheck_codepage_crash(reason):
    return "[shell] shellcheck" in reason and SHELLCHECK_HPUTCHAR in reason and "[SC" not in reason


def _shellcheck_diag(r, script):
    """完整的診斷:hook 的整段原因、hook 的原始輸出,以及直接跑一次 shellcheck 的原始位元組(不經過 hook 的摘要)。"""
    sc = shutil.which("shellcheck")
    direct = subprocess.run([sc, "-f", "gcc", str(script)], cwd=str(script.parent), capture_output=True) if sc else None
    parts = [f"碼頁:{_windows_codepage_numbers() if WINDOWS else '(非 Windows)'} → {_windows_codepages() if WINDOWS else ''};"
             f"已知限制條件 1:{CODEPAGE_CANNOT_ENCODE_CJK_PATH}",
             "--- hook 的原因(完整)", r.reason, "--- hook 的原始 stdout", r.stdout, "--- hook 的 stderr", r.stderr]
    if direct is not None:
        parts += [f"--- 直接執行 {sc} -f gcc {script}(rc={direct.returncode})",
                  f"stdout={direct.stdout!r}", f"stderr={direct.stderr!r}"]
    return "\n".join(parts)


def test_shellcheck_codepage_crash_is_recognised_narrowly():
    # 條件 2、3:只認那一句 hPutChar,而且必須是 shellcheck、不能有任何檢查結果
    crash = "✗ [shell] shellcheck(exit 1)\n  x<stderr>: hPutChar: invalid argument (invalid character)"
    assert _is_shellcheck_codepage_crash(crash)
    assert not _is_shellcheck_codepage_crash(crash + "\n  部署.sh:2:6: note: Double quote [SC2086]")
    assert not _is_shellcheck_codepage_crash("✗ [shell] shellcheck(exit 1)\n  a.sh: openBinaryFile: does not exist")
    assert not _is_shellcheck_codepage_crash("✗ [python] Python 語法\n  " + SHELLCHECK_HPUTCHAR)
    assert not _is_shellcheck_codepage_crash("✗ [shell] shellcheck(exit 1)\n  hPutChar: resource vanished")


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck 未安裝")
def test_shellcheck_with_ascii_path_reports_location(tmp_path):
    # B4 的對照組:同一項檢查換成 ASCII 路徑,每個平台(包括 Windows)都必須真的跑 shellcheck 並報出 SC2086
    proj = tmp_path / "shell scripts"
    proj.mkdir()
    script = write(proj, "deploy.sh", SCRIPT_WITH_SC2086)
    r = run_hook(proj)
    assert r.blocked, _shellcheck_diag(r, script)
    assert "deploy.sh:2:" in r.reason and "SC2086" in r.reason, _shellcheck_diag(r, script)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck 未安裝")
@pytest.mark.xfail(condition=CODEPAGE_CANNOT_ENCODE_CJK_PATH, strict=True, raises=ShellcheckCodepageLimit,
                   reason="已知限制(B4):Windows 碼頁編不出路徑時,shellcheck 寫輸出就崩潰(hPutChar),不是 hook 的問題")
def test_shellcheck_with_cjk_path_reports_location(tmp_path, capsys):
    proj = tmp_path / CJK_DIR
    proj.mkdir()
    script = write(proj, CJK_SCRIPT, SCRIPT_WITH_SC2086)
    r = run_hook(proj)
    assert r.blocked, _shellcheck_diag(r, script)
    if CODEPAGE_CANNOT_ENCODE_CJK_PATH and _is_shellcheck_codepage_crash(r.reason):
        diag = _shellcheck_diag(r, script)
        with capsys.disabled():                       # xfail 不會印出例外內容:完整輸出直接寫進 CI 的紀錄
            # 寫位元組:Windows runner 的 stdout 是 cp1252,用 print 印中文本身就會 UnicodeEncodeError
            sys.stdout.flush()
            sys.stdout.buffer.write(f"\n[已知限制 B4 的完整輸出]\n{diag}\n[完整輸出結束]\n".encode("utf-8"))
            sys.stdout.buffer.flush()
        raise ShellcheckCodepageLimit(diag)
    assert "部署.sh:2:" in r.reason and "SC2086" in r.reason, _shellcheck_diag(r, script)
