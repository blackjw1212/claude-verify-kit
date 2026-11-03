"""內建驗證器的正確性回歸:每一條都是舊版實測會誤擋或漏擋的情境。"""

import shutil

import pytest

from conftest import run_hook, write


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


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck 未安裝")
def test_shellcheck_with_cjk_path_reports_location(tmp_path):
    proj = tmp_path / "殼層 腳本"
    proj.mkdir()
    write(proj, "部署.sh", "#!/bin/sh\necho $1\n")
    r = run_hook(proj)
    assert r.blocked
    assert "部署.sh:2:" in r.reason and "SC2086" in r.reason
