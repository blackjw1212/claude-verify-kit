# claude-verify-kit

讓 Claude Code 在「專案無法通過驗證」前**無法宣告完成**的 Stop hook 閘門,搭配一份跨專案的 `CLAUDE.md` 工作契約。

對齊 Anthropic 官方原則:最高槓桿不是「跑測試」,而是給 agent **可驗證的成功標準**。本套件依專案內實際存在的檔案,自動派發對應檢查 —— 任一項失敗即擋下 Claude，直到修好。

## 涵蓋的驗證維度

| 產物 | 可驗證綠燈 | 工具 |
|---|---|---|
| 韌體 ESP32 / S3 / WROOM | 編譯/連結通過 | `idf.py build` / `pio run` / `arduino-cli compile` |
| Hackintosh OpenCore | plist 合法 + 符合 OC 規範 | `plutil -lint` + `ocvalidate`（版本須對應 OC release） |
| ACPI / SSDT | 可編譯成 .aml | `iasl` |
| Gmail 過濾 / 一般 XML | well-formed | Python 內建（免裝） |
| AdGuard / adblock 規則 | 語法合法 | `aglint`（需 `.aglintrc`，含 error-token 保險） |
| Excel xlsx | 可載入不損毀 | `openpyxl` |
| Shell / Python / Node | lint / 編譯 / 測試 | `shellcheck` / `py_compile`+`pytest` / `npm test` |

工具或設定缺失 → 該維度**軟性略過**（印警告不擋）。結構合法 **≠** 行為正確；硬體與規則的實際效果仍須上機/實測。

## 安裝

兩種擇一，**勿同時用**（會跑兩次）。

### A. 全域（所有專案統一，不進版控）
```bash
./install-global.sh
```
裝到 `~/.claude/`，hook 指向 `~/.claude/hooks/verify.py`。

### B. 專案層（跟著 repo 走，clone 即生效）
把本 repo 的 `CLAUDE.md` 與 `.claude/` 複製進你的專案，或用：
```bash
./apply-to-repo.sh /path/to/your/repo            # 只套用 + commit
./apply-to-repo.sh --push /path/to/your/repo     # 套用 + commit + push（用你自己的 git 認證）
```
專案層的 `.claude/settings.json` 已把 hook 指向 `$CLAUDE_PROJECT_DIR/.claude/hooks/verify.py`。

> Windows：把 `settings.json` 與安裝腳本中的 `python3` 改成 `python`。

### C. 全機掃描（新專案漏套、kit 更新後 repo 沒跟上，都靠這支）

```bash
./scan-and-apply.sh                 # 只掃描回報，不動任何東西（預設）
./scan-and-apply.sh --apply         # 套用 + commit
./scan-and-apply.sh --apply --push  # 套用 + commit + push
./scan-and-apply.sh --root ~/work   # 自訂掃描根目錄（可重複）
```

掃出所有 git repo 並分類 **NO_KIT**（沒套）/ **STALE**（hooks 版本落後 kit）/ **CURRENT**（略過），
套用後自動驗收:hooks 同版、可編譯、`settings.json` 沒被 gitignore 誤擋。

> 為什麼需要:新 repo 不會自己套，kit 修完 bug 各 repo 也不會自己更新。
> 實際發生過:兩個新專案完全沒套，且兩個 repo 的閘門因 kit bug 長期假綠。
> **建議固定週期跑一次 dry-run。**

## 自訂驗證標準（不被內建項目侷限）

任一專案放 `.claude/verify`（`.sh` / `.py` / 可執行檔），閘門就**只認它的 exit code**，內建項目全部讓位。適合把專屬成功標準寫死，例如：
- Gmail 過濾 XML：「無重複/衝突 entry」
- LINE / AdGuard ruleset：「核心 gateway host 沒被誤擋」

## 逃生口

硬體專屬或半成品狀態要讓 Claude 正常收尾：
```bash
touch .claude/skip-verify   # 用完刪掉
```

## 運作機制

Stop hook 從 stdin 讀事件 JSON，失敗時回 `{"decision":"block","reason":...}`（Stop 事件用 top-level `decision`/`reason`，非 `hookSpecificOutput`）讓 Claude 續跑修正；`.claude/verify.json` 設 `"block_mode": "exit2"` 則改用 exit 2 + stderr。

防無限迴圈（`verify.py`）：
- Claude Code 在 hook 擋下後續跑時，下一次 Stop 的 `stop_hook_active` 為 true。此時若**上次擋下之後專案沒有任何修改**（檔案清單＋大小＋mtime 的指紋相同），hook 直接放行並用 `systemMessage` 告知使用者；**有修改**才重新驗證，仍失敗就再擋。
- 同一輪最多連續擋 `max_consecutive_blocks` 次（預設 5），之後放行交還給人。
- 舊行為（`stop_hook_active` 一律放行）可用 `"rerun_when_active": false` 找回。

> 更正：舊版 README 寫「Claude Code 並無固定的連續 block 上限」——與官方文件不符。官方 Stop 章節寫明 Claude Code 有**連續 8 次續跑**的上限（可用 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` 調整），每次 Claude 呼叫工具就重新計數。hook 自己檢查仍然必要：那個上限只是最後一道保險。

工具不存在、逾時、真正失敗三者分開處理：
- **真正失敗**：擋。
- **逾時**：預設也擋——逾時代表沒驗完，絕不算通過。只有在 `.claude/verify.json` 明確設 `"on_timeout": "warn"`（全域或 `validators.<id>`）才改成只警告。逾時的工具、指令、執行秒數、上限、成因（驗證器上限／整體預算）與處置（已終止程序樹／未執行）記在 `.claude/.verify.log`，並標為疑點。
- **工具不存在**：預設只警告（`systemMessage` 讓使用者看得到），`"on_missing": "block"` 可改成擋。注意：自訂驗證 `.claude/verify.sh` 的直譯器（`sh`）不存在時，整份 DoD 都沒跑；要嚴格請設 `validators.override.on_missing: "block"`。

## 只驗變更的部分、快取、平行

- **變更怎麼判定**：比對檔案內容雜湊（存在 `.claude/.verify_cache.json`），不是 transcript 也不是 git。transcript 看不到 Bash 改的檔（`sed -i`、腳本、`git checkout`），官方文件也說它在 Stop 當下可能還沒寫完；git status 列的是所有未 commit 的變更而非這一輪的，也看不到被 `.gitignore` 排除、但驗證器照樣會檢查的檔案。只 `touch`、內容沒變不算變更。
- **逐檔驗證器**（JSON、XML、plist、ACPI、xlsx、shellcheck、Python 語法）：只驗內容變了的檔案，其餘沿用上次的結果——**包括上次的失敗**，沒修好的檔照樣擋。快取鍵 = 檔案內容雜湊 + 驗證器版本 + 工具識別（路徑、大小、mtime；shellcheck 另含 `.shellcheckrc`）。
- **專案層級驗證器**（建置、測試、自訂驗證）：它的輸入變了才重跑，**只快取通過**（失敗可能是環境問題，裝好套件後檔案沒變也必須重跑）。
  - 韌體：原始碼、標頭、建置設定、`EMBED_FILES`／`board_build.embed_*` 宣告的檔案。遇到 `file(GLOB)`、`configure_file`、PlatformIO `extra_scripts`、含變數的嵌入路徑等看不出輸入的寫法，就改成任何變更都重建；任何檔案被刪除也重建。
  - pytest、npm、cargo、aglint、`.claude/verify*`：任何檔案變更都重跑（它們可能讀任何檔案）。
- **平行**：彼此獨立的驗證器同時跑，最多 `max_parallel` 個（預設 min(4, CPU 數)），其中重型（韌體、測試、Node、Rust、自訂驗證）最多 `max_heavy` 個（預設 1），避免韌體建置吃滿 CPU。`max_parallel: 1` = 完全依序。
- **已知限制**：內容改了、但大小與 mtime 都被刻意還原成很久以前的時間（例如 `cp -p` 一個同大小的舊檔），預設模式看不到（與 git index 相同）。要嚴格請設 `"trust_mtime": false`（每次重讀全部檔案）。工具以外的環境變化（例如 `pip install` 進全域 Python）不在快取鍵裡；venv 的 site-packages、`node_modules`、`.pio/libdeps`、`managed_components` 的異動有算進去。
- **相關設定**（`.claude/verify.json`）：`max_parallel`、`max_heavy`、`cache`（false = 每次全驗）、`override_cache`（false = 自訂驗證每次都跑，即 Stage 1 行為）、`trust_mtime`、`debug`（或環境變數 `CLAUDE_VERIFY_DEBUG=1`：把每個驗證器為什麼跑、為什麼沿用寫進 `.claude/.verify.log`）。
- **效能**：`python3 bench/benchmark.py` 量三種情境（無變更／只改一個 JSON／改韌體原始碼），見下表（合成專案 719 個檔案，韌體建置以固定 3 秒的假 `pio` 代替，CPU 4 核）：

| 情境 | 原版 bf696de | Stage 1 | Stage 2 |
|---|---|---|---|
| 無變更 | 0.05 s | 0.06 s | 0.07 s |
| 只改一個 JSON | 7.43 s | 3.85 s | 0.70 s |
| 改韌體原始碼 | 7.34 s | 3.90 s | 3.81 s |

## Cross-Model 計劃審查（Claude ↔ Codex）

第二個、與上面**職責不同**的 Stop hook 閘門:`verify.py` 審「完成的程式碼能否編譯/測試」（交付前），`plan-review.py` 審「**實作計劃的邏輯漏洞**」（動手前）。讓創意的 Claude 主寫、讓穩定的 Codex 當無情 Reviewer。

運作(harness 自動化,無需人肉複製貼上):
1. Claude 把非小改的 Implementation Plan 寫進 `.claude/plan.md`。
2. Claude 想收工 → `plan-review.py` 掃 `.claude/plan.md` 結尾,**沒看到 `[REVIEW_PASSED_MARKER]` 就擋下**,注入提示詞逼它跑 `codex-review` skill。
3. skill 用 `codex exec -s read-only`(Reviewer 只讀不改）送審,後續輪 `codex exec resume --last` 保留同一 session 上下文，來回辯駁。
4. Codex 回 `VERDICT: APPROVED` → Claude 在 `.claude/plan.md` 末端蓋 `[REVIEW_PASSED_MARKER]` → hook 放行。

需求:本機已安裝並登入 **Codex CLI**（`codex login`）。防無限迴圈同樣靠 `stop_hook_active`（每段工作只擋一次）。不需要計劃審查時,刪掉 `.claude/plan.md` 即解除此閘門。

> Reviewer 方法論與循環邏輯定義在 `.claude/skills/codex-review/SKILL.md`,可自行調整嚴格度。

## Loop Engineering（由 harness 強制的自我疊代迴圈）

第三個閘門 `loop-gate.py`：把「重複性高、驗收明確」的任務交給 Agent 自我疊代,但**輪次上限、無進展偵測、客觀驗收三件事由 harness 強制,不靠模型自律**。

- 專案放 `.claude/loop.json`（複製 `.claude/loop.example.json`）即武裝;不存在則此閘門不作用。
- 每次 Stop 跑 `loop.json.checks`（二元,全 exit 0 才綠燈放行）。有紅 → 擋下逼下一輪;達 `max_rounds` 或連續 `no_progress_limit` 輪錯誤不變 → **硬停**並要求依格式「舉手提報」交還人類。
- 輪次 counter 持久化在 `.claude/.loop_state.json`,模型無權繞過 → 迴圈一定有界,不會變 token 黑洞。

分工:客觀二元驗收 → `loop-gate.py`;主觀品質/裁判隔離 → `codex-review`(外部模型 Codex);交付前把關 → `verify.py`。細節與 `loop.json` 欄位見 [loop-engineering.md](loop-engineering.md)。

## 授權

MIT
