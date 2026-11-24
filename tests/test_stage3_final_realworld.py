"""Stage 3 Final Acceptance — 真實專案驗收(隔離環境、無網路)抓到的問題。

1. node_modules/.cache:xo、ava、eslint、babel、terser 都把自己的快取寫在這裡(find-cache-dir 慣例)。
   它每次跑測試都會變 → 相依目錄的簽章每次都不同 → 每次 Stop 都重跑整套 npm test。
   實測 chalk v6.0.1:無變更的 Stop 每次 9 秒以上。
"""

import json

from conftest import FAKE_IMPL, run_hook, write  # noqa: F401


def test_tool_cache_inside_node_modules_does_not_force_a_rerun(project, fake, tmp_path):
    # 假 npm 每次執行都往 node_modules/.cache 寫一個新檔(xo-linter / ava 的行為)
    fake.make("npm")
    launcher = fake.launcher("npm")
    impl = tmp_path / "npm_with_cache.py"
    impl.write_text(
        "import os, sys, time\n"
        f"os.makedirs({str(project / 'node_modules' / '.cache' / 'xo-linter')!r}, exist_ok=True)\n"
        f"open(os.path.join({str(project / 'node_modules' / '.cache' / 'xo-linter')!r}, str(time.time_ns())), 'w').write('x')\n"
        f"with open({str(fake.log)!r}, 'a') as fh: fh.write('npm start 0\\n')\n", encoding="utf-8")
    import sys  # noqa: PLC0415
    if launcher.suffix == ".cmd":
        launcher.write_text(f'@"{sys.executable}" "{impl}" %*\r\n@exit /b 0\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
    write(project, "package.json", json.dumps({"scripts": {"test": "x"}}))
    write(project, "node_modules/left-pad/index.js", "module.exports = 1;\n")
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    assert fake.runs("npm") == 1, "只有 node_modules/.cache 變了,不該重跑"


def test_real_dependency_change_next_to_the_cache_still_reruns(project, fake):
    fake.make("npm")
    write(project, "package.json", json.dumps({"scripts": {"test": "x"}}))
    dep = write(project, "node_modules/left-pad/index.js", "module.exports = 1;\n")
    write(project, "node_modules/.cache/xo-linter/a", "x")
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    dep.write_text("module.exports = 22;\n", encoding="utf-8")
    run_hook(project, env=fake.env)
    assert fake.runs("npm") == 2


def test_a_package_literally_named_cache_is_still_tracked(project, fake):
    # 只排除 node_modules 最上層的 .cache 目錄;套件內的 .cache 或名叫 cache 的套件照常追蹤
    fake.make("npm")
    write(project, "package.json", json.dumps({"scripts": {"test": "x"}}))
    f = write(project, "node_modules/some-pkg/.cache/data.js", "module.exports = 1;\n")
    assert not run_hook(project, env=fake.env).blocked
    assert not run_hook(project, env=fake.env).blocked
    f.write_text("module.exports = 2;\n", encoding="utf-8")
    run_hook(project, env=fake.env)
    assert fake.runs("npm") == 2
