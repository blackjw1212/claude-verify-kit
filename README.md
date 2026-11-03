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

原則：**寧可多花幾秒重新驗證，也不能因為快取把失敗的專案判定為通過。**快取只在「檔案內容」和「環境」都沒變時才沿用；任何一項追不到，就當成變了。

- **變更怎麼判定**：比對檔案內容的 SHA-256（`.claude/.verify_cache.json`），不是 transcript 也不是 git。transcript 看不到 Bash 改的檔，官方文件也說它在 Stop 當下可能還沒寫完；git status 列的是所有未 commit 的變更，也看不到被 `.gitignore` 排除、但驗證器照樣會檢查的檔案。只 `touch`、內容沒變不算變更。
- **環境也算變更**（全綠快速路徑與防迴圈都看它）：
  - 工具執行檔本身（實際路徑、大小、mtime、ctime、inode；symlink 換目標也算）。
  - 被 `SKIP_DIRS` 排除的相依目錄：`node_modules`、`managed_components`、`.pio/libdeps`、專案直譯器的所有 site-packages（含 venv 與使用者 site），逐檔比對大小、mtime、ctime、inode，就地改套件也抓得到。
  - 工具鏈：PlatformIO 的 `packages/`、`platforms/`（`PLATFORMIO_CORE_DIR`；平台建置腳本與所有 `framework-*` 套件逐檔追蹤）、ESP-IDF 的 `IDF_PATH`（版本檔，加上 `components/`、`tools/` 逐檔追蹤）、IDF 自己的 Python 環境（`IDF_PYTHON_ENV_PATH`）、PATH 上的 xtensa/riscv 編譯器、`IDF_TOOLS_PATH`、rustup（`RUSTUP_TOOLCHAIN`、`settings.toml`、toolchains）、Arduino 核心（`packages/*/hardware/*/*` 逐檔追蹤）與 sketchbook 函式庫。**就地修補框架原始碼**（例如改了 ESP-IDF 某個元件）也會重建。
  - 相關環境變數（`IDF_*`、`PLATFORMIO_*`、`PYTHONPATH`、`NODE_OPTIONS`…），以及 `.claude/verify.json` 的設定雜湊（設定改了，所有快取作廢）。
  - 相依目錄超過 `dep_scan_cap`（預設 30 萬個檔）、或問不到直譯器的套件目錄 → 那個驗證器每次都跑。
- **逐檔驗證器**（JSON、XML、plist、ACPI、xlsx、shellcheck、Python 語法）：只驗內容變了的檔案，其餘沿用上次的結果，**包括上次的失敗**。
- **專案層級驗證器**：只快取通過。追蹤分三種模式：
  - `precise`：韌體的原始碼、標頭、建置設定、宣告嵌入的檔案、被 `include()` 的專案內 `.cmake`。
  - `any`：任何專案檔案變更都重跑。適用 pytest、npm、cargo、aglint，以及用了 `file(GLOB)`、`configure_file` 的韌體專案。
  - `always`：每次都跑，也不走全綠快速路徑。適用三種情況：建置會執行任意程式（`execute_process`、`add_custom_command`、FetchContent、PlatformIO `extra_scripts`）；引用專案外的路徑（`include()`、`EXTRA_COMPONENT_DIRS`、`add_subdirectory`、`lib_extra_dirs`、`-I`、`lib_deps = file://`、嵌入檔）；或路徑含無法解析的變數。`${CMAKE_CURRENT_SOURCE_DIR}` 等已知變數，以及同檔 `set()` 的字面值，會先代換。
  - 任何檔案被刪除時，`precise` 模式也重建。
- **自訂驗證 `.claude/verify*`**：預設每次都跑（`override_cache: false`），因為它讀什麼、依賴什麼無從得知。設成 `true` 會把常見工具、相依目錄、工具鏈與**整份環境變數**（`CC`、`CFLAGS` 之類都算；只存雜湊，值不落地）算進環境，沒變就不重跑。注意：有自訂驗證時，內建檢查（JSON、Python 語法…）**全部不跑**，自訂驗證要自己涵蓋它們。
- **快取本身的安全**：快取檔有 SHA-256 校驗碼，並記錄所在目錄、格式版本（目前 2）、kit 版本、設定雜湊。任一項不符（截斷、寫壞、欄位型別錯、手改、從別的目錄複製來）→ 整份作廢、完整驗證。全綠紀錄和快取存在同一個檔、同一次原子寫入，所以刪掉或寫壞快取，就不會走快速路徑。狀態檔的欄位型別也會檢查。hook 若仍在第一次執行時崩潰，會改用「不含快取」的完整驗證重試；兩次都失敗才放行，而且使用者看得到。**信任邊界**：校驗碼擋不住「改完連校驗碼一起重算」的刻意竄改（實測過：重算後的偽造快取會被接受），因為 hook 與被它管的程序權限相同——能這樣做的人也能改 `verify.json` 或建立 `skip-verify`。要防模型自己動這些檔，請在專案的 `.claude/settings.json` 加拒絕規則（見下方「保護驗證設定」）。
- **並行與終止**：多個 hook 同時跑時，快取用原子替換寫入（Windows 上目標檔被別的程序開著時會短暫重試）；結果以內容雜湊為鍵，舊的寫入不會套到不同的內容上。hook 不論怎麼結束都不留孤兒程序：SIGTERM／SIGINT 由訊號處理器收掉整棵子程序樹；SIGKILL（包括整個程序群組被 SIGKILL）由外部看門程序收掉（POSIX）、由 Job Object 收掉（Windows）。hook 的父程序（Claude Code）死掉時，hook 會停掉工具、不寫快取、直接結束。工具正常結束後留下的背景程序也會被收掉（`kill_leftover_processes: false` 可關）。被終止的那一次一定不會寫入快取。
- **平行**：彼此獨立的驗證器同時跑，最多 `max_parallel` 個，其中重型最多 `max_heavy` 個（預設 1）。
- **已知限制**：
  - Windows 上，內容改了但大小與 mtime 都被還原（`touch -r`、`robocopy` 之類），預設模式看不到——Windows 的 `st_ctime` 是建立時間，沒有可用的變更時間。POSIX 上 ctime 會擋下這種情況。要完全保險請設 `"trust_mtime": false`。
  - 被略過的目錄（`dist/`、`build/`、`.cache/`…）裡的檔案，只有被建置設定明確引用時才會追蹤；測試若讀取那些目錄，請設 `"cache": false`。
  - 工具鏈與相依目錄是用「逐檔大小／mtime／ctime／inode」追蹤，沒有讀內容。
- **相關設定**（`.claude/verify.json`）：`max_parallel`、`max_heavy`、`cache`、`override_cache`、`trust_mtime`、`dep_scan_cap`、`kill_leftover_processes`、`debug`（或環境變數 `CLAUDE_VERIFY_DEBUG=1`）。紀錄檔 `.claude/.verify.log` 的每個工具結果會寫 `pid`、耗時、結束原因（`exit`／`signal N`／`timeout_killed`）。
- **效能**：`python3 bench/benchmark.py`，結果見下表。

合成專案 719 個檔案，CPU 4 核，每格為中位數，單位秒。韌體建置用固定 3 秒的假 `pio`；「未重建」表示那一次根本沒有呼叫建置工具。

| 情境 | 原版 bf696de | Stage 1 | Stage 2 初版 c202ef0 | Stage 2 驗收後 |
|---|---|---|---|---|
| 無變更 | 0.07 | 0.07 | 0.08 | 0.15 |
| 只改一個 JSON | 7.58 | 3.97 | 0.91 | 0.88 |
| 改一個 Python 原始碼 | 7.63 | 3.97 | 0.90 | 0.93 |
| 改韌體原始碼 | 7.45 | 4.11 | 3.91 | 3.92 |
| 工具版本變更（只換 pio） | 0.05 **未重建** | 0.07 **未重建** | 0.09 **未重建** | 3.20 重建 |
| 刪掉快取（完整重驗） | 7.88 | 4.00 | 3.96 | 3.96 |
| 冷啟動 | 7.92 | 4.07 | 3.85 | 3.99 |

大型專案（約 1 萬個專案檔＋4 萬個 `node_modules` 檔）：無變更 0.68 s（初版 0.22 s，多出來的是逐檔掃描 `node_modules`），只改一個 JSON 2.30 s（原版 30.0 s）。

自訂驗證（`.claude/verify.sh`）的實際專案：`override_cache: false`（預設）每次約 16.5 s；設成 `true`，無變更時約 0.13 s。

Stage 3（SHA-256、ctime／inode、看門程序）對 Stage 2 驗收後（`2f94628`），5 萬個專案檔＋4 萬個 `node_modules` 檔，各 20 次的中位數：

| 情境 | Stage 2 驗收後 | Stage 3 |
|---|---|---|
| 無變更 | 1.68 | 1.80 |
| 改一個 JSON（pytest、node 是 any 模式，也會重跑） | 5.99 | 6.46 |
| 多檔（5 種驗證器並行） | 6.58 | 7.14 |
| 工具版本變更 | 2.83 | 3.21 |
| 刪掉快取（完整重驗） | 6.69 | 7.03 |
| 冷啟動 | 8.11 | 7.73 |

兩版的重建行為完全相同；Stage 3 多出的 5–13% 是快取檔變大（5.44 → 8.65 MB）與逐檔多比 ctime／inode 的成本。完整數據（P95、CPU、峰值記憶體、子程序數）：`python3 bench/stage3_perf.py`。


### 保護驗證設定（建議）

快取的校驗碼只防意外損毀與過期沿用。要讓模型不能用內建工具改掉驗證結果或關掉驗證，在專案的 `.claude/settings.json` 加：

```json
{
  "permissions": {
    "deny": [
      "Edit(/.claude/.verify_cache.json)",
      "Edit(/.claude/.verify_state.json)",
      "Edit(/.claude/verify.json)",
      "Edit(/.claude/skip-verify)"
    ]
  }
}
```

依官方文件（Configure permissions），`/` 開頭的規則以專案設定所在的主工作目錄為錨點；`Edit` 拒絕規則涵蓋內建的檔案工具、Bash 裡被辨識的檔案指令（`cat`、`sed`、`tee`…）與重新導向，**但不涵蓋**自己開檔的任意子程序（例如一支 Python 腳本）。要作業系統層級的強制，請開 Claude Code 的 sandbox。`touch` 是否在被辨識的指令內，文件沒有列出（疑點）。

## 跨平台 CI 與驗收工具

- **CI**：`.github/workflows/tests.yml`，Ubuntu／Windows／macOS × Python 3.8／3.11／3.12／3.13，`fail-fast: false`，每格上傳 JUnit XML；另有 vermin 檢查 Python 3.8 相容性。假工具是 Python 實作、依平台產生 sh 或 `.cmd` 啟動器，所以絕大多數測試在 Windows 上也會跑，而且走的是真的 `.cmd` 執行路徑。只有 Windows 才有意義的測試在其他平台標成跳過（不是通過）。
- **真實工具鏈**：`python3 bench/real_toolchains.py --work DIR`。在隔離目錄用真的 pip／npm／cargo／rustup／cmake／gcc／clang／shellcheck／PlatformIO／ESP-IDF 建置，逐步記錄裁決、是否真的重跑、驗證器結果與原因摘錄。需要網路，不進 CI。
- **真實專案**：`python3 bench/real_projects.py --work DIR --src DIR --site PATH`。一律在隔離副本上做，原始目錄不寫入。
- **效能**：`python3 bench/stage3_perf.py --revs 2f94628,WORKTREE --repeat 20`（5 萬專案檔＋4 萬 node_modules；中位數／P95、CPU、峰值記憶體、子程序數、快取大小）。

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
