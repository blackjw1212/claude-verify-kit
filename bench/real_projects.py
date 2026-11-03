#!/usr/bin/env python3
"""真實專案驗收(Stage 3-5)。每個專案都在隔離副本上做,原始目錄一個位元組都不動。

專案:
  site        使用者的網站 repo(有自己的 .claude/verify.sh);副本不含 .git
  python      pallets/itsdangerous(venv + pip install -e . + pytest)
  node        chalk/chalk(npm install,node_modules 數萬檔;npm test = xo + node --test + tsc)
  cmake       DaveGamble/cJSON(自訂驗證跑 cmake + ctest)
  platformio  platformio/platform-native 的 examples/hello-world(離線環境)

用法:
  python3 bench/real_projects.py --work DIR --src DIR [--site PATH] [--only site,python]
    --src 底下要有 itsdangerous、chalk、cJSON、platform-native 的 clone
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import real_toolchains as rt  # noqa: E402
from real_toolchains import Suite, SetupError, write  # noqa: E402


def _copy(src, dst, ignore=(".git",)):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*ignore), symlinks=True)


def _edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= 1, (path, old)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return text


def _tree_size(path):
    n, size = 0, 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            try:
                size += os.lstat(os.path.join(dp, f)).st_size
                n += 1
            except OSError:
                pass
    return n, size


# ---------------------------------------------------------------- 使用者的網站

def suite_site(work, args):
    s = Suite("site", work)
    _copy(Path(args.site), s.proj)
    s.notes.append(f"副本:{args.site} → {s.proj}(不含 .git;原始目錄沒有任何寫入)")
    s.version("node", ["node", "--version"])
    s.version("npm", ["npm", "--version"])
    n, size = _tree_size(s.proj)
    s.notes.append(f"副本 {n} 個檔案、{size / 1e6:.1f} MB")
    s.step("初次 Stop:跑 .claude/verify.sh", "pass", "override", "run")
    s.step("無變更:verify.sh 照樣每次都跑(預設 override_cache=false)", "pass", "override", "run")
    feed = s.proj / "data" / "etf-feed.json"
    orig = feed.read_bytes()
    feed.write_bytes(orig[: len(orig) // 2])
    s.step("JSON:把 data/etf-feed.json 截斷一半", "block", "override", "run", reason_has="etf-feed")
    feed.write_bytes(orig)
    s.step("還原 JSON", "pass", "override", "run")
    js = s.proj / "scripts" / "check-static-site.mjs"
    js_orig = _edit(js, "\n", "\nconst = ;\n")
    s.step("JS:scripts/check-static-site.mjs 語法錯誤", "block", "override", "run",
           reason_has="check-static-site.mjs")
    js.write_text(js_orig, encoding="utf-8")
    s.step("還原 JS", "pass", "override", "run")
    py = s.proj / ".claude" / "hooks" / "loop-gate.py"
    py_orig = py.read_text(encoding="utf-8")
    py.write_text(py_orig + "\ndef broken(:\n", encoding="utf-8")
    # Final Acceptance 起預設 custom_verify_mode=augment:verify.sh 照跑,內建的 Python 語法檢查也跑
    s.step("Python:.claude/hooks/loop-gate.py 語法錯誤(verify.sh 不檢查 Python,內建檢查擋下)", "block", "python",
           "run", reason_has=".claude/hooks/loop-gate.py:")
    rt.configure(s.proj, custom_verify_mode="replace")
    s.step("同一個錯誤 + 明確設定 custom_verify_mode=replace(舊行為:只跑 verify.sh)", "pass", "override", "run")
    (s.proj / ".claude" / "verify.json").unlink()
    py.write_text(py_orig, encoding="utf-8")
    settings = s.proj / ".claude" / "settings.json"
    st_orig = settings.read_text(encoding="utf-8")
    settings.write_text(st_orig.replace("loop-gate.py", "loop-gate-missing.py", 1), encoding="utf-8")
    s.step("hooks 設定指向不存在的腳本", "block", "hooks", "run", reason_has="loop-gate-missing.py")
    settings.write_text(st_orig, encoding="utf-8")
    s.step("還原 hooks 設定", "pass", "hooks", "run")
    configure = rt.configure
    configure(s.proj, max_lines=10)
    s.step("設定檔:新增 .claude/verify.json", "pass", "override", "run")
    configure(s.proj, override_cache=True)
    s.step("改成 override_cache=true", "pass", "override", "run")
    s.step("override_cache=true + 無變更 → 沿用", "pass", "override", ("fast", "cached"))
    feed.write_bytes(orig[: len(orig) // 2])
    s.step("override_cache=true + JSON 截斷", "block", "override", "run", reason_has="etf-feed")
    feed.write_bytes(orig)
    s.step("還原", "pass", "override", "run")
    # 大型 node_modules:合成 40,000 檔(2,000 個套件 × 20 檔),放在 backend/ 底下
    nm = s.proj / "backend" / "node_modules"
    t0 = time.monotonic()
    for i in range(2000):
        d = nm / f"pkg{i:04d}" / "lib"
        d.mkdir(parents=True)
        for j in range(19):
            (d / f"f{j}.js").write_text(f"module.exports={i * 100 + j};\n")
        (nm / f"pkg{i:04d}" / "package.json").write_text('{"name":"p%d","version":"1.0.0"}' % i)
    s.notes.append(f"合成 node_modules 40,000 檔,建立耗時 {time.monotonic() - t0:.1f} 秒")
    s.step("override_cache=true + 新增 4 萬檔 node_modules(相依變了)", "pass", "override", "run")
    s.step("override_cache=true + 4 萬檔 node_modules + 無變更", "pass", "override", ("fast", "cached"))
    configure(s.proj, override_cache=False)
    s.step("override_cache=false + 4 萬檔 node_modules", "pass", "override", "run")
    s.step("override_cache=false + 4 萬檔 node_modules + 無變更", "pass", "override", "run")
    return s.report()


# ---------------------------------------------------------------- Python:itsdangerous

def suite_python(work, args):
    s = Suite("python_itsdangerous", work)
    _copy(Path(args.src) / "itsdangerous", s.proj)
    venv = s.proj / ".venv"
    vpy = venv / ("Scripts/python.exe" if rt.WINDOWS else "bin/python")
    s.sh([args.py, "-m", "venv", venv])
    s.sh([vpy, "-m", "pip", "install", "-q", "-e", ".", "pytest", "freezegun"], timeout=1800)
    s.version("python", [vpy, "-VV"])
    s.version("pytest", [vpy, "-m", "pytest", "--version"])
    n, _ = _tree_size(venv)
    s.notes.append(f".venv {n} 個檔案(pytest 的環境簽章會逐檔掃它)")
    s.step("初次驗證(pytest)", "pass", "pytest", "run")
    s.step("無變更", "pass", "pytest", "fast")
    src = s.proj / "src" / "itsdangerous" / "signer.py"
    orig = _edit(src, "\nclass ", "\nclass Broken(:\n    pass\n\nclass ")
    s.step("原始碼語法錯誤", "block", "python", "run", reason_has="src/itsdangerous/signer.py:")
    src.write_text(orig, encoding="utf-8")
    s.step("修正", "pass", "pytest", "run")
    test = next((s.proj / "tests").rglob("test_encoding.py"))
    t_orig = test.read_text(encoding="utf-8")
    test.write_text(t_orig + "\n\ndef test_injected_failure():\n    assert 1 + 1 == 3\n", encoding="utf-8")
    s.step("測試失敗", "block", "pytest", "run", reason_has="test_injected_failure")
    test.write_text(t_orig, encoding="utf-8")
    s.step("還原測試", "pass", "pytest", "run")
    write(s.proj, "docs/extra.rst", "說明文件\n")
    s.step("只改文件(pytest 是 any 模式:任何檔案變了都重跑)", "pass", "pytest", "run")
    s.sh([vpy, "-m", "pip", "install", "-q", "freezegun==1.4.0"])
    s.step("venv 裡就地換 freezegun 版本(專案檔沒動)", "pass", "pytest", "run")
    return s.report()


# ---------------------------------------------------------------- Node:chalk

def suite_node(work, args):
    s = Suite("node_chalk", work)
    _copy(Path(args.src) / "chalk", s.proj)
    s.sh(["npm", "install", "--no-audit", "--no-fund"], timeout=1800)
    s.version("node", ["node", "--version"])
    s.version("npm", ["npm", "--version"])
    n, size = _tree_size(s.proj / "node_modules")
    s.notes.append(f"node_modules {n} 個檔案、{size / 1e6:.1f} MB(真實安裝)")
    s.step("初次驗證(npm test = xo + c8 node --test + tsc)", "pass", "node", "run")
    s.step("無變更(要逐檔掃 node_modules 的簽章)", "pass", "node", "fast")
    s.step("無變更(第二次)", "pass", "node", "fast")
    src = s.proj / "source" / "index.js"
    orig = _edit(src, "\nconst ", "\nconst = ;\nconst ")
    s.step("原始碼語法錯誤", "block", "node", "run", reason_has="source/index.js")
    src.write_text(orig, encoding="utf-8")
    s.step("修正", "pass", "node", "run")
    test = s.proj / "test" / "chalk.js"
    t_orig = test.read_text(encoding="utf-8")
    test.write_text(t_orig + "\ntest('injected failure', () => {\n\tassert.equal(1 + 1, 3);\n});\n", encoding="utf-8")
    s.step("測試失敗", "block", "node", "run", reason_has="injected failure")
    test.write_text(t_orig, encoding="utf-8")
    s.step("還原測試", "pass", "node", "run")
    return s.report()


# ---------------------------------------------------------------- CMake:cJSON

CJSON_VERIFY = ('set -e\nB=build/verify\n'
                'cmake -S . -B "$B" -G Ninja -DENABLE_CJSON_TEST=On -DENABLE_CJSON_UTILS=On >/dev/null\n'
                'cmake --build "$B"\nctest --test-dir "$B" --output-on-failure\n')


def suite_cmake(work, args):
    s = Suite("cmake_cjson", work)
    _copy(Path(args.src) / "cJSON", s.proj)
    write(s.proj, ".claude/verify.sh", CJSON_VERIFY)
    s.notes.append("內建沒有 CMake 驗證器:用 .claude/verify.sh 跑 cmake + ctest(預設每次都跑)")
    for k, c in (("cmake", ["cmake", "--version"]), ("ninja", ["ninja", "--version"]), ("gcc", ["gcc", "--version"])):
        s.version(k, c)
    s.step("初次驗證(完整建置 + ctest)", "pass", "override", "run")
    s.step("無變更(增量建置 + ctest)", "pass", "override", "run")
    src = s.proj / "cJSON.c"
    orig = _edit(src, "\nCJSON_PUBLIC(const char*) cJSON_Version(void)", "\nint broken = ;\nCJSON_PUBLIC(const char*) cJSON_Version(void)")
    s.step("cJSON.c 編譯錯誤", "block", "override", "run", reason_has="cJSON.c:")
    src.write_text(orig, encoding="utf-8")
    s.step("修正", "pass", "override", "run")
    t = s.proj / "tests" / "parse_number.c"
    t_orig = _edit(t, "assert_parse_number(\"0\", 0, 0);", "assert_parse_number(\"0\", 1, 1);")
    s.step("測試斷言失敗", "block", "override", "run", reason_has="parse_number")
    t.write_text(t_orig, encoding="utf-8")
    s.step("還原測試", "pass", "override", "run")
    return s.report()


# ---------------------------------------------------------------- PlatformIO:platform-native 的 hello-world

def suite_platformio(work, args):
    if not (args.pio_core and args.pio_venv):
        return {"suite": "platformio_hello", "status": "未驗證", "why": "沒有提供 --pio-core / --pio-venv"}
    vbin = Path(args.pio_venv) / ("Scripts" if rt.WINDOWS else "bin")
    s = Suite("platformio_hello", work,
              env={"PLATFORMIO_CORE_DIR": args.pio_core, "PATH": f"{vbin}{os.pathsep}{os.environ['PATH']}"})
    _copy(Path(args.src) / "platform-native" / "examples" / "hello-world", s.proj)
    s.version("pio", ["pio", "--version"])
    s.step("初次驗證(pio run)", "pass", "firmware", "run")
    s.step("無變更", "pass", "firmware", "fast")
    main = next(p for p in (s.proj / "src").iterdir() if p.suffix in (".c", ".cpp"))
    orig = main.read_text(encoding="utf-8")
    main.write_text(orig + "\nint broken = ;\n", encoding="utf-8")
    s.step("編譯錯誤", "block", "firmware", "run", reason_has=f"src/{main.name}:")
    main.write_text(orig, encoding="utf-8")
    s.step("修正", "pass", "firmware", "run")
    write(s.proj, "README.md", "# 與建置無關\n")
    s.step("改 README", "pass", "firmware", "cached")
    return s.report()


SUITES = {"site": suite_site, "python": suite_python, "node": suite_node, "cmake": suite_cmake,
          "platformio": suite_platformio}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--site", default=None)
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=None)
    ap.add_argument("--py", default="python3.12")
    ap.add_argument("--pio-core")
    ap.add_argument("--pio-venv")
    args = ap.parse_args()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    names = args.only.split(",") if args.only else list(SUITES)
    results = []
    for n in names:
        print(f"== {n}", flush=True)
        if n == "site" and not args.site:
            results.append({"suite": "site", "status": "未驗證", "why": "沒有提供 --site"})
            continue
        try:
            results.append(SUITES[n](work, args))
        except SetupError as e:
            results.append({"suite": n, "status": "未驗證", "why": f"準備環境失敗:{e}"})
        print(f"   → {results[-1]['status']} {results[-1].get('why') or ''}", flush=True)
    out = Path(args.out) if args.out else work / "results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    out.with_suffix(".md").write_text(rt.to_markdown(results).replace("真實工具鏈驗收結果", "真實專案驗收結果"),
                                      encoding="utf-8")
    print("寫入", out)
    return 0 if all(r["status"] == "通過" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
