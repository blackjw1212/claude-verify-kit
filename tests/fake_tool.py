"""測試用的假外部工具(跨平台)。由 conftest.FakeTools 產生的啟動器呼叫:
    python fake_tool.py <bindir>/<name>.fake.json [args...]
行為由那份 JSON 決定:sleep 秒數、輸出內容與編碼、exit code。
直接收設定檔路徑而不是去 PATH 上找,所以測試把 PATH 縮到最小時假工具照樣能跑。"""
import json
import os
import sys
import time

cfg_path = sys.argv[1]
name = os.path.basename(cfg_path)[:-len(".fake.json")]
with open(cfg_path, encoding="utf-8") as fh:
    cfg = json.load(fh)
log = os.environ.get("FAKE_TOOL_LOG")


def mark(kind):
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{name} {kind} {time.time():.6f}\n")


mark("start")
time.sleep(float(cfg.get("sleep", 0)))
out = cfg.get("output", "")
if out:
    # 直接寫位元組:輸出編碼由測試指定(utf-8 / cp950 …),不受執行環境的 locale 影響,
    # 才測得到 hook 自己的解碼邏輯。
    sys.stdout.buffer.write((out + "\n").encode(cfg.get("encoding", "utf-8")))
    sys.stdout.flush()
mark("end")
sys.exit(int(cfg.get("rc", 0)))
