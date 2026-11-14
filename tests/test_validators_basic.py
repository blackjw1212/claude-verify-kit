"""內建驗證器的正確性回歸:每一條都是舊版實測會誤擋或漏擋的情境。"""

import codecs
import inspect
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
# 第一次跨平台 CI(run 43137519055):Windows runner 上,經由 hook 執行的 shellcheck 要把「殼層 腳本\部署.sh」
# 寫到輸出時自己崩潰(hPutChar: invalid argument (invalid character)),四格都一樣。hook 有擋下,
# 但訊息不是 shellcheck 的檢查結果。只在下面三個條件同時成立時才算「已知限制」
# (strict xfail,raises 限定那一個例外):
#   1. Windows,而且 ANSI 碼頁(GetACP)無法無損編碼這個測試路徑:Python 的嚴格編碼(加上解碼還原)與
#      Windows 自己的 WideCharToMultiByte(WC_NO_BEST_FIT_CHARS,不接受 best-fit 替代字元)兩邊都說編不出。
#      任何一步不確定(碼頁不認得、API 呼叫失敗、兩邊不一致)→ 條件不成立,不套用 xfail。
#   2. 擋下的原因是 shellcheck,而且是那一句 hPutChar 錯誤、沒有任何 SC 檢查結果
#   3. 其他失敗(別的 shellcheck 錯誤、hook 錯誤、沒有擋下)照樣是失敗
# 測試因故通過 → XPASS(strict) → 失敗,提醒把這個標記拿掉。ASCII 路徑的對照組在每個平台都必須報出 SC2086。
#
# 為什麼條件 1 只看 ACP(第三輪 CI,run 43552531737,實測):runner 的碼頁是 ACP 1252、OEMCP 437、
# 主控台輸入／輸出 65001。測試直接執行同一個 shellcheck(繼承 pytest 的主控台)時正常印出 UTF-8 路徑與 SC2086;
# 經由 hook 執行(run_tool 用 CREATE_NO_WINDOW,子程序沒有主控台)時崩潰。主控台碼頁編得出這個路徑、
# ACP 編不出,所以第二輪的「四個碼頁都編不出」不成立,沒有被算成已知限制。
# 推定(沒有查證):shellcheck(GHC 編譯)沒有主控台時用 ACP 寫輸出。這只是與上面兩個觀察一致的解釋,
# 不是確認過的 GHC 行為;這個條件是這次 CI 實測支持的條件。也不能用 chcp 代替 GetACP:chcp 回報的是主控台碼頁。

CJK_DIR, CJK_SCRIPT = "殼層 腳本", "部署.sh"
SHELLCHECK_HPUTCHAR = "hPutChar: invalid argument (invalid character)"
SCRIPT_WITH_SC2086 = b"#!/bin/sh\necho $1\n"      # 位元組:Windows 的文字模式會寫成 CRLF(見 B5)
WC_NO_BEST_FIT_CHARS = 0x400


def _windows_codepage_numbers():
    """{角色: 碼頁},只供診斷:ANSI、OEM、主控台輸入、主控台輸出(沒有主控台時後兩個是 0)。條件 1 只用 ACP。"""
    import ctypes  # noqa: PLC0415
    k32 = ctypes.windll.kernel32
    return {"ACP": k32.GetACP(), "OEMCP": k32.GetOEMCP(),
            "ConsoleCP": k32.GetConsoleCP(), "ConsoleOutputCP": k32.GetConsoleOutputCP()}


def _windows_acp(k32=None):
    """真正的 ANSI 碼頁:GetACP()。k32 可以換成替身(測試用)。"""
    if k32 is None:
        import ctypes  # noqa: PLC0415
        k32 = ctypes.windll.kernel32
    return k32.GetACP()


def _codec_lossless(text, cp):
    """Python 的嚴格編碼:True = 編得出而且解碼還原相同;False = 編不出或還原不同;None = 不認得這個碼頁。"""
    if not cp:
        return None
    try:
        name = codecs.lookup(f"cp{cp}").name
    except LookupError:
        return None
    try:
        return text.encode(name).decode(name) == text
    except UnicodeError:
        return False


def _wide_char_to_multibyte(text, cp, flags):
    """Windows 的 WideCharToMultiByte:(位元組, 是否用了替代字元);不是 Windows 或呼叫失敗 → None。"""
    if not WINDOWS or not text:
        return None
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415
    f = ctypes.WinDLL("kernel32", use_last_error=True).WideCharToMultiByte   # 自己的實例:不動共用的 argtypes
    f.argtypes = [wintypes.UINT, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_int,
                  ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(wintypes.BOOL)]
    f.restype = ctypes.c_int
    wide = len(text.encode("utf-16-le")) // 2
    used = wintypes.BOOL(False)
    size = f(cp, flags, text, wide, None, 0, None, ctypes.byref(used))
    if size <= 0:
        return None
    buf = ctypes.create_string_buffer(size)
    used = wintypes.BOOL(False)
    if f(cp, flags, text, wide, buf, size, None, ctypes.byref(used)) != size:
        return None
    return buf.raw[:size], bool(used.value)


def _winapi_lossless(text, cp):
    """Windows 自己的轉換,不接受 best-fit:True = 沒用替代字元;False = 用了;None = 問不到。
    UTF-8(65001)等碼頁不接受這組參數,呼叫失敗 → None。"""
    r = _wide_char_to_multibyte(text, cp, WC_NO_BEST_FIT_CHARS)
    return None if r is None else not r[1]


def _acp_cannot_encode(text, acp, winapi_lossless=_winapi_lossless):
    """條件 1:兩邊都明確說「編不出」才成立;None(不確定)或任一邊說編得出 → 不成立。"""
    return _codec_lossless(text, acp) is False and winapi_lossless(text, acp) is False


CODEPAGE_CANNOT_ENCODE_CJK_PATH = WINDOWS and _acp_cannot_encode(CJK_DIR + "\\" + CJK_SCRIPT, _windows_acp())


class ShellcheckCodepageLimit(Exception):
    """只有這個例外會被算成已知限制;它只在 _is_shellcheck_codepage_crash 成立時丟出。"""


def _is_shellcheck_codepage_crash(reason):
    return "[shell] shellcheck" in reason and SHELLCHECK_HPUTCHAR in reason and "[SC" not in reason


def _shellcheck_diag(r, script):
    """完整的診斷:hook 的整段原因、hook 的原始輸出,以及直接跑一次 shellcheck 的原始位元組(不經過 hook 的摘要)。"""
    sc = shutil.which("shellcheck")
    direct = subprocess.run([sc, "-f", "gcc", str(script)], cwd=str(script.parent), capture_output=True) if sc else None
    if WINDOWS:
        acp, rel = _windows_acp(), CJK_DIR + "\\" + CJK_SCRIPT
        cps = (f"碼頁(只供診斷):{_windows_codepage_numbers()};條件 1 用 ACP {acp}:"
               f"測試路徑 Python 嚴格編碼可無損={_codec_lossless(rel, acp)}、"
               f"WideCharToMultiByte(不接受 best-fit)可無損={_winapi_lossless(rel, acp)};"
               f"完整路徑 {_codec_lossless(str(script), acp)}/{_winapi_lossless(str(script), acp)}")
    else:
        cps = "碼頁:(非 Windows)"
    parts = [f"{cps};已知限制條件 1:{CODEPAGE_CANNOT_ENCODE_CJK_PATH}",
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


CJK_REL_PATH = CJK_DIR + "\\" + CJK_SCRIPT


class _RunnerKernel32:
    """第三輪 CI 的 Windows runner 實測的四個碼頁。"""
    GetACP = staticmethod(lambda: 1252)
    GetOEMCP = staticmethod(lambda: 437)
    GetConsoleCP = staticmethod(lambda: 65001)
    GetConsoleOutputCP = staticmethod(lambda: 65001)


def test_condition_1_reads_the_ansi_code_page_not_the_console_one():
    # 那台 runner 的主控台碼頁編得出路徑、ACP 編不出:讀成主控台碼頁(chcp 回報的那個)條件 1 就不成立
    acp = _windows_acp(_RunnerKernel32)
    assert acp == 1252
    assert _acp_cannot_encode(CJK_REL_PATH, acp, lambda text, cp: False)
    assert not _acp_cannot_encode(CJK_REL_PATH, _RunnerKernel32.GetConsoleCP(), lambda text, cp: False)


@pytest.mark.parametrize("acp, winapi, expected", [
    (1252, False, True),     # 第三輪 CI 的 runner:兩邊都說編不出 → 條件 1 成立
    (1252, True, False),     # 兩邊不一致 → 不套用
    (1252, None, False),     # 問不到 Windows → 不套用
    (950, False, False),     # ACP 能表示這個路徑(繁中 Windows)→ 不套用,即使另一邊說編不出
    (950, True, False),
    (936, True, False),
    (65001, None, False),    # ACP 是 UTF-8:API 不接受不用 best-fit 的參數(None)、Python 編得出 → 不套用
    (99999, False, False),   # 不認得的碼頁 → 不套用
    (0, False, False),
], ids=["cp1252-both-cannot", "cp1252-winapi-can", "cp1252-winapi-unknown", "cp950-winapi-cannot",
        "cp950", "cp936", "utf8-acp", "unknown-codepage", "zero"])
def test_condition_1_needs_both_checks_to_say_the_ansi_code_page_cannot_encode(acp, winapi, expected):
    assert _acp_cannot_encode(CJK_REL_PATH, acp, lambda text, cp: winapi) is expected


def test_codec_check_is_strict_and_round_trips():
    assert _codec_lossless(CJK_REL_PATH, 1252) is False
    assert _codec_lossless("ā", 1252) is False             # Windows 的 best-fit 會換成 a;嚴格編碼不接受
    assert _codec_lossless("shell scripts\\deploy.sh", 1252) is True
    assert _codec_lossless(CJK_REL_PATH, 950) is True
    assert _codec_lossless("¢", 950) is False              # 編得出,但解碼回來是全形的 ￠:不算無損
    assert _codec_lossless(CJK_REL_PATH, 99999) is None


@pytest.mark.skipif(not WINDOWS, reason="Windows 專屬(在其他平台記為未驗證)")
def test_winapi_check_rejects_best_fit_substitutes():
    assert _winapi_lossless("shell scripts\\deploy.sh", 1252) is True
    assert _winapi_lossless(CJK_REL_PATH, 1252) is False
    assert _winapi_lossless("ā", 1252) is False
    # 沒有 WC_NO_BEST_FIT_CHARS 時 Windows 把 ā 換成 a,而且不算用了替代字元:只看替代字元旗標會被騙
    assert _wide_char_to_multibyte("ā", 1252, 0) == (b"a", False)
    assert _winapi_lossless(CJK_REL_PATH, 65001) is None    # UTF-8 不接受這組參數:問不到,不是「編不出」


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
                   reason="已知限制(B4):Windows 的 ANSI 碼頁(GetACP)無法無損編碼路徑時,"
                          "經由 hook 執行的 shellcheck 寫輸出就崩潰(hPutChar)")
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


def test_shellcheck_known_limit_marker_stays_strict_and_narrow():
    marks = {m.name: m for m in test_shellcheck_with_cjk_path_reports_location.pytestmark}
    x = marks["xfail"].kwargs
    assert x["strict"] is True                               # 意外通過 → XPASS(strict) → 失敗
    assert x["raises"] is ShellcheckCodepageLimit            # 只有條件 2 成立時丟的例外算已知限制
    assert x["condition"] is CODEPAGE_CANNOT_ENCODE_CJK_PATH
    # 兩邊都是布林值時 is 分不出「換成別的條件」,所以也比對裝飾器的原始碼
    assert "@pytest.mark.xfail(condition=CODEPAGE_CANNOT_ENCODE_CJK_PATH, strict=True," \
        in inspect.getsource(test_shellcheck_with_cjk_path_reports_location)
    assert x["reason"].startswith("已知限制")
    assert "skipif" in marks
    assert WINDOWS or CODEPAGE_CANNOT_ENCODE_CJK_PATH is False
