"""共用的測試工具:在暫存專案裡以子程序執行 Stop hook,模擬 Claude Code 的 stdin。"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "verify.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("verify_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HookRun:
    def __init__(self, cp, elapsed):
        self.rc = cp.returncode
        self.raw_stdout = cp.stdout
        self.stdout = cp.stdout.decode("utf-8")
        self.stderr = cp.stderr.decode("utf-8", "replace")
        self.elapsed = elapsed
        text = self.stdout.strip()
        self.json = json.loads(text) if text else None

    @property
    def blocked(self):
        return self.rc == 2 or bool(self.json and self.json.get("decision") == "block")

    @property
    def reason(self):
        if self.json and "reason" in self.json:
            return self.json["reason"]
        return self.stderr if self.rc == 2 else ""

    @property
    def system(self):
        return (self.json or {}).get("systemMessage") or ""

    def __repr__(self):
        return f"HookRun(rc={self.rc}, stdout={self.stdout[:300]!r}, stderr={self.stderr[:300]!r})"


def run_hook(project, payload=None, env=None, raw=None, timeout=120):
    import time
    e = dict(os.environ)
    e["CLAUDE_PROJECT_DIR"] = str(project)
    e.update(env or {})
    data = raw if raw is not None else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    t0 = time.monotonic()
    cp = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True,
                        env=e, cwd=str(project), timeout=timeout)
    return HookRun(cp, time.monotonic() - t0)


def write(project, rel, text, mode=None):
    p = Path(project) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")
    if mode is not None:
        p.chmod(mode)
    return p


def configure(project, **cfg):
    write(project, ".claude/verify.json", json.dumps(cfg, ensure_ascii=False))


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture(scope="session")
def hook():
    return load_hook()


# ---------------------------------------------------------------- 跨平台假工具(Stage 3-1)
#
# 假工具本體是 Python(tests/fake_tool.py),啟動器依平台產生:
#   POSIX   <name>        sh 腳本:exec python fake_tool.py <name>.fake.json "$@"
#   Windows <name>.cmd    批次檔:"python" fake_tool.py <name>.fake.json %*   ← 順便走真的 .cmd 執行路徑
# 每次呼叫把「名稱 start/end 時間」寫進專案外的紀錄檔,測試靠它判斷有沒有跑、跑幾次、是否同時。

WINDOWS = os.name == "nt"
POSIX = not WINDOWS
FAKE_IMPL = Path(__file__).resolve().parent / "fake_tool.py"


class FakeTools:
    def __init__(self, bindir, log):
        self.bindir, self.log = bindir, log
        self.env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "CLAUDE_VERIFY_DEBUG": "1",
                    "FAKE_TOOL_LOG": str(log)}

    def launcher(self, name):
        return self.bindir / (name + ".cmd" if WINDOWS else name)

    def make(self, name, sleep=0.0, rc=0, output="", encoding="utf-8"):
        cfg = self.bindir / f"{name}.fake.json"
        cfg.write_text(json.dumps({"sleep": sleep, "rc": rc, "output": output, "encoding": encoding}),
                       encoding="utf-8")
        path = self.launcher(name)
        if WINDOWS:
            # 設定檔用 %~dp0(批次檔自己的目錄)指:cmd.exe 以 OEM 碼頁讀批次檔內容,
            # 把含中文或空白的目錄寫死在檔案裡會被讀壞;%~dp0 由 cmd 自己展開,沒有這個問題。
            path.write_text(f'@"{sys.executable}" "{FAKE_IMPL}" "%~dp0{name}.fake.json" %*\r\n'
                            f'@exit /b %ERRORLEVEL%\r\n', encoding="utf-8")
        else:
            path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_IMPL}" "{cfg}" "$@"\n', encoding="utf-8")
            path.chmod(0o755)
        return path

    def wrap(self, name, real):
        """PATH 上放一個轉呼叫真工具的啟動器(模擬 nvm 切換之類:同名、不同執行檔)。"""
        path = self.launcher(name)
        if WINDOWS:
            path.write_text(f'@"{real}" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding="utf-8")
        else:
            path.write_text(f'#!/bin/sh\nexec "{real}" "$@"\n', encoding="utf-8")
            path.chmod(0o755)
        return path

    @staticmethod
    def bump(path):
        """改變工具的識別(大小與 mtime),但不改變行為 —— 模擬升級。"""
        comment = "\r\nrem bump\r\n" if str(path).lower().endswith((".cmd", ".bat")) else "\n# bump\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(comment)

    def calls(self, name=None):
        rows = [ln.split() for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        out = {}
        for tool, kind, ts in rows:
            out.setdefault(tool, []).append((kind, float(ts)))
        return out if name is None else out.get(name, [])

    def runs(self, name):
        return sum(1 for k, _ in self.calls(name) if k == "start")


@pytest.fixture
def fake(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    return FakeTools(bindir, log)


def venv_python(venv_dir):
    return Path(venv_dir) / ("Scripts/python.exe" if WINDOWS else "bin/python")


def pid_alive(pid):
    """程序還在跑嗎?跨平台。
    POSIX:被殺掉但沒人回收的殭屍(容器的 PID 1 常不回收)算死。
    Windows:OpenProcess + GetExitCodeProcess,STILL_ACTIVE(259)才算活著。"""
    if WINDOWS:
        import ctypes  # noqa: PLC0415
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = f"/proc/{pid}/status"
    if os.path.exists(status):
        try:
            with open(status, encoding="utf-8") as fh:
                return not any(ln.startswith("State:") and "Z" in ln.split()[1] for ln in fh)
        except (ProcessLookupError, FileNotFoundError):
            return False        # 檢查之後、讀完之前程序消失了(第一次 CI 實測:讀到一半 ESRCH)
    return True
