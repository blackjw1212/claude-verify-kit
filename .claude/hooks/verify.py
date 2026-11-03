#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop-hook verifier v3 — 跨產物類型的「可驗證成功標準」閘門。

Claude 宣告完成(Stop 事件)時執行;驗證失敗就擋下,讓 Claude 繼續修正。
依專案內實際存在的檔案自動派發對應檢查(韌體、OpenCore、ACPI、XML、JSON、
AdGuard、xlsx、shell、Python、Node、Rust)。

優先序:
  1) .claude/skip-verify 存在            → 放行(硬體專屬改動 / WIP 用)。
  2) 防迴圈:stop_hook_active 為 true,且上次擋下之後專案沒有任何新修改
     → 放行並告知使用者(不重跑、不再擋)。有新修改才重新驗證。
  3) .claude/verify(.sh/.py/.cmd/.bat)  → 只跑它,exit code 即裁決。
  4) 否則                                → 跑所有「適用」的內建檢查並聚合結果。

只驗變更的部分(Stage 2):
  - 變更以「檔案內容雜湊」判定(.claude/.verify_cache.json),不看 transcript 或 git:
    前者看不到 Bash 改的檔、後者看不到 .gitignore 掉但仍要驗的檔。只 touch 不算變更。
  - 逐檔驗證器(JSON/XML/plist/ACPI/xlsx/shellcheck/Python 語法)只驗內容變了的檔案,
    其餘沿用上次的結果 —— 包括上次的失敗(沒修好的檔照樣擋)。
  - 專案層級驗證器(建置/測試/自訂驗證)在「它的輸入」變了才重跑,只快取通過:
      韌體  原始碼/標頭/建置設定/宣告嵌入的檔案;遇到 file(GLOB)、extra_scripts 等
            看不出輸入的寫法就改成任何變更都重建;任何檔案被刪除也重建
      測試、Node、Rust、aglint、自訂驗證  任何檔案變更都重跑(它們可能讀任何檔)
  - 彼此獨立的驗證器平行執行:最多 max_parallel 個,其中重型最多 max_heavy 個。

每個結果是四種狀態之一,處置不同(.claude/verify.json 可調):
  fail     真正的驗證失敗           → 擋下
  missing  工具不存在               → 預設只警告(on_missing: warn|block|ignore)
  timeout  逾時,記錄為疑點         → 預設擋下(on_timeout: block|warn|ignore)
                                     逾時 = 沒驗完,絕不算通過;要放寬必須明確設定
  error    執行錯誤 / hook 內部錯誤 → 只警告(fail-open,但使用者看得到)

協定依據 https://code.claude.com/docs/en/hooks (2026-11 查核):
  - Stop 輸入含 stop_hook_active;Claude Code 另有「連續 8 次續跑」上限
    (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP),本 hook 自己的上限要比它低才有意義。
  - 擋下:exit 0 + {"decision":"block","reason":...}(預設 block_mode=json),
    或 exit 2 + stderr(block_mode=exit2)。
  - systemMessage 顯示給使用者;exit 0 時 stderr 只進 debug log,誰都看不到。
  - hook 逾時會被取消且輸出作廢 = 靜默放行,所以全部檢查共用一個時間預算
    (budget_seconds,預設 540,低於 settings.json 的 600)。

放置:~/.claude/hooks/verify.py 或 <repo>/.claude/hooks/verify.py
需求:Python 3.8+,只用標準函式庫(xlsx 檢查需 openpyxl,缺了只警告)。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import locale
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.parsers.expat
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VERSION = "3.0.0-dev.1"
IS_WIN = os.name == "nt"

# Windows 主控台 Python 的 stdio 預設 cp950(Big5),Claude Code 以 UTF-8 讀寫。
# stdout 另外一律輸出 ASCII-only JSON(ensure_ascii),編碼怎麼錯都不會壞;
# stderr 在 block_mode=exit2 時就是 reason 本身,必須是 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 走訪時整棵剪掉的目錄。必須含虛擬環境與工具快取,否則掃進 .venv / node_modules
# 會讓 Stop hook 逾時而非回報結果。target/ 同理(Rust 建置產物)。
SKIP_DIRS = {".git", "build", ".pio", "node_modules", ".claude", ".vscode",
             "managed_components", "dist", ".cache", "__pycache__",
             ".venv", "venv", "site-packages", ".tox", "target",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", "coverage"}

# 這些檔名「規格上就允許註解/尾逗號」(JSONC)。嚴格 JSON 解析失敗時,
# 只有它們會再用 JSONC 規則重試;一般 .json 有註解仍然算壞掉。
# 每一項都有出處(2026-11-03 查核),沒有出處的不收 —— 放寬錯了就是漏擋:
#   tsconfig/jsconfig  tsc 6.0.2 實測:--init 產生的檔案自帶註解,含註解+尾逗號的 -p 可編譯
#   devcontainer       devcontainers/spec:「JSON with Comments file called devcontainer.json」
#   .eslintrc.json     ESLint v8 文件:「ESLint's JSON files also allow JavaScript-style comments」
#   .babelrc.json      Babel 文件:以 JSON5 解析(本檢查只處理註解與尾逗號,不是完整 JSON5)
#   api-extractor.json 官方範本(rushstack)本身含註解
#   tslint.json        palantir/tslint 讀檔時先 stripComments
# 刻意不收:deno.json、turbo.json(官方文件都說要註解請改用 .jsonc);
#           biome.json(查不到官方說法)。
JSONC_NAMES = ("tsconfig*.json", "jsconfig*.json", "devcontainer.json",
               ".devcontainer.json", ".eslintrc.json", ".babelrc.json",
               "api-extractor.json", "tslint.json")

CONFIG_NAME = "verify.json"
STATE_NAME = ".verify_state.json"
LOG_NAME = ".verify.log"
LOG_MAX_BYTES = 256 * 1024

DEFAULT_CONFIG = {
    "max_lines": 8,                 # 每項失敗最多顯示幾行
    "max_reason_chars": 4000,       # 整段 reason 上限(Claude Code 對 hook 字串上限 10,000)
    "budget_seconds": 540,          # 全部檢查共用的時間預算;須低於 hook 的 timeout
    "block_mode": "json",           # json | exit2
    "rerun_when_active": True,      # False = 舊行為:stop_hook_active 一律放行
    "max_consecutive_blocks": 5,    # 同一輪連續擋幾次後放行;0 = 只靠 Claude Code 的上限
    "on_missing": "warn",           # warn | block | ignore
    "on_timeout": "block",          # block | warn | ignore — 逾時不算通過,預設擋
    "max_parallel": min(4, os.cpu_count() or 1),  # 同時跑幾個驗證器;1 = 完全依序
    "max_heavy": 1,                 # 其中重型(韌體建置、測試)最多幾個
    "cache": True,                  # False = 每次都驗全部(不沿用快取結果)
    "override_cache": True,         # False = 自訂驗證每次都跑(Stage 1 行為)
    "trust_mtime": True,            # False = 每次重新雜湊所有檔案(最慢、最保守)
    "debug": False,                 # 或環境變數 CLAUDE_VERIFY_DEBUG=1:把跳過/沿用的理由寫進紀錄
    "validators": {},               # {"<id>": {"timeout": 秒, "on_missing": ..., "on_timeout": ...}}
}

# 各驗證器的預設逾時(秒)。實際上限 = min(這個值, 剩餘預算)。
DEFAULT_TIMEOUTS = {
    "override": 540, "firmware": 480, "rust": 480, "node": 300, "pytest": 300,
    "python": 120, "adblock": 120, "xlsx": 120, "shell": 60, "plist": 60,
    "opencore": 60, "acpi": 60, "xml": 60, "json": 60,
}
POLICIES = ("warn", "block", "ignore")
BOOL_KEYS = ("rerun_when_active", "cache", "override_cache", "trust_mtime", "debug")


def is_debug(cfg):
    return bool(cfg.get("debug")) or os.environ.get("CLAUDE_VERIFY_DEBUG", "") not in ("", "0")


# ---------------------------------------------------------------- 小工具

def warn(msg):
    """寫進 stderr = 只進 Claude Code 的 debug log。要讓人看到請放 systemMessage。"""
    try:
        print(f"[verify] {msg}", file=sys.stderr)
    except Exception:
        pass


def clip(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def _windows_encodings():
    encs = []
    try:
        import ctypes  # noqa: PLC0415
        encs.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")
        encs.append(f"cp{ctypes.windll.kernel32.GetACP()}")
    except Exception:
        pass
    try:
        encs.append(locale.getpreferredencoding(False))
    except Exception:
        pass
    return tuple(dict.fromkeys(e for e in encs if e))


def decode_output(data, encodings=None):
    """子程序輸出的位元組 → 字串。先試 UTF-8,Windows 再試 OEM / ANSI 碼頁。

    寫死 UTF-8 + replace 的話,cp950 的中文錯誤訊息會變成一整片 �,
    模型讀不懂就修不動。
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if encodings is None:
        encodings = ("utf-8",) + (_windows_encodings() if IS_WIN else ())
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------- 設定

def _valid_policy(v):
    return v in POLICIES


def load_config(project, notes):
    cfg = dict(DEFAULT_CONFIG)
    cfg["validators"] = {}
    path = project / ".claude" / CONFIG_NAME
    if not path.exists():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        notes.append(f".claude/{CONFIG_NAME} 無法解析({e}),已改用預設值")
        return cfg
    if not isinstance(raw, dict):
        notes.append(f".claude/{CONFIG_NAME} 頂層必須是物件,已改用預設值")
        return cfg

    def bad(key, why):
        notes.append(f".claude/{CONFIG_NAME}:{key} {why},已忽略")

    for key, val in raw.items():
        if key.startswith("$") or key.startswith("_"):
            continue
        if key not in DEFAULT_CONFIG:
            bad(key, "不是已知設定")
            continue
        if key in ("on_missing", "on_timeout"):
            if not _valid_policy(val):
                bad(key, "必須是 warn / block / ignore")
                continue
        elif key == "block_mode":
            if val not in ("json", "exit2"):
                bad(key, "必須是 json / exit2")
                continue
        elif key in BOOL_KEYS:
            if not isinstance(val, bool):
                bad(key, "必須是 true / false")
                continue
        elif key == "validators":
            if not isinstance(val, dict):
                bad(key, "必須是物件")
                continue
            clean = {}
            for vid, opts in val.items():
                if not isinstance(opts, dict):
                    bad(f"validators.{vid}", "必須是物件")
                    continue
                o = {}
                for k2, v2 in opts.items():
                    if k2 == "timeout" and isinstance(v2, (int, float)) and not isinstance(v2, bool) and v2 > 0:
                        o[k2] = float(v2)
                    elif k2 in ("on_missing", "on_timeout") and _valid_policy(v2):
                        o[k2] = v2
                    else:
                        bad(f"validators.{vid}.{k2}", "名稱或值不合法")
                clean[vid] = o
            val = clean
        else:  # 數值
            minimum = {"max_lines": 1, "max_reason_chars": 500, "budget_seconds": 1,
                       "max_consecutive_blocks": 0, "max_parallel": 1, "max_heavy": 1}[key]
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val < minimum:
                bad(key, f"必須是 ≥ {minimum} 的數字")
                continue
            if key != "budget_seconds":
                val = int(val)
        cfg[key] = val
    return cfg


def vopt(cfg, vid, key):
    opts = cfg["validators"].get(vid) or {}
    if key in opts:
        return opts[key]
    if key == "timeout":
        return float(DEFAULT_TIMEOUTS.get(vid, 120))
    return cfg[key]


# ---------------------------------------------------------------- 執行外部工具

def _cmd_quote(arg):
    if arg and not re.search(r'[\s&|<>^(),;=!\'`]', arg):
        return arg
    return f'"{arg}"'


def resolve_argv(argv, is_win=None, which=None):
    """把 argv 轉成可直接 spawn 的形式,不經過 shell=True。

    回傳 (spawn, None) 或 (None, ("missing"|"error", 說明))。
    spawn 通常是 list;Windows 的 .cmd/.bat 會回傳一整條字串:
      - CreateProcess 不能直接執行 .cmd/.bat(npm、npx、aglint 都是這種包裝),
        舊版用裸名稱呼叫 → FileNotFoundError → 被當成「工具缺失」靜默放行。
      - list 形式會被 list2cmdline 用 MSVC 規則加反斜線跳脫引號,cmd.exe 讀不懂,
        所以要自己組字串:"cmd.exe" /d /s /c ""C:\\x\\npm.cmd" test"。
      - cmd.exe 在雙引號內仍會展開 %VAR%,而且無法可靠跳脫(BatBadBut 類問題);
        路徑或參數含 % 或 " 時直接拒絕,不冒險。
    """
    is_win = IS_WIN if is_win is None else is_win
    which = which or shutil.which
    exe = str(argv[0])
    has_sep = ("/" in exe) or ("\\" in exe)
    path = exe if (has_sep and os.path.isfile(exe)) else (None if has_sep else which(exe))
    if not path:
        return None, ("missing", f"找不到 {exe}")
    low = path.lower()
    rest = [str(a) for a in argv[1:]]
    if is_win and low.endswith((".cmd", ".bat")):
        for a in [path] + rest:
            if any(c in a for c in '"%\r\n'):
                return None, ("error", f"路徑或參數含 cmd.exe 無法安全跳脫的字元:{a!r}")
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        inner = " ".join(_cmd_quote(x) for x in [path] + rest)
        return f'"{comspec}" /d /s /c "{inner}"', None
    if low.endswith(".py") and (is_win or not os.access(path, os.X_OK)):
        return [sys.executable, path] + rest, None
    return [path] + rest, None


def _kill_tree(proc):
    """逾時要殺整棵程序樹:只殺直接子程序的話,孫程序(npm→node、idf.py→ninja)
    會留著繼續吃 CPU。輸出寫到暫存檔而非 pipe,所以孫程序不會讓我們卡在讀取。"""
    try:
        if IS_WIN:
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
            subprocess.run([taskkill, "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception as e:
        warn(f"結束逾時程序失敗:{e}")
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _read_capped(fh, head=64 * 1024, tail=512 * 1024):
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    fh.seek(0)
    if size <= head + tail:
        return fh.read()
    first = fh.read(head)
    fh.seek(size - tail)
    return first + b"\n...[output truncated]...\n" + fh.read()


def run_tool(argv, cwd, timeout, env=None, stdin_bytes=None):
    """→ (status, rc, text, seconds);status ∈ ok / fail / missing / timeout / error。"""
    if timeout <= 0:
        return "timeout", None, "整體時間預算已用盡,未執行", 0.0
    spawn, why = resolve_argv(argv)
    if spawn is None:
        return why[0], None, why[1], 0.0
    out = tempfile.TemporaryFile()
    inp = subprocess.DEVNULL  # 不讓工具等鍵盤輸入而卡到逾時
    if stdin_bytes is not None:
        inp = tempfile.TemporaryFile()
        inp.write(stdin_bytes)
        inp.seek(0)
    kwargs = dict(cwd=cwd, stdin=inp, stdout=out, stderr=subprocess.STDOUT)
    if env is not None:
        kwargs["env"] = env
    if IS_WIN:
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(spawn, **kwargs)
    except FileNotFoundError:
        out.close()
        return "missing", None, f"找不到 {argv[0]}", 0.0
    except OSError as e:
        out.close()
        return "error", None, f"無法執行 {argv[0]}:{e}", 0.0
    try:
        rc = proc.wait(timeout=timeout)
        status = "ok" if rc == 0 else "fail"
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        rc, status = None, "timeout"
    dur = time.monotonic() - t0
    try:
        text = decode_output(_read_capped(out))
    finally:
        out.close()
        if stdin_bytes is not None:
            inp.close()
    if status == "timeout":
        text = f"逾時(>{timeout:.0f}s),已終止整個程序樹\n" + text
    return status, rc, text, dur


# ---------------------------------------------------------------- 摘要錯誤輸出

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
LOC_RES = (
    re.compile(r"(?:^|[\s(\[\"'])(?:[A-Za-z]:)?[\w.\-/\\~]*\.[A-Za-z0-9_]{1,10}:\d+"),  # path:line[:col]
    re.compile(r'File "[^"]+", line \d+'),                                          # Python traceback
    re.compile(r"[\w.\-/\\]+\.[A-Za-z0-9_]{1,10}\(\d+(?:,\d+)?\)"),                 # MSVC / tsc
    re.compile(r"^\s*-->\s+\S+:\d+"),                                               # rustc
    re.compile(r"^\S+\.(?:dsl|asl)\s+\d+:"),                                        # iasl
    re.compile(r"\bline \d+(?:, column \d+)?", re.I),                               # 各種「line N」
)
ERR_RE = re.compile(
    r"\b(?:error|errors|failed|failure|fatal|exception|traceback|assertion\w*|assert|panicked|syntaxerror)\b"
    r"|失敗|錯誤|❌",
    re.I,
)
MARKER_RE = re.compile(
    r"^\s*(?:not ok\b|FAILED\b|FAIL\b|E\s{2,}|✗|✖|×|error(?:\[\w+\])?:"
    r"|[A-Za-z_.]*(?:Error|Exception)(?:\[\w+\])?:)")  # ValueError: … / Error: Cannot find module …
STACK_RE = re.compile(r"^\s*(?:at\s|\S+\s\()|^\s*File \"")  # 堆疊框:有用但次於失敗點本身
TAP_COMMENT_RE = re.compile(r"^\s*#\s")                     # TAP 診斷行,多半是測試自己印的 log
TAP_ERROR_RE = re.compile(r"^\s*error:\s*[|>]-?\s*$")       # node --test 的 YAML:錯誤內容在下一行
NOISE_RE = re.compile(r"\b0 (?:errors?|failed|failures?|problems?)\b", re.I)
BANNER_RE = re.compile(r"^[=_\-*#~]{3,}\s*|\s*[=_\-*#~]{3,}$")
HEADER_ONLY_RE = re.compile(r"^(?:errors?|failures?|short test summary info|warnings summary|traceback:?)$", re.I)
ABS_PATH_RE = re.compile(r"(?:^|[\s(])(?:/|[A-Za-z]:[\\/]|node:)")


def relativize(text, project):
    root = str(project)
    variants = {root + os.sep, root + "/"}
    if IS_WIN:
        variants.add(root.replace("\\", "/") + "/")
    for v in sorted(variants, key=len, reverse=True):
        flags = re.I if IS_WIN else 0
        text = re.sub(r"file:///?" + re.escape(v.lstrip("/")), "", text, flags=flags)
        text = re.sub(re.escape(v), "", text, flags=flags)
    return text


def _rank(ln):
    """越小越重要;None = 不挑。"""
    has_loc = any(r.search(ln) for r in LOC_RES)
    if TAP_COMMENT_RE.match(ln):
        return 1 if re.match(r"^\s*#\s*(?:fail|failed)\s+[1-9]", ln, re.I) else 4
    if MARKER_RE.match(ln):
        return 0
    if has_loc:
        if ABS_PATH_RE.search(ln):
            return 3          # 專案外的路徑:標準庫 / node 內部堆疊
        return 1 if STACK_RE.match(ln) else 0
    if ERR_RE.search(ln):
        return 2
    return None


def summarize(text, max_lines, project=None):
    """從工具輸出挑出最有用的幾行,不整包貼 log。
    優先序:失敗標記與專案內「檔案:行號」> 堆疊框 > 錯誤關鍵字 > 專案外路徑 > TAP 註解。
    都沒有就取最後幾行。"""
    text = ANSI_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")
    if project is not None:
        text = relativize(text, project)
    lines = []
    for ln in text.split("\n"):
        ln = BANNER_RE.sub("", ln.rstrip())  # "==== ERRORS ====" 之類的裝飾
        if ln.strip() and not HEADER_ONLY_RE.match(ln.strip()):
            lines.append(ln)
    scored = []
    seen = set()
    detail = 0
    for i, ln in enumerate(lines):
        if TAP_ERROR_RE.match(ln):
            detail = 2  # 接下來兩行是錯誤內容本身
            continue
        rank = 0 if detail else _rank(ln)
        detail = max(0, detail - 1)
        if rank is None or ln in seen or NOISE_RE.search(ln):
            continue
        seen.add(ln)
        scored.append((rank, i, ln))
    if scored:
        keep = sorted(sorted(scored)[:max_lines], key=lambda t: t[1])
        out = [clip(t[2], 220) for t in keep]
        if len(scored) > max_lines:
            out.append(f"…另 {len(scored) - max_lines} 行省略")
        return out
    out = [clip(ln, 220) for ln in lines[-max_lines:]]
    if len(lines) > max_lines:
        out.insert(0, f"…前 {len(lines) - max_lines} 行省略")
    return out


# ---------------------------------------------------------------- 結果與執行環境

class Result:
    __slots__ = ("vid", "label", "status", "lines", "rc", "duration", "meta")

    def __init__(self, vid, label, status, lines=None, rc=None, duration=0.0, meta=None):
        self.vid, self.label, self.status = vid, label, status
        self.lines = lines or []
        self.rc, self.duration = rc, duration
        self.meta = meta or {}

    def as_log(self):
        rec = {"id": self.vid, "status": self.status, "rc": self.rc,
               "sec": round(self.duration, 2)}
        rec.update(self.meta)
        return rec


# 逾時的成因與處置,寫進 .verify.log 與訊息,讓人分得出「工具卡住」還是「預算不夠」。
TIMEOUT_CAUSES = {
    "validator_timeout": "超過此驗證器的上限",
    "budget_limited": "超過整體時間預算剩下的時間",
    "budget_exhausted": "整體時間預算已用盡,沒有執行",
}


def timeout_result(vid, label, limit, dur, cause, cmd=""):
    started = cause != "budget_exhausted"
    action = "killed_process_tree" if started else "not_started"
    line = (f"執行 {dur:.0f}s 後逾時(上限 {limit:.0f}s,{TIMEOUT_CAUSES[cause]}),已終止整個程序樹"
            if started else TIMEOUT_CAUSES[cause])
    return Result(vid, label, "timeout", [line + ";結果未判定,不算通過"], duration=dur,
                  meta={"cmd": cmd, "limit": round(limit, 1), "cause": cause, "action": action})


def _display_cmd(argv):
    return " ".join("<inline script>" if "\n" in str(a) else str(a) for a in argv)[:300]


class Ctx:
    def __init__(self, project, cfg, t0):
        self.project = project
        self.cfg = cfg
        self.deadline = t0 + float(cfg["budget_seconds"])
        self._files = None
        self._manifest = None
        self.debug = is_debug(cfg)

    @property
    def manifest(self):
        if self._manifest is None:  # 沒有上次紀錄 → 全部視為新檔(只在直接呼叫檢查函式時發生)
            self._manifest = Manifest(self.project).scan(self.files() + claude_extras(self.project))
        return self._manifest

    @manifest.setter
    def manifest(self, value):
        self._manifest = value

    # ---- 時間預算
    def remaining(self):
        return self.deadline - time.monotonic()

    def timeout_for(self, vid):
        return max(0.0, min(float(vopt(self.cfg, vid, "timeout")), self.remaining() - 3.0))

    # ---- 檔案(整個專案只走訪一次;舊版每個檢查各 rglob 一次,而且 rglob 會先
    #      鑽進 node_modules 才被過濾掉)
    def files(self):
        if self._files is None:
            self._files = walk_files(self.project)
        return self._files

    def reset_files(self):
        self._files = None

    def glob_rel(self, *patterns):
        return [rel for rel in self.files()
                if any(fnmatch.fnmatch(rel.rsplit("/", 1)[-1], p) for p in patterns)]

    def glob(self, *patterns):
        return [self.project / rel for rel in self.glob_rel(*patterns)]

    def rel(self, path):
        try:
            return Path(path).relative_to(self.project).as_posix()
        except ValueError:
            return str(path)

    # ---- 執行外部工具並轉成 Result
    def budget_cause(self, vid, limit):
        if limit <= 0:
            return "budget_exhausted"
        return "budget_limited" if limit < float(vopt(self.cfg, vid, "timeout")) else "validator_timeout"

    def tool_raw(self, vid, argv, cwd=None, env=None, stdin_bytes=None):
        """→ (status, rc, 原始輸出, 秒數, 本次上限)。給需要自己解析輸出的逐檔驗證器用。"""
        limit = self.timeout_for(vid)
        status, rc, text, dur = run_tool(argv, str(cwd or self.project), limit,
                                         env=env, stdin_bytes=stdin_bytes)
        return status, rc, text, dur, limit

    def result_from(self, vid, label, status, rc, text, dur, limit, argv):
        if status == "ok":
            return Result(vid, label, "pass", rc=rc, duration=dur)
        if status == "fail":
            return Result(vid, label, "fail", summarize(text, self.cfg["max_lines"], self.project), rc, dur)
        if status == "missing":
            return Result(vid, label, "missing", [f"{text}(不在 PATH)"], duration=dur)
        if status == "timeout":
            return timeout_result(vid, label, limit, dur, self.budget_cause(vid, limit), _display_cmd(argv))
        return Result(vid, label, "error", [clip(text, 220)], duration=dur)

    def tool(self, vid, label, argv, cwd=None, env=None, stdin_bytes=None,
             fail_detect=None):
        status, rc, text, dur, limit = self.tool_raw(vid, argv, cwd, env, stdin_bytes)
        # fail_detect 只能在「跑完了」的兩種結果之間改判;逾時/缺工具/錯誤不可能被改成通過。
        if status in ("ok", "fail") and fail_detect is not None:
            status = "fail" if fail_detect(rc, text) else "ok"
        return self.result_from(vid, label, status, rc, text, dur, limit, argv)

    def budget_stop(self, vid, label, t0):
        """程序內的檢查(JSON/XML/xlsx)在檔案之間發現預算用盡。"""
        r = timeout_result(vid, label, 0.0, time.monotonic() - t0, "budget_exhausted")
        r.lines = ["整體時間預算用盡,其餘檔案沒有檢查;結果未判定,不算通過"]
        r.meta["action"] = "stopped_mid_scan"
        return r


def walk_files(project):
    """回傳專案內所有檔案的相對路徑(POSIX 分隔),剪掉 SKIP_DIRS。"""
    out = []
    root = str(project)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for fn in files:
            out.append(prefix + fn)
    out.sort()
    return out


def claude_extras(project):
    """.claude/ 只納入頂層的非隱藏檔與 hooks/(設定與自訂驗證腳本改了要重驗)。
    隱藏的狀態檔(本 hook 與 loop-gate 寫的)不納入,否則寫狀態就會被當成變更。"""
    out = []
    cdir = project / ".claude"
    try:
        if cdir.is_dir():
            for p in sorted(cdir.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    out.append(".claude/" + p.name)
            hooks = cdir / "hooks"
            if hooks.is_dir():
                for p in sorted(hooks.iterdir()):
                    if p.is_file() and not p.name.startswith("."):
                        out.append(".claude/hooks/" + p.name)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 變更偵測:內容雜湊清單
#
# 變更的唯一依據是「檔案內容」,不是 transcript、也不是 git:
#   - transcript 看不到 Bash 改的檔(sed -i、腳本、git checkout),官方文件也說它在 Stop
#     當下可能還沒寫完 → 拿它當來源會漏擋。
#   - git status 列的是「所有未 commit 的變更」而非這一輪的,且看不到被 .gitignore 排除、
#     但驗證器照樣會檢查的檔案 → 一樣會漏。
# 內容雜湊兩者都不會漏:誰改的、怎麼改的都一樣。stat(大小+mtime)沒變的檔案沿用上次的
# 雜湊,不重讀(同 git index);但「上次觀察時才剛被改過」(racy,2 秒內)的一律重讀。

CACHE_NAME = ".verify_cache.json"
CACHE_FORMAT = 1
BIG_FILE = 64 * 1024 * 1024     # 超過就不讀內容,只比大小與 mtime
RACY_NS = 2 * 10 ** 9

# 驗證器邏輯版本:改了某個驗證器的判定方式就 +1,它的舊快取自動失效。
VALIDATOR_VERSIONS = {
    "firmware": 1, "plist": 1, "opencore": 1, "acpi": 1, "xml": 1, "json": 1,
    "adblock": 1, "xlsx": 1, "shell": 1, "python": 1, "pytest": 1, "node": 1,
    "rust": 1, "override": 1,
}
# 重型:會吃滿 CPU 的建置/測試,同時最多跑 max_heavy 個(預設 1),避免互搶到逾時。
HEAVY = {"firmware", "rust", "node", "pytest", "override"}


def _hash_file(path, size, mtime_ns):
    if size > BIG_FILE:
        return f"stat:{size}:{mtime_ns}"
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return f"unreadable:{size}:{mtime_ns}"
    return h.hexdigest()


class Manifest:
    """每個檔案的 [大小, mtime_ns, 觀察時間_ns, 內容雜湊]。"""

    def __init__(self, project, prev=None, trust_mtime=True):
        self.project = project
        self.prev = prev if isinstance(prev, dict) else {}
        self.trust = trust_mtime
        self.entries = {}
        self.added, self.modified, self.deleted = [], [], []
        self.hashed = 0

    def scan(self, rels):
        now = time.time_ns()
        entries, added, modified = {}, [], []
        for rel in rels:
            path = self.project / rel
            try:
                st = os.stat(path)
            except OSError:
                continue
            size, mt = st.st_size, st.st_mtime_ns
            old = self.prev.get(rel)
            valid_old = isinstance(old, list) and len(old) == 4
            if (self.trust and valid_old and old[0] == size and old[1] == mt and old[3]
                    and (old[2] - old[1]) >= RACY_NS):
                h = old[3]
            else:
                h = _hash_file(path, size, mt)
                self.hashed += 1
            entries[rel] = [size, mt, now, h]
            if not valid_old:
                added.append(rel)
            elif old[3] != h:
                modified.append(rel)
        self.entries = entries
        self.added, self.modified = added, modified
        self.deleted = sorted(r for r in self.prev if r not in entries)
        return self

    def hash_of(self, rel):
        e = self.entries.get(rel)
        return e[3] if e else None

    def digest(self, rels=None):
        h = hashlib.sha256(VERSION.encode())
        names = sorted(self.entries) if rels is None else sorted(rels)
        for rel in names:
            e = self.entries.get(rel)
            h.update(f"{rel}\0{e[3] if e else '-'}\n".encode("utf-8", "surrogateescape"))
        return h.hexdigest()

    def changes(self, limit=20):
        return {"added": len(self.added), "modified": len(self.modified),
                "deleted": len(self.deleted), "hashed": self.hashed,
                "sample": (self.modified + self.added + self.deleted)[:limit]}


def fingerprint(manifest):
    """專案狀態指紋 = 全部檔案的(路徑, 內容雜湊)。增、刪、改都會變;只 touch 不會。"""
    return manifest.digest()


def load_cache(project):
    empty = {"manifest": {}, "results": {}}
    try:
        c = json.loads((project / ".claude" / CACHE_NAME).read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(c, dict) or c.get("format") != CACHE_FORMAT:
        return empty
    man = c.get("manifest") if isinstance(c.get("manifest"), dict) else {}
    # kit 版本變了 → 驗證結果全部作廢(判定邏輯可能不同);檔案雜湊仍可沿用。
    res = c.get("results") if c.get("kit") == VERSION and isinstance(c.get("results"), dict) else {}
    return {"manifest": man, "results": res}


def save_cache(project, manifest, results):
    path = project / ".claude" / CACHE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"format": CACHE_FORMAT, "kit": VERSION,
                                   "manifest": manifest.entries, "results": results},
                                  ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as e:
        warn(f"無法寫入快取:{e}")


# ---------------------------------------------------------------- 工具與環境識別(快取鍵的一部分)

def exe_identity(name):
    """工具的實際路徑 + 大小 + mtime:升級工具 → 識別改變 → 舊結果作廢。找不到回 None。"""
    name = str(name)
    has_sep = ("/" in name) or ("\\" in name)
    path = name if (has_sep and os.path.isfile(name)) else (None if has_sep else shutil.which(name))
    if not path:
        return None
    try:
        st = os.stat(path)
        return f"{path}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return path


def path_sig(path):
    try:
        st = os.stat(path)
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return "-"


def env_sig(prefixes=(), names=()):
    items = sorted((k, v) for k, v in os.environ.items()
                   if k in names or any(k.startswith(p) for p in prefixes))
    return json.dumps(items, ensure_ascii=False)


def _read_small(path, limit=4096):
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


# ---------------------------------------------------------------- 逐檔驗證器的共用件
# runner(ctx, rels) → (outcomes, extras)
#   outcomes: {rel: ("pass"|"fail", [lines])}  只有「確定歸屬到某個檔案」的結果才進快取
#   extras:   [Result]  缺工具/逾時/錯誤/無法歸屬到檔案的失敗 —— 永遠不進快取

LOC_PREFIX_RE = re.compile(r"^(.+?):(\d+):")


def attribute_lines(text, rels, project, per_file=20):
    """把工具輸出的每一行歸給它開頭寫的那個檔案。回傳 ({rel: [lines]}, 無法歸屬的行數)。"""
    text = relativize(ANSI_RE.sub("", text or ""), project).replace("\r\n", "\n")
    wanted = set(rels)
    by, stray = {}, 0
    for ln in text.split("\n"):
        if not ln.strip():
            continue
        m = LOC_PREFIX_RE.match(ln)
        cand = m.group(1).replace("\\", "/") if m else None
        if cand in wanted:
            lst = by.setdefault(cand, [])
            if len(lst) < per_file:
                lst.append(clip(ln.rstrip(), 220))
        else:
            stray += 1
    return by, stray


def file_results(ctx, vid, label, targets, outcomes, extras, duration=0.0, meta=None):
    lines, nfail = [], 0
    for rel in targets:
        o = outcomes.get(rel)
        if o and o[0] == "fail":
            nfail += 1
            lines.extend(o[1])
    out = []
    n = ctx.cfg["max_lines"]
    if nfail:
        shown = lines[:n] + ([f"…另 {len(lines) - n} 行省略"] if len(lines) > n else [])
        out.append(Result(vid, label, "fail", shown, duration=duration, meta=dict(meta or {})))
    out.extend(extras)
    if not out:
        out.append(Result(vid, label, "pass", duration=duration, meta=dict(meta or {})))
    return out


def _per_file_outcomes(ctx, vid, label, rels, argv_for, cwd_for=None):
    """同一個工具逐檔執行。中途遇到缺工具/逾時/錯誤就停,但已找到的失敗要保留。"""
    outcomes = {}
    for rel in rels:
        path = ctx.project / rel
        r = ctx.tool(vid, label, argv_for(path), cwd=cwd_for(path) if cwd_for else None)
        if r.status in ("missing", "timeout", "error"):
            return outcomes, [r]
        if r.status == "fail":
            outcomes[rel] = ("fail", [f"{rel}:"] + ["  " + ln for ln in r.lines])
        else:
            outcomes[rel] = ("pass", [])
    return outcomes, []


def _per_file(ctx, vid, label, files, argv_for, cwd_for=None):
    """(Stage 1 介面)逐檔執行並聚合成 list[Result]。"""
    rels = [ctx.rel(f) for f in files]
    t0 = time.monotonic()
    outcomes, extras = _per_file_outcomes(ctx, vid, label, rels, argv_for, cwd_for)
    total = time.monotonic() - t0
    for r in extras:
        r.duration = total
    return file_results(ctx, vid, label, rels, outcomes, extras, total)


# ---------------------------------------------------------------- 逐檔驗證器


def run_plist(ctx, rels):
    return _per_file_outcomes(ctx, "plist", "plist 語法(plutil)", rels,
                              lambda f: ["plutil", "-lint", str(f)])


def run_acpi(ctx, rels):
    return _per_file_outcomes(ctx, "acpi", "ACPI/SSDT(iasl)", rels,
                              lambda f: ["iasl", str(f)], cwd_for=lambda f: f.parent)


def run_xml(ctx, rels):
    outcomes, t0 = {}, time.monotonic()
    for rel in rels:
        if ctx.remaining() < 3:
            return outcomes, [ctx.budget_stop("xml", "XML 格式", t0)]
        parser = xml.parsers.expat.ParserCreate()
        try:
            with open(ctx.project / rel, "rb") as fh:
                parser.ParseFile(fh)
            outcomes[rel] = ("pass", [])
        except xml.parsers.expat.ExpatError as e:
            outcomes[rel] = ("fail", [f"{rel}:{e.lineno}:{e.offset}: {xml.parsers.expat.ErrorString(e.code)}"])
        except OSError as e:
            outcomes[rel] = ("fail", [f"{rel}: 無法讀取({e})"])
    return outcomes, []


def strip_jsonc(text):
    """移除 // 與 /* */ 註解及尾逗號(字串內的不動)。只用在 JSONC_NAMES。"""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            seg = text[i: n if j < 0 else j + 2]
            out.append("\n" * seg.count("\n"))  # 保住行號
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _json_one(path, rel):
    try:
        # utf-8-sig 容忍 UTF-8 BOM(Windows 編輯器常見)。
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        return ("fail", [f"{rel}: 無法以 UTF-8 讀取({e})"])
    try:
        json.loads(text)
        return ("pass", [])
    except json.JSONDecodeError as e:
        err = e
    if any(fnmatch.fnmatch(path.name, p) for p in JSONC_NAMES):
        try:
            json.loads(strip_jsonc(text))
            return ("pass", [])
        except json.JSONDecodeError as e2:
            err = e2
    return ("fail", [f"{rel}:{err.lineno}:{err.colno}: {err.msg}"])


def run_json(ctx, rels):
    outcomes, t0 = {}, time.monotonic()
    for rel in rels:
        if ctx.remaining() < 3:
            return outcomes, [ctx.budget_stop("json", "JSON 格式", t0)]
        outcomes[rel] = _json_one(ctx.project / rel, rel)
    return outcomes, []


def run_xlsx(ctx, rels):
    import openpyxl  # noqa: PLC0415  —— 能走到這裡代表工具識別時已確認可 import
    outcomes, t0 = {}, time.monotonic()
    for rel in rels:
        if ctx.remaining() < 3:
            return outcomes, [ctx.budget_stop("xlsx", "Excel xlsx", t0)]
        try:
            openpyxl.load_workbook(str(ctx.project / rel))
            outcomes[rel] = ("pass", [])
        except Exception as e:
            outcomes[rel] = ("fail", [f"{rel}: 損毀/無法載入({clip(str(e), 160)})"])
    return outcomes, []


def _chunks(rels, project, max_chars=6000):
    """命令列長度有上限(Windows 約 32K 字元),檔案多時分批。"""
    batch, size = [], 0
    for rel in rels:
        n = len(str(project / rel)) + 3
        if batch and size + n > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(rel)
        size += n
    if batch:
        yield batch


def run_shell(ctx, rels):
    outcomes, extras = {}, []
    label = "shellcheck"
    for batch in _chunks(rels, ctx.project):
        # -f gcc:每個問題一行 file:line:col: severity: msg [SCxxxx],可以準確歸屬到檔案。
        argv = ["shellcheck", "-f", "gcc"] + [str(ctx.project / r) for r in batch]
        st, rc, text, dur, limit = ctx.tool_raw("shell", argv)
        if st == "ok":
            outcomes.update((r, ("pass", [])) for r in batch)
            continue
        if st != "fail":
            extras.append(ctx.result_from("shell", label, st, rc, text, dur, limit, argv))
            break
        by, stray = attribute_lines(text, batch, ctx.project)
        if rc == 1 and by:  # 1 = 有問題;2 以上 = shellcheck 自己出錯
            for r in batch:
                outcomes[r] = ("fail", by[r]) if r in by else ("pass", [])
        else:
            extras.append(Result("shell", label, "fail", summarize(text, ctx.cfg["max_lines"], ctx.project),
                                 rc, dur))
    return outcomes, extras


# 在「專案的」直譯器裡一次編譯全部檔案:語法以專案的 Python 版本為準,
# 一個子程序搞定(舊版每檔一個 py_compile 子程序),也不在專案裡寫 __pycache__。
_SYNTAX_SCRIPT = r"""
import sys, warnings
warnings.simplefilter("ignore")
bad = 0
for p in sys.stdin.read().split("\0"):
    if not p:
        continue
    try:
        with open(p, "rb") as fh:
            compile(fh.read(), p, "exec", dont_inherit=True)
    except SyntaxError as e:
        bad += 1
        print("%s:%s:%s: %s: %s" % (p, e.lineno or 0, e.offset or 0, type(e).__name__, e.msg))
    except (ValueError, OSError) as e:
        bad += 1
        print("%s:0:0: %s" % (p, e))
sys.exit(1 if bad else 0)
"""


def project_python(project):
    """專案 venv 的直譯器優先;沒有才退回跑這支 hook 的 Python。

    用全域 pytest 跑有 venv 的專案,會看不到專案自己的套件而噴
    ModuleNotFoundError —— 那是假陰性、不是真失敗(實測同一專案:
    全域 8 errors vs venv 105 passed)。
    """
    for vdir in (".venv", "venv"):
        for sub, exe in (("Scripts", "python.exe"), ("bin", "python")):
            p = project / vdir / sub / exe
            if p.exists():
                return str(p)
    return sys.executable


def run_python_syntax(ctx, rels):
    py = project_python(ctx.project)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    payload = "\0".join(str(ctx.project / r) for r in rels).encode("utf-8", "surrogateescape")
    argv = [py, "-c", _SYNTAX_SCRIPT]
    st, rc, text, dur, limit = ctx.tool_raw("python", argv, env=env, stdin_bytes=payload)
    if st == "ok":
        return {r: ("pass", []) for r in rels}, []
    if st != "fail":
        return {}, [ctx.result_from("python", "Python 語法", st, rc, text, dur, limit, argv)]
    by, stray = attribute_lines(text, rels, ctx.project)
    if rc == 1 and by and not stray:
        return {r: (("fail", by[r]) if r in by else ("pass", [])) for r in rels}, []
    return {}, [Result("python", "Python 語法", "fail",
                       summarize(text, ctx.cfg["max_lines"], ctx.project), rc, dur)]


# ---- 逐檔驗證器的工具識別:回傳 (識別字串, None) 或 (None, 缺工具的 Result)

def _exe_tool(vid, label, exe, extra=lambda ctx: ""):
    def ident(ctx):
        i = exe_identity(exe)
        if i is None:
            return None, Result(vid, label, "missing", [f"找不到 {exe}(不在 PATH)"])
        return i + "|" + extra(ctx), None
    return ident


def _xlsx_tool(ctx):
    try:
        import openpyxl  # noqa: PLC0415
        return "openpyxl " + str(getattr(openpyxl, "__version__", "?")), None
    except Exception:
        return None, Result("xlsx", "Excel xlsx", "missing", ["openpyxl 未安裝(pip install openpyxl)"])


def _python_tool(ctx):
    py = project_python(ctx.project)
    return (exe_identity(py) or py), None


def _rc_digest(ctx, *names):
    """設定檔(例如 .shellcheckrc)也會改變結果:把它們的內容雜湊算進工具識別。"""
    rels = [r for r in ctx.files() if r.rsplit("/", 1)[-1] in names]
    return ctx.manifest.digest(rels) if rels else ""


def _asl_digest(ctx):
    """ASL 的 Include 會把 .asl/.asi 拉進來:它們改了,所有 .dsl 都要重編。"""
    rels = [r for r in ctx.files() if r.lower().endswith((".asl", ".asi"))]
    return ctx.manifest.digest(rels) if rels else ""


FILE_VALIDATORS = {
    # vid: (標籤, 檔名樣式, runner, 工具識別)
    "plist": ("plist 語法(plutil)", ("*.plist",), run_plist, _exe_tool("plist", "plist 語法(plutil)", "plutil")),
    "acpi": ("ACPI/SSDT(iasl)", ("*.dsl",), run_acpi, _exe_tool("acpi", "ACPI/SSDT(iasl)", "iasl", _asl_digest)),
    "xml": ("XML 格式", ("*.xml",), run_xml, lambda ctx: ("builtin", None)),
    "json": ("JSON 格式", ("*.json",), run_json, lambda ctx: ("builtin|" + ",".join(JSONC_NAMES), None)),
    "xlsx": ("Excel xlsx", ("*.xlsx",), run_xlsx, _xlsx_tool),
    "shell": ("shellcheck", ("*.sh",), run_shell,
              _exe_tool("shell", "shellcheck", "shellcheck",
                        lambda ctx: _rc_digest(ctx, ".shellcheckrc", "shellcheckrc"))),
    "python": ("Python 語法", ("*.py",), run_python_syntax, _python_tool),
}


def run_file_validator(ctx, vid, rels=None):
    """不經快取,直接驗證全部(或指定的)檔案 → list[Result]。"""
    label, patterns, runner, ident = FILE_VALIDATORS[vid]
    targets = ctx.glob_rel(*patterns) if rels is None else rels
    if not targets:
        return []
    _, missing = ident(ctx)
    if missing is not None:
        return [missing]
    t0 = time.monotonic()
    outcomes, extras = runner(ctx, targets)
    return file_results(ctx, vid, label, targets, outcomes, extras, time.monotonic() - t0)


# ---------------------------------------------------------------- 專案層級驗證器
# spec = dict(vid, label, run=callable→list[Result], inputs=("any"|"precise", rels|None, 原因),
#             env=識別字串, cacheable=bool)
# 只快取「通過」:失敗可能是環境問題(少裝套件),裝好之後檔案沒變也必須重跑。

FW_SOURCE_EXT = (".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".h++",
                 ".ipp", ".tpp", ".inc", ".inl", ".ino", ".pde", ".s", ".asm", ".ld", ".lf", ".csv")
FW_CONFIG_NAMES = {"platformio.ini", "CMakeLists.txt", "sdkconfig", "idf_component.yml",
                   "dependencies.lock", "sketch.yaml", "library.json", "library.properties",
                   "Makefile", "component.mk"}
FW_CONFIG_GLOBS = ("*.cmake", "sdkconfig.*", "Kconfig*", "*.projbuild")
# 建置會讀「無法從設定靜態看出來」的檔案時,改成任何變更都重建(保守)。
FW_DYNAMIC_CMAKE_RE = re.compile(
    r"\b(?:file\s*\(\s*(?:GLOB|GLOB_RECURSE|READ|STRINGS)\b|configure_file\s*\(|execute_process\s*\("
    r"|add_custom_command\s*\(|add_custom_target\s*\(|\w*_create_partition_image\s*\(|fatfs_create_\w*_image\s*\()",
    re.I)
CMAKE_TOKEN_RE = re.compile(r'"[^"]*"|[^\s()"]+|[()]')
CMAKE_KEYWORD_RE = re.compile(r"[A-Z][A-Z0-9_]+")


def cmake_embedded_tokens(text):
    """從 CMake 文字取出 EMBED_FILES / EMBED_TXTFILES / target_add_binary_data 的路徑 token。
    先切 token 再判斷:不加引號的全大寫字是下一個關鍵字;路徑(含 / 或 .)就算大寫也不會被誤判。"""
    text = re.sub(r"(?m)#.*$", "", text)
    toks = CMAKE_TOKEN_RE.findall(text)
    out, collecting = [], False
    for i, t in enumerate(toks):
        if t in ("EMBED_FILES", "EMBED_TXTFILES"):
            collecting = True
            continue
        if collecting:
            if t in ("(", ")") or CMAKE_KEYWORD_RE.fullmatch(t):
                collecting = False
            else:
                out.append(t)
                continue
        if t.lower() == "target_add_binary_data" and i + 3 < len(toks) and toks[i + 1] == "(":
            out.append(toks[i + 3])  # target_add_binary_data(<target> <file> ...)
    return out


def _norm_rel(project, base_dir, token):
    """把設定裡寫的路徑轉成專案相對路徑;落在專案外或含變數就回 None(→ 保守模式)。"""
    token = token.strip().strip('"').strip("'")
    if not token or "${" in token or "$(" in token:
        return None
    p = Path(token)
    p = p if p.is_absolute() else (base_dir / p)
    try:
        return Path(os.path.normpath(str(p))).relative_to(project).as_posix()
    except ValueError:
        return None


def firmware_inputs(ctx):
    """韌體建置的輸入檔:原始碼/標頭/建置設定 + 設定裡宣告要嵌入的檔案。
    發現建置會讀「靜態看不出來」的東西(file(GLOB)、configure_file、PlatformIO extra_scripts、
    嵌入路徑含變數…)就改成任何變更都重建,寧可多建一次也不漏。"""
    pr = ctx.project
    embedded = set()
    for rel in ctx.files():
        name = rel.rsplit("/", 1)[-1]
        if name != "CMakeLists.txt" and not name.endswith(".cmake"):
            continue
        text = _read_small(pr / rel, 256 * 1024)
        m = FW_DYNAMIC_CMAKE_RE.search(text)
        if m:
            return ("any", None, f"{rel} 使用 {m.group(0).strip()}")
        base = (pr / rel).parent
        for tok in cmake_embedded_tokens(text):
            nr = _norm_rel(pr, base, tok)
            if nr is None:
                return ("any", None, f"{rel} 的嵌入路徑無法靜態解析:{tok}")
            embedded.add(nr)
    ini = pr / "platformio.ini"
    if ini.exists():
        text = _read_small(ini, 256 * 1024)
        if re.search(r"^\s*extra_scripts\s*=", text, re.M):
            return ("any", None, "platformio.ini 使用 extra_scripts")
        for m in re.finditer(r"^\s*board_build\.embed_(?:txt)?files\s*=(.*(?:\n[ \t]+.*)*)", text, re.M):
            for tok in m.group(1).split():
                nr = _norm_rel(pr, pr, tok)
                if nr is None:
                    return ("any", None, f"platformio.ini 的嵌入路徑無法靜態解析:{tok}")
                embedded.add(nr)
    rels = []
    for rel in ctx.files():
        name = rel.rsplit("/", 1)[-1]
        if (rel in embedded or name in FW_CONFIG_NAMES or name.lower().endswith(FW_SOURCE_EXT)
                or any(fnmatch.fnmatch(name, g) for g in FW_CONFIG_GLOBS)):
            rels.append(rel)
    missing_embeds = sorted(e for e in embedded if e not in ctx.manifest.entries)
    reason = f"原始碼/設定 {len(rels)} 個檔案,嵌入 {len(embedded)} 個"
    if missing_embeds:
        reason += f"(其中 {len(missing_embeds)} 個不存在,建置應會失敗)"
    return ("precise", rels + missing_embeds, reason)


def spec_firmware(ctx):
    pr = ctx.project
    if (pr / "platformio.ini").exists():
        cmd, label = ["pio", "run"], "PlatformIO 建置"
    elif (pr / "CMakeLists.txt").exists() and (
            (pr / "sdkconfig").exists() or (pr / "sdkconfig.defaults").exists()
            or (pr / "main" / "idf_component.yml").exists()):
        cmd, label = ["idf.py", "build"], "ESP-IDF 建置"
    elif (pr / "sketch.yaml").exists():
        prof = pr / ".claude" / "arduino-profile"
        p = prof.read_text(encoding="utf-8").strip() if prof.exists() else "default"
        cmd, label = ["arduino-cli", "compile", "--profile", p], "arduino-cli 編譯"
    else:
        inos = ctx.glob("*.ino")
        fqbn = pr / ".claude" / "fqbn"
        if not (inos and fqbn.exists()):
            return None
        cmd = ["arduino-cli", "compile", "-b", fqbn.read_text(encoding="utf-8").strip(),
               str(inos[0].parent)]
        label = "arduino-cli 編譯"
    env = "|".join([str(exe_identity(cmd[0])), " ".join(cmd),
                    _read_small(pr / ".claude" / "fqbn"), _read_small(pr / ".claude" / "arduino-profile"),
                    path_sig(pr / ".pio" / "libdeps"), path_sig(pr / "managed_components"),
                    env_sig(prefixes=("IDF_", "PLATFORMIO_", "ESP_IDF", "ARDUINO_"))])
    return dict(vid="firmware", label=label, run=lambda: [ctx.tool("firmware", label, cmd)],
                inputs=firmware_inputs(ctx), env=env, cacheable=True)


def spec_opencore(ctx):
    oc = next((p for p in ctx.glob("*.plist") if p.name == "config.plist"), None)
    if oc is None:
        return None
    label = "OpenCore 語意(ocvalidate)"

    def run():
        r = ctx.tool("opencore", label, ["ocvalidate", str(oc)])
        if r.status == "fail":
            r.lines.append("注意:ocvalidate 版本須對應你的 OpenCore release")
        return [r]
    base = ctx.rel(oc.parent)
    prefix = "" if base == "." else base + "/"
    rels = [r for r in ctx.files() if r.startswith(prefix)]
    return dict(vid="opencore", label=label, run=run, env=str(exe_identity("ocvalidate")),
                inputs=("precise", rels, f"{prefix or './'} 底下 {len(rels)} 個檔案"), cacheable=True)


def spec_adblock(ctx):
    if not ctx.glob(".aglintrc", ".aglintrc.yaml", ".aglintrc.yml", ".aglintrc.json"):
        return None
    runner = ["aglint"] if shutil.which("aglint") else ["npx", "--no-install", "aglint"]

    # 保險:AGLint 的 exit code 規格未經官方確認,部分 linter 即使有錯仍回 0,
    # 故同時偵測輸出中的 error token(格式如 "12:0  error  ..."),避免漏接。
    def failed(rc, text):
        return rc != 0 or bool(re.search(r"\d+:\d+\s+error\b", text))
    label = "AdGuard 規則(aglint)"
    env = "|".join([str(exe_identity(runner[0])), path_sig(ctx.project / "node_modules" / ".package-lock.json")])
    return dict(vid="adblock", label=label, run=lambda: [ctx.tool("adblock", label, runner, fail_detect=failed)],
                inputs=("any", None, "規則檔可能是任何副檔名"), env=env, cacheable=True)


def _venv_sig(project):
    sigs = []
    for vdir in (".venv", "venv"):
        base = project / vdir
        if base.is_dir():
            for sp in sorted(list(base.glob("lib/python*/site-packages")) + list(base.glob("Lib/site-packages"))):
                sigs.append(f"{sp.name}:{path_sig(sp)}")
    return ",".join(sigs)


def check_pytest(ctx):
    py = project_python(ctx.project)
    # 用「該直譯器能不能 import pytest」當判準,而不是 PATH 上有沒有 pytest ——
    # venv 沒裝 pytest 時硬跑會回非 0,變成另一種假擋。
    # 探測本身的結果要分清楚:只有「跑完且 import 失敗」才是缺 pytest。
    # 探測逾時 / 跑不起來若也當成「缺工具」,在 on_missing=warn 下等於整組測試沒跑就放行。
    limit = max(0.0, min(30.0, ctx.remaining() - 3.0))
    st, _, text, dur = run_tool([py, "-c", "import pytest"], str(ctx.project), limit)
    if st == "timeout":
        return [timeout_result("pytest", "pytest", limit, dur, ctx.budget_cause("pytest", limit),
                               _display_cmd([py, "-c", "import pytest"]))]
    if st == "error":
        return [Result("pytest", "pytest", "error", [clip(text, 220)], duration=dur)]
    if st != "ok":  # fail(ImportError)或 missing(直譯器本身不見了)
        return [Result("pytest", "pytest", "missing", [f"{py} 沒有 pytest(pip install pytest)"])]

    # rc 5 = 沒有收集到任何測試:不是失敗(舊版會因此誤擋)。
    def failed(rc, _text):
        return rc not in (0, 5)
    return [ctx.tool("pytest", "pytest", [py, "-m", "pytest", "-q", "--tb=short", "-rfE"],
                     fail_detect=failed)]


def _has_tests(ctx):
    return bool(ctx.glob("test_*.py", "*_test.py")) or (ctx.project / "tests").is_dir()


def spec_pytest(ctx):
    if not ctx.glob("*.py") or not _has_tests(ctx):
        return None
    py = project_python(ctx.project)
    env = "|".join([str(exe_identity(py)), _venv_sig(ctx.project),
                    env_sig(names=("PYTHONPATH", "VIRTUAL_ENV", "PYTEST_ADDOPTS"))])
    # 測試可能讀專案裡任何檔案(fixture、資料、設定),無法靜態判斷 → 任何變更都重跑。
    return dict(vid="pytest", label="pytest", run=lambda: check_pytest(ctx),
                inputs=("any", None, "測試可能讀取任何檔案"), env=env, cacheable=True)


def _node_cmd(ctx):
    pkg = ctx.project / "package.json"
    if not pkg.exists():
        return None
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8-sig") or "{}").get("scripts", {})
    except Exception:
        scripts = {}
    if not isinstance(scripts, dict):
        scripts = {}
    return ["npm", "test"] if "test" in scripts else (["npm", "run", "build"] if "build" in scripts else None)


def spec_node(ctx):
    cmd = _node_cmd(ctx)
    if cmd is None:
        return None
    label = " ".join(cmd)
    nm = ctx.project / "node_modules"
    env = "|".join([str(exe_identity("npm")), str(exe_identity("node")), path_sig(nm),
                    path_sig(nm / ".package-lock.json"), env_sig(names=("NODE_ENV", "NODE_OPTIONS"))])
    return dict(vid="node", label=label, run=lambda: [ctx.tool("node", label, cmd)],
                inputs=("any", None, "測試/打包可能讀取任何檔案"), env=env, cacheable=True)


def spec_rust(ctx):
    if not (ctx.project / "Cargo.toml").exists():
        return None
    env = "|".join([str(exe_identity("cargo")), str(exe_identity("rustc")), env_sig(names=("RUSTFLAGS",))])
    return dict(vid="rust", label="cargo test",
                run=lambda: [ctx.tool("rust", "cargo test", ["cargo", "test", "--quiet"])],
                inputs=("any", None, "build.rs / include_str! / 測試可能讀取任何檔案"), env=env, cacheable=True)


# ---- Stage 1 介面(不經快取):回傳 list[Result],不適用回傳 []

def _run_spec(spec):
    return spec["run"]() if spec else []


def check_firmware(ctx):
    return _run_spec(spec_firmware(ctx))


def check_plist(ctx):
    return run_file_validator(ctx, "plist") + _run_spec(spec_opencore(ctx))


def check_acpi(ctx):
    return run_file_validator(ctx, "acpi")


def check_xml(ctx):
    return run_file_validator(ctx, "xml")


def check_json(ctx):
    return run_file_validator(ctx, "json")


def check_adblock(ctx):
    return _run_spec(spec_adblock(ctx))


def check_xlsx(ctx):
    return run_file_validator(ctx, "xlsx")


def check_shell(ctx):
    return run_file_validator(ctx, "shell")


def check_python(ctx):
    out = run_file_validator(ctx, "python")
    if out and _has_tests(ctx):
        out += check_pytest(ctx)
    return out


def check_node(ctx):
    return _run_spec(spec_node(ctx))


def check_cargo(ctx):
    return _run_spec(spec_rust(ctx))


BUILTIN_CHECKS = [
    check_firmware, check_plist, check_acpi, check_xml, check_json,
    check_adblock, check_xlsx, check_shell, check_python, check_node, check_cargo,
]

# 有快取的流程用的順序(結果也照這個順序回報)。
PIPELINE = [
    ("firmware", spec_firmware), ("plist", None), ("opencore", spec_opencore), ("acpi", None),
    ("xml", None), ("json", None), ("adblock", spec_adblock), ("xlsx", None), ("shell", None),
    ("python", None), ("pytest", spec_pytest), ("node", spec_node), ("rust", spec_rust),
]


# ---------------------------------------------------------------- 專案自訂標準(override)

def find_override(project):
    d = project / ".claude"
    for name in ("verify", "verify.sh", "verify.py", "verify.cmd", "verify.bat"):
        f = d / name
        if f.is_file():
            return f
    return None


def _override_cmd(f):
    suffix = f.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(f)]
    if suffix == ".sh":
        return ["sh", str(f)]
    return [str(f)]  # .cmd / .bat / 無副檔名(需 +x)


def run_override(ctx, f):
    return [ctx.tool("override", f"專案自訂驗證 .claude/{f.name}", _override_cmd(f))]


def spec_override(ctx, f):
    cmd = _override_cmd(f)
    label = f"專案自訂驗證 .claude/{f.name}"
    # 自訂腳本讀什麼完全不知道 → 任何檔案變更都重跑;override_cache=false 則每次都跑(Stage 1 行為)。
    return dict(vid="override", label=label, run=lambda: [ctx.tool("override", label, cmd)],
                inputs=("any", None, "自訂腳本可能讀取任何檔案"), env=str(exe_identity(cmd[0])),
                cacheable=bool(ctx.cfg["override_cache"]))


# ---------------------------------------------------------------- 排程:決定誰要跑、誰沿用快取

def plan_and_run(ctx, cache_results, override=None):
    """回傳 (results 依固定順序, 新的快取結果, debug 計畫)。"""
    use_cache = bool(ctx.cfg["cache"])
    man = ctx.manifest
    all_digest = man.digest()
    resolved, jobs, plan, new_cache = {}, [], [], {}

    def project_job(idx, spec):
        vid = spec["vid"]
        mode, rels, why = spec["inputs"]
        idig = all_digest if mode == "any" else man.digest(rels)
        key = hashlib.sha256("\0".join([vid, str(VALIDATOR_VERSIONS[vid]), spec["env"], mode, idig])
                             .encode("utf-8", "surrogateescape")).hexdigest()
        # precise 模式:任何檔案被刪除都重跑 —— 被刪的可能是建置用到、但我們沒認出來的檔案
        forced = mode == "precise" and bool(man.deleted)
        prev = cache_results.get(vid) if (use_cache and spec["cacheable"]) else None
        if isinstance(prev, dict) and prev.get("key") == key and prev.get("status") == "pass" and not forced:
            resolved[idx] = [Result(vid, spec["label"], "pass", meta={"cached": True})]
            new_cache[vid] = prev
            plan.append({"id": vid, "action": "cached", "why": f"輸入未變({mode}:{why})"})
            return
        reason = "有檔案被刪除" if forced else ("沒有快取" if not prev else "輸入或環境有變")
        plan.append({"id": vid, "action": "run", "why": f"{reason}({mode}:{why})"})

        def job():
            res = spec["run"]()
            ok = all(r.status == "pass" for r in res)
            return res, ({"key": key, "status": "pass"} if ok and spec["cacheable"] else None)
        jobs.append((idx, vid, vid in HEAVY, job))

    def file_job(idx, vid):
        label, patterns, runner, ident = FILE_VALIDATORS[vid]
        targets = ctx.glob_rel(*patterns)
        if not targets:
            return
        tool_id, missing = ident(ctx)
        if missing is not None:
            resolved[idx] = [missing]
            plan.append({"id": vid, "action": "missing", "why": missing.lines[0]})
            return
        tool_key = f"{vid}:{VALIDATOR_VERSIONS[vid]}:{tool_id}"
        prev = cache_results.get(vid) if use_cache else None
        prev_files = prev.get("files") if (isinstance(prev, dict) and prev.get("tool") == tool_key
                                           and isinstance(prev.get("files"), dict)) else {}
        cached, todo = {}, []
        for rel in targets:
            h = man.hash_of(rel)
            p = prev_files.get(rel)
            if h and isinstance(p, list) and len(p) == 3 and p[0] == h and p[1] in ("pass", "fail"):
                cached[rel] = (p[1], p[2])
            else:
                todo.append(rel)

        def job():
            t0 = time.monotonic()
            outcomes, extras = runner(ctx, todo) if todo else ({}, [])
            allo = dict(cached)
            allo.update(outcomes)
            meta = {"checked": len(outcomes), "cached": len(cached)}
            res = file_results(ctx, vid, label, targets, allo, extras, time.monotonic() - t0, meta)
            entry = {"tool": tool_key, "files": {r: [man.hash_of(r), o[0], o[1]] for r, o in allo.items()}}
            return res, entry
        if not todo:
            resolved[idx], new_cache[vid] = job()
            plan.append({"id": vid, "action": "cached", "why": f"{len(targets)} 個檔案內容都沒變"})
        else:
            plan.append({"id": vid, "action": "run",
                         "why": f"檢查 {len(todo)} 個變更的檔案,沿用 {len(cached)} 個",
                         "files": todo[:20]})
            jobs.append((idx, vid, False, job))

    if override is not None:
        project_job(0, spec_override(ctx, override))
    else:
        for idx, (vid, spec_fn) in enumerate(PIPELINE):
            try:
                if spec_fn is None:
                    file_job(idx, vid)
                else:
                    spec = spec_fn(ctx)
                    if spec is None:
                        if ctx.debug:
                            plan.append({"id": vid, "action": "n/a"})
                        continue
                    project_job(idx, spec)
            except Exception as e:
                resolved[idx] = [Result(vid, vid, "error", [f"內部錯誤:{clip(repr(e), 200)}"])]

    for idx, (res, entry) in execute_jobs(ctx, jobs).items():
        resolved[idx] = res
        if entry is not None:
            new_cache[jobs_vid(jobs, idx)] = entry
    results = [r for idx in sorted(resolved) for r in resolved[idx]]
    return results, new_cache, plan


def jobs_vid(jobs, idx):
    return next(v for i, v, _, _ in jobs if i == idx)


def execute_jobs(ctx, jobs):
    """彼此獨立的驗證器平行執行:總共最多 max_parallel 個,其中重型(建置/測試)最多 max_heavy 個。
    max_parallel=1 → 完全依序執行。"""
    out = {}
    if not jobs:
        return out
    maxp = max(1, int(ctx.cfg["max_parallel"]))
    h = max(1, min(int(ctx.cfg["max_heavy"]), maxp))
    light_n = maxp - h

    def guarded(idx, vid, fn):
        try:
            return fn()
        except Exception as e:
            return [Result(vid, vid, "error", [f"內部錯誤:{clip(repr(e), 200)}"])], None

    if len(jobs) == 1 or maxp == 1:
        for idx, vid, _, fn in sorted(jobs, key=lambda j: (not j[2], j[0])):
            out[idx] = guarded(idx, vid, fn)
        return out
    heavy_pool = ThreadPoolExecutor(max_workers=h)
    light_pool = ThreadPoolExecutor(max_workers=light_n) if light_n > 0 else heavy_pool
    try:
        futs = {}
        for idx, vid, heavy, fn in sorted(jobs, key=lambda j: (not j[2], j[0])):  # 重型先排:它們最久
            pool = heavy_pool if heavy else light_pool
            futs[pool.submit(guarded, idx, vid, fn)] = idx
        for fut in as_completed(futs):
            out[futs[fut]] = fut.result()
    finally:
        heavy_pool.shutdown(wait=True)
        if light_pool is not heavy_pool:
            light_pool.shutdown(wait=True)
    return out


# ---------------------------------------------------------------- 狀態、紀錄、輸出

def load_state(project):
    try:
        st = json.loads((project / ".claude" / STATE_NAME).read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(project, state):
    path = project / ".claude" / STATE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as e:
        warn(f"無法寫入狀態檔:{e}")


def append_log(project, record):
    """逾時、缺工具、擋下都記在 .claude/.verify.log(JSON Lines),超過 256KB 只留後半。"""
    path = project / ".claude" / LOG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "v": VERSION}, **record)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            data = path.read_bytes()[-LOG_MAX_BYTES // 2:]
            nl = data.find(b"\n")
            path.write_bytes(data[nl + 1:] if nl >= 0 else b"")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as e:
        warn(f"無法寫入紀錄檔:{e}")


def policy_of(cfg, r):
    if r.status == "fail":
        return "block"
    if r.status == "pass":
        return "ok"
    if r.status == "missing":
        return vopt(cfg, r.vid, "on_missing")
    if r.status == "timeout":
        return vopt(cfg, r.vid, "on_timeout")
    return "warn"  # error:hook 自己的問題,擋了 Claude 也修不了


KIND = {"missing": "工具缺失", "timeout": "逾時,疑點", "error": "執行錯誤", "fail": "失敗"}


def fmt_rc(rc):
    """被訊號終止(POSIX 負值)或 Windows NTSTATUS(如 0xC0000005)要讀得出來。"""
    if rc is None:
        return ""
    if rc < 0:
        return f"被訊號 {-rc} 終止"
    if rc > 255:
        return f"exit 0x{rc & 0xFFFFFFFF:08X}"
    return f"exit {rc}"


def build_reason(blocking, warnings, cfg):
    limit = cfg["max_reason_chars"]
    parts = [f"驗證未通過({len(blocking)} 項)。請修正後再結束,不要宣稱任務已完成。"]
    for i, r in enumerate(blocking):
        head = f"✗ [{r.vid}] {r.label}"
        if r.status == "timeout":
            head += ":逾時,結果未判定(逾時不算通過)"
        elif r.status != "fail":
            head += f":{KIND[r.status]}(設定要求視為失敗)"
        elif r.rc not in (None, 0):
            head += f"({fmt_rc(r.rc)})"
        block = "\n".join([head] + ["  " + ln for ln in r.lines])
        if sum(len(p) for p in parts) + len(block) > limit - 300:
            parts.append(f"…另 {len(blocking) - i} 項省略(輸出太長)")
            break
        parts.append(block)
    timed_out = [r.vid for r in blocking if r.status == "timeout"]
    if timed_out:
        parts.append("逾時不代表失敗,也不代表通過。若是卡住(等待輸入、死迴圈、測試掛住)請修正;"
                     "若是正常的長時間建置,請使用者決定是否調高 .claude/verify.json 的 "
                     + "、".join(f"validators.{v}.timeout" for v in dict.fromkeys(timed_out)) + "。")
    if warnings:
        parts.append("⚠ 未驗證(不擋):" + ";".join(f"{r.label}({KIND[r.status]})" for r in warnings))
    parts.append("(硬體專屬或 WIP 改動需要略過時,請使用者決定是否 touch .claude/skip-verify;不要自行建立。)")
    return clip("\n\n".join(parts), limit)


def warning_message(warnings, notes):
    bits = []
    if warnings:
        bits.append("未驗證 " + str(len(warnings)) + " 項 — "
                    + ";".join(f"{r.label}({KIND[r.status]}" + (f":{r.lines[0]}" if r.lines else "") + ")"
                               for r in warnings))
    bits.extend(notes)
    return clip("verify:" + " | ".join(bits), 1500) if bits else None


def finish(cfg=None, decision=None, reason=None, system=None):
    """輸出裁決並結束。stdout 一律 ASCII-only JSON。"""
    mode = (cfg or DEFAULT_CONFIG)["block_mode"]
    if decision == "block" and mode == "exit2":
        sys.stderr.write(reason + "\n")
        sys.stderr.flush()
        # 官方文件:Stop hook exit 2「stdout 為空且 stderr 像是 No such file or directory」
        # 會被當成 hook 本身壞掉(non-blocking)。編譯錯誤常含這句,所以這時補一份 JSON。
        if re.search(r"no such file or directory", reason, re.I):
            print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=True))
        sys.exit(2)
    out = {}
    if decision == "block":
        out = {"decision": "block", "reason": reason}
    if system:
        out["systemMessage"] = system
    if out:
        print(json.dumps(out, ensure_ascii=True))
    sys.exit(0)


# ---------------------------------------------------------------- main

def read_input():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.buffer.read()
    except Exception:
        return {}
    # 明確以 UTF-8 解碼:Windows 預設 cp950 會讓含中文的 last_assistant_message
    # 解碼失敗 → 整個輸入被丟掉 → stop_hook_active 跟著消失 → 防迴圈失效。
    try:
        data = json.loads(raw.decode("utf-8-sig", "replace") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def main():
    t0 = time.monotonic()
    data = read_input()
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    try:
        project = project.resolve()
        os.chdir(str(project))
    except OSError:
        sys.exit(0)

    if (project / ".claude" / "skip-verify").exists():
        finish()

    notes = []
    cfg = load_config(project, notes)
    state = load_state(project)
    session = str(data.get("session_id") or "")
    active = bool(data.get("stop_hook_active"))
    if not active or state.get("session") != session:
        state["blocks"] = 0
        state.pop("block_fp", None)
    state["session"] = session

    ctx = Ctx(project, cfg, t0)
    cache = load_cache(project)
    manifest = Manifest(project, cache["manifest"], cfg["trust_mtime"]).scan(
        ctx.files() + claude_extras(project))
    ctx.manifest = manifest
    fp_before = fingerprint(manifest)

    if active:
        if not cfg["rerun_when_active"]:
            finish(cfg)  # 舊行為:已在續跑中 → 一律放行
        last = "、".join(state.get("last_block") or []) or "未知"
        if state.get("block_fp") == fp_before:
            save_state(project, state)
            append_log(project, {"decision": "allow-unchanged", "active": True,
                                 "last_block": state.get("last_block")})
            finish(cfg, system=warning_message([], notes + [
                "上次擋下後專案檔案沒有變更,本次不重驗、直接放行以免卡死。"
                f"上次未通過:{last}(這不是通過,請自行確認)。"]))
        cap = cfg["max_consecutive_blocks"]
        if cap and state.get("blocks", 0) >= cap:
            save_state(project, state)
            append_log(project, {"decision": "allow-cap", "active": True, "blocks": state["blocks"],
                                 "last_block": state.get("last_block")})
            finish(cfg, system=warning_message([], notes + [
                f"已連續擋下 {cap} 次仍未通過,放行交還給你。上次未通過:{last}(這不是通過);"
                f"紀錄在 .claude/{LOG_NAME}。"]))

    override = find_override(project)
    if state.get("ok_fp") == fp_before and (override is None or cfg["override_cache"]):
        if ctx.debug:
            append_log(project, {"decision": "allow-unchanged-green", "changes": manifest.changes()})
        finish(cfg, system=warning_message([], notes))  # 自上次全綠後沒有任何變更

    results, new_cache, plan = plan_and_run(ctx, cache["results"], override)

    blocking, warnings = [], []
    for r in results:
        pol = policy_of(cfg, r)
        if pol == "block":
            blocking.append(r)
        elif pol == "warn":
            warnings.append(r)
        if r.status in ("missing", "timeout", "error"):
            warn(f"{r.vid}: {r.status} {' / '.join(r.lines)}")
    if ctx.debug:
        for step in plan:
            warn(f"plan {step['id']}: {step['action']} — {step.get('why', '')}")

    # 驗證器可能寫檔(例如 iasl 產生 .aml):重新掃一次,記下「驗證完之後」的狀態。
    ctx.reset_files()
    after = Manifest(project, manifest.entries, cfg["trust_mtime"]).scan(
        ctx.files() + claude_extras(project))
    fp_after = fingerprint(after)
    save_cache(project, after, new_cache)
    record = {
        "decision": "block" if blocking else "allow", "active": active,
        "sec": round(time.monotonic() - t0, 2),
        "results": [r.as_log() for r in results],
        "suspect": [r.vid for r in results if r.status == "timeout"],
    }
    if ctx.debug:
        record["changes"] = manifest.changes()
        record["plan"] = plan
    append_log(project, record)

    if blocking:
        state["blocks"] = state.get("blocks", 0) + 1
        state["block_fp"] = fp_after
        state["last_block"] = [f"{r.vid}:{r.status}" for r in blocking]
        state.pop("ok_fp", None)
        save_state(project, state)
        finish(cfg, "block", build_reason(blocking, warnings, cfg),
               warning_message(warnings, notes))

    state["blocks"] = 0
    state.pop("block_fp", None)
    state.pop("last_block", None)
    if any(r.status in ("timeout", "error") for r in results):
        state.pop("ok_fp", None)  # 沒驗完不算綠,下次重驗
    elif override is None or cfg["override_cache"]:
        state["ok_fp"] = fp_after
    else:
        state.pop("ok_fp", None)
    save_state(project, state)
    finish(cfg, system=warning_message(warnings, notes))


def entry():
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:  # hook 自己壞掉:放行(Claude 修不了 hook),但讓使用者看到
        warn(f"內部錯誤:{e!r}")
        try:
            print(json.dumps({"systemMessage": f"verify.py 內部錯誤,本次未驗證:{clip(repr(e), 300)}"},
                             ensure_ascii=True))
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    entry()
