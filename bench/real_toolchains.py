#!/usr/bin/env python3
"""真實工具鏈驗收(Stage 3-2)。

每個工具鏈在隔離目錄裡建一個真的專案、用真的工具(pip / npm / cargo / rustup / cmake /
gcc / clang / shellcheck / PlatformIO / ESP-IDF)建置,然後照順序做:
  初次驗證 → 無變更 → 真的改相依 → 故意編譯錯誤 → 修正 → 換工具版本
每一步記錄:hook 的裁決、哪個驗證器真的重跑(plan 的 action)、原因裡有沒有指到 file:line、耗時。

這支需要網路(PyPI、npm registry、crates.io、static.rust-lang.org、github release),
所以不進 pytest、不進 CI;結果寫成 JSON 與 Markdown,當作驗收紀錄。

用法:
  python3 bench/real_toolchains.py --work DIR [--only python_venv,node] [--out results.json]
  ESP-IDF 與 PlatformIO 需要事先準備好的環境:--idf-env FILE(shell 的 export 檔)、--pio-core DIR --pio-venv DIR
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.request
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "verify.py"
WINDOWS = os.name == "nt"


class SetupError(Exception):
    pass


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def configure(project, **cfg):
    write(project, ".claude/verify.json", json.dumps(cfg))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12] if Path(path).exists() else None


class Suite:
    def __init__(self, name, work, env=None, hook_py=None, keep=False):
        """keep=True:沿用準備階段已經建好的目錄(兩段式執行:先有網路安裝、再無網路建置與測試)。"""
        self.name = name
        self.dir = Path(work) / name
        if self.dir.exists() and not keep:
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proj = self.dir / "proj"
        self.proj.mkdir(exist_ok=True)
        self.env = dict(env or {})
        self.hook_py = hook_py or sys.executable
        self.steps, self.setup, self.versions, self.notes = [], [], {}, []

    # ---- 真的指令
    def sh(self, cmd, cwd=None, env=None, timeout=3600, check=True, label=None):
        e = dict(os.environ)
        e.update(self.env)
        e.update(env or {})
        t0 = time.monotonic()
        try:
            cp = subprocess.run([str(c) for c in cmd], cwd=str(cwd or self.proj), env=e, capture_output=True,
                                timeout=timeout)
            rc, out = cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", "replace")
        except FileNotFoundError as ex:
            rc, out = 127, str(ex)
        rec = {"cmd": label or " ".join(str(c) for c in cmd), "rc": rc, "sec": round(time.monotonic() - t0, 2),
               "tail": out.strip()[-400:]}
        self.setup.append(rec)
        if check and rc != 0:
            raise SetupError(f"{rec['cmd']} → rc {rc}: {rec['tail'][-300:]}")
        return out

    def version(self, key, cmd, env=None):
        try:
            out = self.sh(cmd, env=env, check=False, label=f"[版本] {' '.join(map(str, cmd))}")
            self.versions[key] = out.strip().splitlines()[0][:120] if out.strip() else "?"
        except Exception as ex:  # noqa: BLE001
            self.versions[key] = f"?({ex})"

    # ---- 跑 hook
    def hook(self, env=None):
        e = dict(os.environ)
        e.update(self.env)
        e.update(env or {})
        e["CLAUDE_PROJECT_DIR"] = str(self.proj)
        e["CLAUDE_VERIFY_DEBUG"] = "1"
        payload = json.dumps({"session_id": "rt", "stop_hook_active": False, "hook_event_name": "Stop"})
        t0 = time.monotonic()
        cp = subprocess.run([self.hook_py, str(HOOK)], input=payload.encode(), capture_output=True, env=e,
                            cwd=str(self.proj), timeout=1800)
        sec = time.monotonic() - t0
        text = cp.stdout.decode("utf-8").strip()
        out = json.loads(text) if text else {}
        blocked = cp.returncode == 2 or out.get("decision") == "block"
        reason = out.get("reason") or (cp.stderr.decode("utf-8", "replace") if cp.returncode == 2 else "")
        logf = self.proj / ".claude" / ".verify.log"
        rec = json.loads(logf.read_text(encoding="utf-8").splitlines()[-1]) if logf.exists() else {}
        if rec.get("decision") == "allow-unchanged-green":
            actions = {"*": "fast"}
        else:
            actions = {p["id"]: p["action"] for p in rec.get("plan", []) if p.get("action") != "n/a"}
        return {"verdict": "block" if blocked else "pass", "sec": round(sec, 2), "actions": actions,
                "results": {r["id"]: r["status"] for r in rec.get("results", [])},
                "reason": reason, "system": out.get("systemMessage", "")}

    def step(self, label, expect, vid=None, action=None, reason_has=None, env=None):
        """expect:pass / block;action:預期的 plan 動作(run / cached / fast),None = 不檢查。"""
        r = self.hook(env)
        got_action = r["actions"].get("*") or (r["actions"].get(vid) if vid else None)
        problems = []
        if r["verdict"] != expect:
            problems.append(f"裁決 {r['verdict']},預期 {expect}")
        if action and got_action not in (action if isinstance(action, tuple) else (action,)):
            problems.append(f"{vid} 的動作是 {got_action},預期 {action}")
        if reason_has and reason_has not in r["reason"]:
            problems.append(f"原因沒有出現 {reason_has!r}")
        # 「有排程重跑」不等於「真的驗過」:工具缺失 / 逾時 / 執行錯誤也會被預設放行(on_missing=warn),
        # 那種放行不能算通過。重跑的那一步,驗證器的結果必須是真的 pass 或 fail。
        status = r["results"].get(vid) if vid else None
        if got_action == "run" and status not in ("pass", "fail"):
            problems.append(f"{vid} 的結果是 {status},不是真的驗過")
        unverified = sorted(k for k, v in r["results"].items() if v in ("missing", "timeout", "error"))
        if unverified:
            problems.append(f"有驗證器沒有真的驗:{unverified}")
        excerpt = next((ln.strip() for ln in r["reason"].splitlines() if reason_has and reason_has in ln), "")
        if not excerpt and r["reason"]:
            excerpt = " / ".join(ln.strip() for ln in r["reason"].splitlines()[1:3])
        self.steps.append({"step": label, "expect": expect, "verdict": r["verdict"], "action": got_action,
                           "status": status,
                           "sec": r["sec"], "ok": not problems, "problems": problems,
                           "reason": excerpt[:220], "all_actions": r["actions"], "results": r["results"]})
        mark = "OK  " if not problems else "FAIL"
        print(f"  [{mark}] {label}: {r['verdict']} / {vid}={got_action}:{status} / {r['sec']}s"
              + (f"  ← {'; '.join(problems)}" if problems else ""), flush=True)
        return r

    def report(self, status=None, why=None):
        if status is None:
            status = "通過" if self.steps and all(s["ok"] for s in self.steps) else "失敗"
        return {"suite": self.name, "status": status, "why": why, "versions": self.versions,
                "steps": self.steps, "setup": self.setup, "notes": self.notes}


def extract(archive, dest):
    with tarfile.open(archive) as t:
        if sys.version_info >= (3, 12):
            t.extractall(dest, filter="data")
        else:
            t.extractall(dest)


def download(url, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)
    return dest


# ---------------------------------------------------------------- Python:venv + pip + pytest

PY_TEST = '''import {mod}
from pkg.core import add


def test_add():
    assert add(2, 3) == 5


def test_dependency_version():
    assert {mod}.__version__ == "{ver}"
'''


def suite_python_venv(work, args):
    s = Suite("python_venv", work)
    py_a, py_b = args.py_a, args.py_b
    write(s.proj, "pkg/__init__.py", "")
    write(s.proj, "pkg/core.py", "def add(a, b):\n    return a + b\n")
    write(s.proj, "tests/test_core.py", PY_TEST.format(mod="packaging", ver="24.0"))
    venv = s.proj / ".venv"
    vpy = venv / ("Scripts/python.exe" if WINDOWS else "bin/python")
    s.sh([py_a, "-m", "venv", venv])
    s.sh([vpy, "-m", "pip", "install", "-q", "pytest==8.3.3", "packaging==24.0"])
    s.version("venv python", [vpy, "-VV"])
    s.version("pytest", [vpy, "-m", "pytest", "--version"])
    s.version("pip", [vpy, "-m", "pip", "--version"])
    s.step("初次驗證", "pass", "pytest", "run")
    s.step("無變更", "pass", "pytest", "fast")
    s.sh([vpy, "-m", "pip", "install", "-q", "packaging==24.1"])          # venv 內就地升級
    s.step("venv 內 pip 就地升級 packaging 24.0→24.1(專案檔沒動)", "block", "pytest", "run",
           reason_has="test_dependency_version")
    write(s.proj, "tests/test_core.py", PY_TEST.format(mod="packaging", ver="24.1"))
    s.step("測試改成新版本", "pass", "pytest", "run")
    write(s.proj, "pkg/core.py", "def add(a, b):\n    return a +\n")
    s.step("故意語法錯誤", "block", "python", "run", reason_has="pkg/core.py:2")
    write(s.proj, "pkg/core.py", "def add(a, b):\n    return a + b\n")
    s.step("修正", "pass", "pytest", "run")
    shutil.rmtree(venv)
    s.sh([py_b, "-m", "venv", venv])
    s.sh([vpy, "-m", "pip", "install", "-q", "pytest==8.3.3", "packaging==24.1"])
    s.version("venv python(重建後)", [vpy, "-VV"])
    s.step(f"重建 venv 換直譯器({Path(py_b).name})", "pass", "pytest", "run")
    return s.report()


def suite_python_system_usersite(work, args):
    sysp = args.py_system
    userbase = Path(work) / "python_system_usersite" / "userbase"
    s = Suite("python_system_usersite", work, env={"PYTHONUSERBASE": str(userbase)}, hook_py=sysp)
    write(s.proj, "pkg/__init__.py", "")
    write(s.proj, "pkg/core.py", "def add(a, b):\n    return a + b\n")
    write(s.proj, "tests/test_core.py", PY_TEST.format(mod="toolz", ver="0.12.0"))
    pipx = [sysp, "-m", "pip", "install", "-q", "--user", "--break-system-packages", "--no-warn-script-location"]
    s.sh(pipx + ["pytest==8.3.3", "toolz==0.12.0"])   # pytest 也要在隔離的使用者目錄裡,否則會變成「工具缺失」
    s.version("system python", [sysp, "-VV"])
    s.version("pytest", [sysp, "-m", "pytest", "--version"])
    s.version("user site", [sysp, "-c", "import site; print(site.getusersitepackages(), site.ENABLE_USER_SITE)"])
    s.notes.append("沒有 venv:hook 用跑它自己的系統 Python 當專案直譯器;套件裝在隔離的 PYTHONUSERBASE")
    s.step("初次驗證", "pass", "pytest", "run")
    s.step("無變更", "pass", "pytest", "fast")
    s.sh(pipx + ["toolz==0.12.1"])
    s.step("pip install --user 升級 toolz 0.12.0→0.12.1(專案檔沒動)", "block", "pytest", "run",
           reason_has="test_dependency_version")
    write(s.proj, "tests/test_core.py", PY_TEST.format(mod="toolz", ver="0.12.1"))
    s.step("測試改成新版本", "pass", "pytest", "run")
    write(s.proj, "pkg/core.py", "def add(a, b)\n    return a + b\n")
    s.step("故意語法錯誤", "block", "python", "run", reason_has="pkg/core.py:1")
    write(s.proj, "pkg/core.py", "def add(a, b):\n    return a + b\n")
    s.step("修正", "pass", "pytest", "run")
    return s.report()


# ---------------------------------------------------------------- Node:npm / npx / node_modules / package-lock

NODE_TEST = '''const test = require("node:test");
const assert = require("node:assert");
const { newer } = require("../src/index.js");

test("newer", () => assert.ok(newer("1.2.4", "1.2.3")));
test("dependency version", () => assert.strictEqual(require("semver/package.json").version, "{ver}"));
'''


def suite_node(work, args):
    s = Suite("node", work)
    npm = shutil.which("npm")
    write(s.proj, "package.json", json.dumps({
        "name": "rt-node", "version": "1.0.0", "private": True,
        "scripts": {"test": "npx --no-install semver -r \">=7\" 7.6.0 && node --test"},
        "dependencies": {"semver": "7.6.0"}}, indent=2))
    write(s.proj, "src/index.js", 'const semver = require("semver");\nmodule.exports = { newer: (a, b) => semver.gt(a, b) };\n')
    write(s.proj, "test/index.test.js", NODE_TEST.replace("{ver}", "7.6.0"))
    s.sh([npm, "install", "--no-audit", "--no-fund", "--save-exact"])
    s.version("node", ["node", "--version"])
    s.version("npm", [npm, "--version"])
    s.step("初次驗證(npm test,內含 npx)", "pass", "node", "run")
    s.step("無變更", "pass", "node", "fast")
    before = (sha(s.proj / "package.json"), sha(s.proj / "package-lock.json"))
    s.sh([npm, "install", "--no-save", "--no-audit", "--no-fund", "semver@7.6.3"])
    after = (sha(s.proj / "package.json"), sha(s.proj / "package-lock.json"))
    s.notes.append(f"--no-save 前後 package.json / package-lock.json 雜湊:{before} → {after}")
    s.step("只有 node_modules 變了(npm install --no-save semver@7.6.3)", "block", "node", "run",
           reason_has="dependency version")
    s.sh([npm, "ci", "--no-audit", "--no-fund"])
    s.step("npm ci 依 package-lock 還原", "pass", "node", "run")
    s.sh([npm, "install", "--save-exact", "--no-audit", "--no-fund", "semver@7.6.3"])
    s.step("npm install 改版本(package.json + lock 都變)", "block", "node", "run", reason_has="dependency version")
    write(s.proj, "test/index.test.js", NODE_TEST.replace("{ver}", "7.6.3"))
    s.step("測試改成新版本", "pass", "node", "run")
    write(s.proj, "src/index.js", 'const semver = require("semver");\nmodule.exports = { newer: (a, b) => semver.gt(a, b) ;\n')
    s.step("故意語法錯誤", "block", "node", "run", reason_has="src/index.js:2")
    write(s.proj, "src/index.js", 'const semver = require("semver");\nmodule.exports = { newer: (a, b) => semver.gt(a, b) };\n')
    s.step("修正", "pass", "node", "run")
    if args.node_alt_url:
        d = s.dir / "node-alt"
        extract(download(args.node_alt_url, s.dir / "node-alt.tar.xz"), d)
        bindir = next(d.glob("node-*/bin"))
        env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        s.version("node(切換後)", ["node", "--version"], env=env)
        s.step("PATH 上的 node 換成另一個大版本", "pass", "node", "run", env=env)
    return s.report()


# ---------------------------------------------------------------- Rust:cargo + crates.io + rustup 切換

RUST_LIB = '''pub fn fmt(n: u64) -> String {
    let mut b = itoa::Buffer::new();
    b.format(n).to_owned()
}

/// get_disjoint_mut 在 Rust 1.86 才穩定:用它來證明「換 toolchain」真的會改變結果
pub fn bump_pair(v: &mut [i32]) -> i32 {
    let [a, b] = v.get_disjoint_mut([0, 1]).unwrap();
    *a += 1;
    *b += 1;
    *a + *b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn works() {
        assert_eq!(fmt(42), "42");
        let mut v = vec![1, 2];
        assert_eq!(bump_pair(&mut v), 5);
    }
}
'''


def suite_rust(work, args):
    base = Path(work) / "rust_iso"
    env = {"RUSTUP_HOME": str(base / "rustup"), "CARGO_HOME": str(base / "cargo"),
           "CARGO_TARGET_DIR": str(Path(work) / "rust" / "target-out")}
    s = Suite("rust", work, env=env)
    s.notes.append("RUSTUP_HOME / CARGO_HOME 都是隔離目錄,不動系統的 ~/.rustup")
    old, new = args.rust_old, args.rust_new
    s.sh(["rustup", "toolchain", "install", old, new, "--profile", "minimal", "--no-self-update"], timeout=3600)
    s.sh(["rustup", "default", new])
    s.version("rustup", ["rustup", "--version"])
    s.version("cargo", ["cargo", "--version"])
    s.version("rustc", ["rustc", "--version"])
    write(s.proj, "Cargo.toml", '[package]\nname = "rtrust"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                '[dependencies]\nitoa = "1.0.10"\n')
    write(s.proj, "src/lib.rs", RUST_LIB)
    s.sh(["cargo", "generate-lockfile"])
    s.sh(["cargo", "update", "-p", "itoa", "--precise", "1.0.10"])
    s.step(f"初次驗證(cargo test,toolchain {new})", "pass", "rust", "run")
    s.step("無變更", "pass", "rust", "fast")
    s.sh(["cargo", "update", "-p", "itoa", "--precise", "1.0.11"])
    s.step("cargo update 只改 Cargo.lock(itoa 1.0.10→1.0.11)", "pass", "rust", "run")
    write(s.proj, "src/lib.rs", RUST_LIB.replace("b.format(n).to_owned()", "let x: u32 = \"s\";\n    b.format(n).to_owned()"))
    s.step("故意編譯錯誤", "block", "rust", "run", reason_has="src/lib.rs:")
    write(s.proj, "src/lib.rs", RUST_LIB)
    s.step("修正", "pass", "rust", "run")
    s.sh(["rustup", "default", old])
    s.version("rustc(切換後)", ["rustc", "--version"])
    s.step(f"rustup default 換成 {old}(專案檔沒動;get_disjoint_mut 尚未穩定)", "block", "rust", "run",
           reason_has="get_disjoint_mut")
    s.sh(["rustup", "default", new])
    s.step(f"換回 {new}", "pass", "rust", ("run", "cached"))
    return s.report()


# ---------------------------------------------------------------- ShellCheck(含換版本)

def suite_shellcheck(work, args):
    s = Suite("shellcheck", work)
    s.version("shellcheck", ["shellcheck", "--version"])
    write(s.proj, "deploy.sh", "#!/bin/sh\necho $1\n")
    s.step("SC2086 未加引號", "block", "shell", "run", reason_has="deploy.sh:2:6")
    write(s.proj, "deploy.sh", '#!/bin/sh\necho "$1"\n')
    s.step("修正", "pass", "shell", "run")
    s.step("無變更", "pass", "shell", "fast")
    if args.shellcheck_alt_url:
        extract(download(args.shellcheck_alt_url, s.dir / "sc-alt.tar.xz"), s.dir / "sc-alt")
        bindir = next((s.dir / "sc-alt").glob("shellcheck-*"))
        env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        s.version("shellcheck(切換後)", ["shellcheck", "--version"], env=env)
        s.step("PATH 上的 shellcheck 換成另一版(檔案沒動)", "pass", "shell", "run", env=env)
        write(s.proj, "deploy.sh", "#!/bin/sh\necho $1\n")
        s.step("另一版也要擋得住 SC2086", "block", "shell", "run", reason_has="deploy.sh:2:6", env=env)
    return s.report()


# ---------------------------------------------------------------- CMake + GCC / Clang(自訂驗證 .claude/verify.sh)

CMAKE_FILES = {
    "CMakeLists.txt": "cmake_minimum_required(VERSION 3.16)\nproject(calc C)\nenable_testing()\n"
                      "add_library(calc src/calc.c src/gnu.c)\ntarget_include_directories(calc PUBLIC include)\n"
                      "add_executable(calc_test tests/calc_test.c)\ntarget_link_libraries(calc_test calc)\n"
                      "add_test(NAME calc_test COMMAND calc_test)\n",
    "include/calc.h": "#define CALC_BASE 0\nint add(int a, int b);\nint twice(int x);\n",
    "src/calc.c": '#include "calc.h"\nint add(int a, int b) { return a + b + CALC_BASE; }\n',
    # GNU C 的巢狀函式:gcc 編得過、clang 編不過 —— 用來證明「換編譯器」會改變結果
    "src/gnu.c": '#include "calc.h"\nint twice(int x) {\n    int inner(int y) { return y * 2; }\n    return inner(x);\n}\n',
    "tests/calc_test.c": '#include <stdio.h>\n#include "calc.h"\nint main(void) {\n'
                         '    if (add(2, 3) != 5) { printf("tests/calc_test.c:4: add(2,3)=%d\\n", add(2, 3)); return 1; }\n'
                         '    return twice(2) == 4 ? 0 : 1;\n}\n',
    ".claude/verify.sh": 'set -e\nCC="${CC:-gcc}"\nB="build/$(basename "$CC")"\n'
                         'cmake -S . -B "$B" -G Ninja -DCMAKE_C_COMPILER="$CC" >/dev/null\n'
                         'cmake --build "$B"\nctest --test-dir "$B" --output-on-failure\n',
}


def suite_cmake(work, args):
    s = Suite("cmake", work)
    for rel, text in CMAKE_FILES.items():
        write(s.proj, rel, text)
    for k, c in (("cmake", ["cmake", "--version"]), ("ninja", ["ninja", "--version"]), ("gcc", ["gcc", "--version"]),
                 ("clang", ["clang", "--version"])):
        s.version(k, c)
    s.notes.append("內建沒有 CMake 驗證器:照 README 的作法用 .claude/verify.sh 跑 cmake + ctest")
    s.step("初次驗證(預設 override_cache=false)", "pass", "override", "run")
    s.step("無變更:自訂驗證仍然每次都跑", "pass", "override", "run")
    write(s.proj, "include/calc.h", CMAKE_FILES["include/calc.h"].replace("CALC_BASE 0", "CALC_BASE 1"))
    s.step("改標頭檔讓測試失敗", "block", "override", "run", reason_has="tests/calc_test.c:4")
    write(s.proj, "include/calc.h", CMAKE_FILES["include/calc.h"])
    s.step("還原標頭檔", "pass", "override", "run")
    write(s.proj, "src/calc.c", '#include "calc.h"\nint add(int a, int b) { return a + b + ; }\n')
    s.step("故意編譯錯誤", "block", "override", "run", reason_has="src/calc.c:2")
    write(s.proj, "src/calc.c", CMAKE_FILES["src/calc.c"])
    s.step("修正", "pass", "override", "run")
    configure(s.proj, override_cache=True)
    s.step("改成 override_cache=true", "pass", "override", "run")
    s.step("override_cache=true + 無變更", "pass", "override", ("fast", "cached"))
    s.step("只改環境變數 CC=clang(clang 編不過 GNU 巢狀函式)", "block", "override", "run",
           reason_has="src/gnu.c", env={"CC": "clang"})
    s.step("CC 拿掉(回到 gcc)", "pass", "override", ("run", "cached"))
    return s.report()


# ---------------------------------------------------------------- PlatformIO native(事先準備的離線環境)

PIO_INI = "[env:native]\nplatform = native\n"
PIO_MAIN = '#include <stdio.h>\nint main(void) { printf("hi\\n"); return 0; }\n'


def suite_platformio(work, args):
    if not (args.pio_core and args.pio_venv):
        return {"suite": "platformio", "status": "未驗證", "why": "沒有提供 --pio-core / --pio-venv"}
    vbin = Path(args.pio_venv) / ("Scripts" if WINDOWS else "bin")
    env = {"PLATFORMIO_CORE_DIR": args.pio_core, "PATH": f"{vbin}{os.pathsep}{os.environ['PATH']}"}
    s = Suite("platformio", work, env=env)
    s.notes.append("PlatformIO registry 在這個環境連不到:platformio 本體來自 PyPI,native 平台來自 GitHub,"
                   "tool-scons 是用 PyPI 的 scons 手工組的套件(setup_offline_pio.sh)。編譯器是系統 gcc。")
    s.sh([vbin / "python", "-m", "pip", "install", "-q", f"platformio=={args.pio_base}"])   # 從已知狀態開始
    write(s.proj, "platformio.ini", PIO_INI)
    write(s.proj, "src/main.c", PIO_MAIN)
    s.version("pio", ["pio", "--version"])
    s.version("gcc", ["gcc", "--version"])
    s.step("初次驗證(pio run)", "pass", "firmware", "run")
    s.step("無變更", "pass", "firmware", "fast")
    write(s.proj, "src/main.c", PIO_MAIN.replace("return 0;", "return 0"))
    s.step("故意編譯錯誤", "block", "firmware", "run", reason_has="src/main.c:2")
    write(s.proj, "src/main.c", PIO_MAIN)
    s.step("修正", "pass", "firmware", "run")
    write(s.proj, "README.md", "# 與建置無關的檔案\n")
    s.step("改與建置無關的檔案", "pass", "firmware", "cached")
    s.sh([vbin / "python", "-m", "pip", "install", "-q", f"platformio=={args.pio_alt}"])
    s.version("pio(切換後)", ["pio", "--version"])
    # 這個環境連不到 PlatformIO registry:舊版要求的 tool-scons 版本不同、裝不到 → 建置真的失敗。
    # 要驗的是「換版本之後 hook 真的重跑、而且照實回報」,不是那個版本能不能建置。
    s.step(f"pip 換 PlatformIO 版本({args.pio_alt};要求另一版 tool-scons,離線裝不到)", "block", "firmware",
           "run", reason_has="PlatformIO")
    s.notes.append("換版本後的失敗原因(pio run 的輸出):tool-scons 版本需求不同、registry 連不到 → HTTPClientError")
    back = args.pio_base
    s.sh([vbin / "python", "-m", "pip", "install", "-q", f"platformio=={back}"])
    s.version("pio(換回)", ["pio", "--version"])
    s.step(f"換回 PlatformIO {back}", "pass", "firmware", ("run", "cached"))
    core = Path(args.pio_core)
    builder = core / "platforms" / "native" / "builder" / "main.py"
    if builder.exists():
        orig_b = builder.read_bytes()
        try:
            builder.write_bytes(orig_b + b"\nraise SystemExit('injected-by-real-toolchains')\n")
            s.step("直接改平台的建置腳本(專案檔沒動)", "block", "firmware", "run")
        finally:
            builder.write_bytes(orig_b)
        s.step("還原平台建置腳本", "pass", "firmware", ("run", "cached"))
    return s.report()


# ---------------------------------------------------------------- ESP-IDF(事先準備的環境)

def _env_from_script(path):
    out = subprocess.run(["sh", "-c", f'. "{path}" && env -0'], capture_output=True, check=True).stdout
    env = dict(x.split("=", 1) for x in out.decode().split("\0") if "=" in x)
    keep = ("IDF_", "PATH")
    return {k: v for k, v in env.items() if k.startswith(keep)}


def suite_esp_idf(work, args):
    if not args.idf_env:
        return {"suite": "esp_idf", "status": "未驗證", "why": "沒有提供 --idf-env"}
    env = _env_from_script(args.idf_env)
    s = Suite("esp_idf", work, env=env)
    idf = Path(env["IDF_PATH"])
    shutil.rmtree(s.proj)
    shutil.copytree(idf / "examples" / "get-started" / "hello_world", s.proj)
    write(s.proj, "sdkconfig.defaults", "CONFIG_IDF_TARGET=\"esp32\"\n")
    main = s.proj / "main" / "hello_world_main.c"
    good = main.read_text(encoding="utf-8")
    s.version("idf.py", ["idf.py", "--version"])
    s.version("xtensa gcc", ["xtensa-esp32-elf-gcc", "--version"])
    s.version("cmake", ["cmake", "--version"])
    s.version("ninja", ["ninja", "--version"])
    s.step("初次驗證(idf.py build,esp32)", "pass", "firmware", "run")
    s.step("無變更", "pass", "firmware", "fast")
    main.write_text(good.replace('printf("Hello world!\\n");', 'printf("Hello world!\\n")'), encoding="utf-8")
    s.step("故意編譯錯誤", "block", "firmware", "run", reason_has="main/hello_world_main.c:")
    main.write_text(good, encoding="utf-8")
    s.step("修正", "pass", "firmware", "run")
    write(s.proj, "sdkconfig.defaults", "CONFIG_IDF_TARGET=\"esp32\"\nCONFIG_LOG_DEFAULT_LEVEL_WARN=y\n")
    s.step("改 sdkconfig.defaults", "pass", "firmware", "run")
    write(s.proj, "notes.txt", "與建置無關\n")
    s.step("改與建置無關的檔案", "pass", "firmware", "cached")
    # 直接改 ESP-IDF 本身(專案外):使用者在本機修補 IDF 元件的情境
    victim = idf / "components" / "log" / "include" / "esp_log.h"
    orig = victim.read_bytes()
    try:
        victim.write_bytes(orig + b"\n#error injected-by-real-toolchains\n")
        s.step("直接改 ESP-IDF 元件的標頭檔(專案檔沒動)", "block", "firmware", "run",
               reason_has="error: #error injected-by-real-toolchains")
    finally:
        victim.write_bytes(orig)
    s.step("還原 ESP-IDF", "pass", "firmware", "run")
    return s.report()


SUITES = {
    "python_venv": suite_python_venv,
    "python_system_usersite": suite_python_system_usersite,
    "node": suite_node,
    "rust": suite_rust,
    "shellcheck": suite_shellcheck,
    "cmake": suite_cmake,
    "platformio": suite_platformio,
    "esp_idf": suite_esp_idf,
}


def to_markdown(results):
    lines = ["# 真實工具鏈驗收結果", ""]
    for r in results:
        lines += [f"## {r['suite']}:{r['status']}", ""]
        if r.get("why"):
            lines += [f"原因:{r['why']}", ""]
        if r.get("versions"):
            lines += ["版本:", ""] + [f"- {k}:`{v}`" for k, v in r["versions"].items()] + [""]
        for n in r.get("notes", []):
            lines.append(f"- 註:{n}")
        if r.get("steps"):
            lines += ["", "| 步驟 | 預期 | 實際 | 動作:結果 | 秒 | 原因摘錄 | 判定 |", "|---|---|---|---|---|---|---|"]
            for st in r["steps"]:
                reason = st["reason"].replace("|", "\\|")
                verdict = "OK" if st["ok"] else "FAIL:" + ";".join(st["problems"])
                lines.append(f"| {st['step']} | {st['expect']} | {st['verdict']} | {st['action']}:{st['status']} | {st['sec']} | "
                             f"{reason} | {verdict} |")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--hook", default=None, help="改用另一份 verify.py(比較修正前後)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--py-a", default="python3.12")
    ap.add_argument("--py-b", default="python3.11")
    ap.add_argument("--py-system", default=sys.executable)
    ap.add_argument("--rust-old", default="1.85.0")
    ap.add_argument("--rust-new", default="1.86.0")
    ap.add_argument("--node-alt-url", default="https://nodejs.org/dist/v20.18.0/node-v20.18.0-linux-x64.tar.xz")
    ap.add_argument("--shellcheck-alt-url", default="https://github.com/koalaman/shellcheck/releases/download/"
                                                     "v0.10.0/shellcheck-v0.10.0.linux.x86_64.tar.xz")
    ap.add_argument("--pio-core")
    ap.add_argument("--pio-venv")
    ap.add_argument("--pio-base", default="6.2.0")
    ap.add_argument("--pio-alt", default="6.1.18")
    ap.add_argument("--idf-env")
    args = ap.parse_args()
    if args.hook:
        global HOOK
        HOOK = Path(args.hook).resolve()
    print("hook:", HOOK, flush=True)
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    names = [n for n in (args.only.split(",") if args.only else SUITES)]
    results = []
    for n in names:
        print(f"== {n}", flush=True)
        try:
            results.append(SUITES[n](work, args))
        except SetupError as e:
            results.append({"suite": n, "status": "未驗證", "why": f"準備環境失敗:{e}"})
        except Exception as e:  # noqa: BLE001
            results.append({"suite": n, "status": "錯誤", "why": "".join(traceback.format_exception_only(type(e), e)),
                            "trace": traceback.format_exc()[-2000:]})
        print(f"   → {results[-1]['status']} {results[-1].get('why') or ''}", flush=True)
    out = Path(args.out) if args.out else work / "results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(results), encoding="utf-8")
    print("寫入", out, "與", out.with_suffix(".md"))
    return 0 if all(r["status"] == "通過" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
