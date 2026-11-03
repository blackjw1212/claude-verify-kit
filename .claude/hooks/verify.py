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

每個結果是四種狀態之一,處置不同(.claude/verify.json 可調):
  fail     真正的驗證失敗           → 擋下
  missing  工具不存在               → 預設只警告(on_missing: warn|block|ignore)
  timeout  逾時,記錄為疑點         → 預設只警告(on_timeout: warn|block|ignore)
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
JSONC_NAMES = ("tsconfig*.json", "jsconfig*.json", "devcontainer.json",
               ".devcontainer.json", ".eslintrc.json", ".babelrc.json",
               "api-extractor.json", "tslint.json", "deno.json", "turbo.json",
               "biome.json")

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
    "on_timeout": "warn",           # warn | block | ignore
    "validators": {},               # {"<id>": {"timeout": 秒, "on_missing": ..., "on_timeout": ...}}
}

# 各驗證器的預設逾時(秒)。實際上限 = min(這個值, 剩餘預算)。
DEFAULT_TIMEOUTS = {
    "override": 540, "firmware": 480, "rust": 480, "node": 300, "pytest": 300,
    "python": 120, "adblock": 120, "xlsx": 120, "shell": 60, "plist": 60,
    "opencore": 60, "acpi": 60, "xml": 60, "json": 60,
}
POLICIES = ("warn", "block", "ignore")


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
        elif key == "rerun_when_active":
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
            minimum = {"max_lines": 1, "max_reason_chars": 500,
                       "budget_seconds": 1, "max_consecutive_blocks": 0}[key]
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
    __slots__ = ("vid", "label", "status", "lines", "rc", "duration")

    def __init__(self, vid, label, status, lines=None, rc=None, duration=0.0):
        self.vid, self.label, self.status = vid, label, status
        self.lines = lines or []
        self.rc, self.duration = rc, duration

    def as_log(self):
        return {"id": self.vid, "status": self.status, "rc": self.rc,
                "sec": round(self.duration, 2)}


class Ctx:
    def __init__(self, project, cfg, t0):
        self.project = project
        self.cfg = cfg
        self.deadline = t0 + float(cfg["budget_seconds"])
        self._files = None

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

    def glob(self, *patterns):
        return [self.project / rel for rel in self.files()
                if any(fnmatch.fnmatch(rel.rsplit("/", 1)[-1], p) for p in patterns)]

    def rel(self, path):
        try:
            return Path(path).relative_to(self.project).as_posix()
        except ValueError:
            return str(path)

    # ---- 執行外部工具並轉成 Result
    def tool(self, vid, label, argv, cwd=None, env=None, stdin_bytes=None,
             fail_detect=None):
        timeout = self.timeout_for(vid)
        status, rc, text, dur = run_tool(argv, str(cwd or self.project), timeout,
                                         env=env, stdin_bytes=stdin_bytes)
        if status in ("ok", "fail") and fail_detect is not None:
            status = "fail" if fail_detect(rc, text) else "ok"
        max_lines = self.cfg["max_lines"]
        if status == "ok":
            return Result(vid, label, "pass", rc=rc, duration=dur)
        if status == "fail":
            return Result(vid, label, "fail", summarize(text, max_lines, self.project), rc, dur)
        if status == "missing":
            return Result(vid, label, "missing", [f"{text}(不在 PATH)"], duration=dur)
        if status == "timeout":
            return Result(vid, label, "timeout",
                          [f"逾時(上限 {timeout:.0f}s),結果未判定"], duration=dur)
        return Result(vid, label, "error", [clip(text, 220)], duration=dur)


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


def fingerprint(project, files):
    """專案狀態指紋:檔案清單 + 大小 + mtime。

    舊版用「有沒有檔案比 marker 新」判斷變更,刪檔完全看不到(刪掉被 import 的模組
    後照樣放行)。指紋把清單本身算進去,增、刪、改都會變。
    .claude/ 只納入頂層的非隱藏檔與 hooks/(設定與自訂驗證腳本改了要重驗),
    隱藏的狀態檔(本 hook 與 loop-gate 寫的)不納入,否則寫狀態就會改變指紋。
    """
    entries = list(files)
    cdir = project / ".claude"
    if cdir.is_dir():
        try:
            for p in sorted(cdir.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    entries.append(".claude/" + p.name)
            hooks = cdir / "hooks"
            if hooks.is_dir():
                for p in sorted(hooks.iterdir()):
                    if p.is_file() and not p.name.startswith("."):
                        entries.append(".claude/hooks/" + p.name)
        except OSError:
            pass
    h = hashlib.sha256(VERSION.encode())
    for rel in entries:
        try:
            st = os.stat(project / rel)
            h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8", "surrogateescape"))
        except OSError:
            h.update(f"{rel}\0gone\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


# ---------------------------------------------------------------- 內建檢查
# 每個檢查回傳 list[Result];不適用回傳 []。


def check_firmware(ctx):
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
            return []
        cmd = ["arduino-cli", "compile", "-b", fqbn.read_text(encoding="utf-8").strip(),
               str(inos[0].parent)]
        label = "arduino-cli 編譯"
    return [ctx.tool("firmware", label, cmd)]


def _per_file(ctx, vid, label, files, argv_for, cwd_for=None):
    """同一個工具逐檔執行,聚合成一個 Result。"""
    fails, worst, total = [], None, 0.0
    for f in files:
        r = ctx.tool(vid, label, argv_for(f), cwd=cwd_for(f) if cwd_for else None)
        total += r.duration
        if r.status in ("missing", "timeout", "error"):
            r.duration = total
            return r
        if r.status == "fail":
            fails.append(f"{ctx.rel(f)}:")
            fails.extend("  " + ln for ln in r.lines)
            worst = r.rc
    if fails:
        return Result(vid, label, "fail", fails, worst, total)
    return Result(vid, label, "pass", duration=total)


def check_plist(ctx):
    files = ctx.glob("*.plist")
    if not files:
        return []
    out = [_per_file(ctx, "plist", "plist 語法(plutil)", files,
                     lambda f: ["plutil", "-lint", str(f)])]
    oc = next((p for p in files if p.name == "config.plist"), None)
    if oc is not None:
        r = ctx.tool("opencore", "OpenCore 語意(ocvalidate)", ["ocvalidate", str(oc)])
        if r.status == "fail":
            r.lines.append("注意:ocvalidate 版本須對應你的 OpenCore release")
        out.append(r)
    return out


def check_acpi(ctx):
    files = ctx.glob("*.dsl")
    if not files:
        return []
    return [_per_file(ctx, "acpi", "ACPI/SSDT(iasl)", files,
                      lambda f: ["iasl", str(f)], cwd_for=lambda f: f.parent)]


def _collect(vid, label, fails, ctx, t0):
    dur = time.monotonic() - t0
    if not fails:
        return Result(vid, label, "pass", duration=dur)
    n = ctx.cfg["max_lines"]
    lines = fails[:n] + ([f"…另 {len(fails) - n} 個檔案省略"] if len(fails) > n else [])
    return Result(vid, label, "fail", lines, duration=dur)


def check_xml(ctx):
    files = ctx.glob("*.xml")
    if not files:
        return []
    t0 = time.monotonic()
    fails = []
    for f in files:
        if ctx.remaining() < 3:
            return [Result("xml", "XML 格式", "timeout", ["時間預算用盡,未檢查完"],
                           duration=time.monotonic() - t0)]
        parser = xml.parsers.expat.ParserCreate()
        try:
            with open(f, "rb") as fh:
                parser.ParseFile(fh)
        except xml.parsers.expat.ExpatError as e:
            fails.append(f"{ctx.rel(f)}:{e.lineno}:{e.offset}: {xml.parsers.expat.ErrorString(e.code)}")
        except OSError as e:
            fails.append(f"{ctx.rel(f)}: 無法讀取({e})")
    return [_collect("xml", "XML 格式", fails, ctx, t0)]


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


def check_json(ctx):
    files = ctx.glob("*.json")
    if not files:
        return []
    t0 = time.monotonic()
    fails = []
    for f in files:
        if ctx.remaining() < 3:
            return [Result("json", "JSON 格式", "timeout", ["時間預算用盡,未檢查完"],
                           duration=time.monotonic() - t0)]
        try:
            # utf-8-sig 容忍 UTF-8 BOM(Windows 編輯器常見)。
            text = f.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            fails.append(f"{ctx.rel(f)}: 無法以 UTF-8 讀取({e})")
            continue
        try:
            json.loads(text)
            continue
        except json.JSONDecodeError as e:
            err = e
        if any(fnmatch.fnmatch(f.name, p) for p in JSONC_NAMES):
            try:
                json.loads(strip_jsonc(text))
                continue
            except json.JSONDecodeError as e2:
                err = e2
        fails.append(f"{ctx.rel(f)}:{err.lineno}:{err.colno}: {err.msg}")
    return [_collect("json", "JSON 格式", fails, ctx, t0)]


def check_adblock(ctx):
    cfg = ctx.glob(".aglintrc", ".aglintrc.yaml", ".aglintrc.yml", ".aglintrc.json")
    if not cfg:
        return []
    runner = ["aglint"] if shutil.which("aglint") else ["npx", "--no-install", "aglint"]

    # 保險:AGLint 的 exit code 規格未經官方確認,部分 linter 即使有錯仍回 0,
    # 故同時偵測輸出中的 error token(格式如 "12:0  error  ..."),避免漏接。
    def failed(rc, text):
        return rc != 0 or bool(re.search(r"\d+:\d+\s+error\b", text))
    return [ctx.tool("adblock", "AdGuard 規則(aglint)", runner, fail_detect=failed)]


def check_xlsx(ctx):
    files = ctx.glob("*.xlsx")
    if not files:
        return []
    try:
        import openpyxl  # noqa: PLC0415
    except Exception:
        return [Result("xlsx", "Excel xlsx", "missing", ["openpyxl 未安裝(pip install openpyxl)"])]
    t0 = time.monotonic()
    fails = []
    for f in files:
        if ctx.remaining() < 3:
            return [Result("xlsx", "Excel xlsx", "timeout", ["時間預算用盡,未檢查完"],
                           duration=time.monotonic() - t0)]
        try:
            openpyxl.load_workbook(str(f))
        except Exception as e:
            fails.append(f"{ctx.rel(f)}: 損毀/無法載入({clip(str(e), 160)})")
    return [_collect("xlsx", "Excel xlsx", fails, ctx, t0)]


def check_shell(ctx):
    files = ctx.glob("*.sh")
    if not files:
        return []
    # -f gcc:每個問題一行 file:line:col: severity: msg [SCxxxx],最好摘要。
    return [ctx.tool("shell", "shellcheck", ["shellcheck", "-f", "gcc"] + [str(p) for p in files])]


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


def check_python(ctx):
    files = ctx.glob("*.py")
    if not files:
        return []
    py = project_python(ctx.project)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    payload = "\0".join(str(f) for f in files).encode("utf-8", "surrogateescape")
    out = [ctx.tool("python", "Python 語法", [py, "-c", _SYNTAX_SCRIPT], env=env,
                    stdin_bytes=payload)]
    has_tests = bool(ctx.glob("test_*.py", "*_test.py")) or (ctx.project / "tests").is_dir()
    if not has_tests:
        return out
    # 用「該直譯器能不能 import pytest」當判準,而不是 PATH 上有沒有 pytest ——
    # venv 沒裝 pytest 時硬跑會回非 0,變成另一種假擋。
    st, _, _, _ = run_tool([py, "-c", "import pytest"], str(ctx.project), min(30.0, ctx.remaining()))
    if st != "ok":
        out.append(Result("pytest", "pytest", "missing", [f"{py} 沒有 pytest(pip install pytest)"]))
        return out

    # rc 5 = 沒有收集到任何測試:不是失敗(舊版會因此誤擋)。
    def failed(rc, _text):
        return rc not in (0, 5)
    out.append(ctx.tool("pytest", "pytest", [py, "-m", "pytest", "-q", "--tb=short", "-rfE"],
                        fail_detect=failed))
    return out


def check_node(ctx):
    pkg = ctx.project / "package.json"
    if not pkg.exists():
        return []
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8-sig") or "{}").get("scripts", {})
    except Exception:
        scripts = {}
    if not isinstance(scripts, dict):
        scripts = {}
    cmd = ["npm", "test"] if "test" in scripts else (["npm", "run", "build"] if "build" in scripts else None)
    if cmd is None:
        return []
    return [ctx.tool("node", " ".join(cmd), cmd)]


def check_cargo(ctx):
    if not (ctx.project / "Cargo.toml").exists():
        return []
    return [ctx.tool("rust", "cargo test", ["cargo", "test", "--quiet"])]


BUILTIN_CHECKS = [
    check_firmware, check_plist, check_acpi, check_xml, check_json,
    check_adblock, check_xlsx, check_shell, check_python, check_node, check_cargo,
]


# ---------------------------------------------------------------- 專案自訂標準(override)

def find_override(project):
    d = project / ".claude"
    for name in ("verify", "verify.sh", "verify.py", "verify.cmd", "verify.bat"):
        f = d / name
        if f.is_file():
            return f
    return None


def run_override(ctx, f):
    suffix = f.suffix.lower()
    if suffix == ".py":
        cmd = [sys.executable, str(f)]
    elif suffix == ".sh":
        cmd = ["sh", str(f)]
    else:  # .cmd / .bat / 無副檔名(需 +x)
        cmd = [str(f)]
    return [ctx.tool("override", f"專案自訂驗證 .claude/{f.name}", cmd)]


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


def build_reason(blocking, warnings, cfg):
    limit = cfg["max_reason_chars"]
    parts = [f"驗證未通過({len(blocking)} 項)。請修正後再結束,不要宣稱任務已完成。"]
    for i, r in enumerate(blocking):
        head = f"✗ [{r.vid}] {r.label}"
        if r.status != "fail":
            head += f":{KIND[r.status]}(設定要求視為失敗)"
        elif r.rc not in (None, 0):
            head += f"(exit {r.rc})"
        block = "\n".join([head] + ["  " + ln for ln in r.lines])
        if sum(len(p) for p in parts) + len(block) > limit - 300:
            parts.append(f"…另 {len(blocking) - i} 項省略(輸出太長)")
            break
        parts.append(block)
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
    fp_before = fingerprint(project, ctx.files())

    if active:
        if not cfg["rerun_when_active"]:
            finish(cfg)  # 舊行為:已在續跑中 → 一律放行
        if state.get("block_fp") == fp_before:
            save_state(project, state)
            append_log(project, {"decision": "allow-unchanged", "active": True})
            finish(cfg, system=warning_message([], notes + [
                "上次擋下後專案沒有新修改,放行以免卡死;驗證仍未通過,請自行確認。"]))
        cap = cfg["max_consecutive_blocks"]
        if cap and state.get("blocks", 0) >= cap:
            save_state(project, state)
            append_log(project, {"decision": "allow-cap", "active": True, "blocks": state["blocks"]})
            finish(cfg, system=warning_message([], notes + [
                f"已連續擋下 {cap} 次仍未通過,放行交還給你;紀錄在 .claude/{LOG_NAME}。"]))

    override = find_override(project)
    if override is None and state.get("ok_fp") == fp_before:
        finish(cfg, system=warning_message([], notes))  # 自上次全綠後沒有任何變更

    results = []
    if override is not None:
        results = run_override(ctx, override)
    else:
        for chk in BUILTIN_CHECKS:
            try:
                results.extend(chk(ctx))
            except Exception as e:
                results.append(Result(chk.__name__.replace("check_", ""), chk.__name__,
                                      "error", [f"內部錯誤:{clip(repr(e), 200)}"]))

    blocking, warnings = [], []
    for r in results:
        pol = policy_of(cfg, r)
        if pol == "block":
            blocking.append(r)
        elif pol == "warn":
            warnings.append(r)
        if r.status in ("missing", "timeout", "error"):
            warn(f"{r.vid}: {r.status} {' / '.join(r.lines)}")

    ctx.reset_files()
    fp_after = fingerprint(project, ctx.files())
    append_log(project, {
        "decision": "block" if blocking else "allow", "active": active,
        "sec": round(time.monotonic() - t0, 2),
        "results": [r.as_log() for r in results],
        "suspect": [r.vid for r in results if r.status == "timeout"],
    })

    if blocking:
        state["blocks"] = state.get("blocks", 0) + 1
        state["block_fp"] = fp_after
        state.pop("ok_fp", None)
        save_state(project, state)
        finish(cfg, "block", build_reason(blocking, warnings, cfg),
               warning_message(warnings, notes))

    state["blocks"] = 0
    state.pop("block_fp", None)
    if any(r.status in ("timeout", "error") for r in results):
        state.pop("ok_fp", None)  # 沒驗完不算綠,下次重驗
    elif override is None:
        state["ok_fp"] = fp_after
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
