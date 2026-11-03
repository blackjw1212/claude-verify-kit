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
