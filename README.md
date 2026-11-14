# claude-verify-kit

讓 Claude Code 在「專案無法通過驗證」前**無法宣告完成**的 Stop hook 閘門,搭配一份跨專案的 `CLAUDE.md` 工作契約。

對齊 Anthropic 官方原則:最高槓桿不是「跑測試」,而是給 agent **可驗證的成功標準**。本套件依專案內實際存在的檔案,自動派發對應檢查 —— 任一項失敗即擋下 Claude，直到修好。

## 涵蓋的驗證維度

| 產物 | 可驗證綠燈 | 工具 | 用真工具實測過（Linux） |
|---|---|---|---|
| 韌體 ESP32 / S3 / WROOM | 編譯/連結通過 | `idf.py build` / `pio run` / `arduino-cli compile` | `idf.py`：ESP-IDF v5.4.4（esp32）；`pio`：只有 native 平台；`arduino-cli`：**未實測** |
| Hackintosh OpenCore | plist 合法 + 符合 OC 規範 | `plutil -lint` + `ocvalidate`（版本須對應 OC release） | **未實測**（只有假工具；`ocvalidate` 沒有任何測試） |
| ACPI / SSDT | 可編譯成 .aml | `iasl` | **未實測**（只有假工具） |
| Gmail 過濾 / 一般 XML | well-formed | Python 內建（免裝） | 是 |
| AdGuard / adblock 規則 | 語法合法 | `aglint`（需 `.aglintrc`，含 error-token 保險） | **未實測**（只有假工具） |
| Excel xlsx | 可載入不損毀 | `openpyxl` | **未實測**（沒有任何測試） |
| Shell / Python / Node / Rust | lint / 編譯 / 測試 | `shellcheck` / `py_compile`+`pytest` / `npm test` / `cargo test` | 是（shellcheck 0.10／0.11、pytest 8／9、npm 10、Rust toolchain 1.85／1.86） |
| Claude Code 本身的設定 | hook 程式（`.claude/*.py`、`.claude/hooks/*.py`）語法正確；`.claude/*.json` 是合法 JSON；`settings.json`／`settings.local.json` 的 hooks 結構正確、指向的腳本存在 | Python 內建（免裝） | 是 |

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

> Windows：把 `settings.json` 與安裝腳本中的 `python3` 改成 `python`。（Windows 上的安裝流程**未實測**。）

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

任一專案放 `.claude/verify`（`.sh` / `.py` / `.cmd` / `.bat` / 可執行檔），每次 Stop 都會執行，exit code 非 0 就擋。適合把專屬成功標準寫死，例如：
- Gmail 過濾 XML：「無重複/衝突 entry」
- LINE / AdGuard ruleset：「核心 gateway host 沒被誤擋」

**自訂驗證與內建檢查的關係**（`.claude/verify.json` 的 `custom_verify_mode`）：

| 值 | 行為 |
|---|---|
| `"augment"`（預設） | 自訂驗證每次都跑；內建檢查照各自的快取規則照跑。**任何一邊失敗都擋**，原因會兩邊都列出來 |
| `"replace"` | 只跑自訂驗證，內建檢查全部不跑（舊版的行為）。要明確設定才會生效 |

舊版（Stage 1–3）固定是 `replace`：放一份 `verify.sh` 之後，JSON、Python 語法、hook 設定等內建檢查就全部停了——例如 `.claude/hooks/*.py` 有語法錯誤也會放行。不認得的設定值一律當成 `augment`。

`.py` 的自訂驗證：環境裡沒有設 `PYTHONIOENCODING`（或設成空字串）時，以 `PYTHONIOENCODING=:backslashreplace` 執行——輸出編碼照系統預設，只是編碼不出的字改印成 `\uXXXX`，不會因為 `UnicodeEncodeError` 而失敗。Windows 上輸出導向管線時用的是系統碼頁（GitHub 的 Windows runner 是 cp1252），印中文就會出錯（第一次跨平台 CI 實測）。自己設了 `PYTHONIOENCODING` 就照你的設定；`.sh`、`.cmd` 等其他形式不受影響。

## 逃生口

硬體專屬或半成品狀態要讓 Claude 正常收尾：
```bash
touch .claude/skip-verify   # 用完刪掉
```

## 運作機制

Stop hook 從 stdin 讀事件 JSON，失敗時回 `{"decision":"block","reason":...}`（Stop 事件用 top-level `decision`/`reason`，非 `hookSpecificOutput`）讓 Claude 續跑修正；`.claude/verify.json` 設 `"block_mode": "exit2"` 則改用 exit 2 + stderr。

防無限迴圈（`verify.py`）：
- Claude Code 在 hook 擋下後續跑時，下一次 Stop 的 `stop_hook_active` 為 true。此時若**上次擋下之後專案沒有任何修改**（檔案內容的 SHA-256 與工具／相依／環境的指紋都相同），hook 直接放行並用 `systemMessage` 告知使用者；**有修改**才重新驗證，仍失敗就再擋。
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
  - 工具執行檔本身（實際路徑、大小、mtime、ctime、inode；symlink 換目標也算）。只看 stat、不讀內容，也不受 `trust_mtime` 影響（執行檔常有上百 MB）；放在不更新 ctime 的檔案系統上、被同大小且還原 mtime 地替換時看不出來（已知限制）。
  - 被 `SKIP_DIRS` 排除的相依目錄：`node_modules`、`managed_components`、`.pio/libdeps`、專案直譯器的所有 site-packages（含 venv 與使用者 site），逐檔追蹤，就地改套件也抓得到：預設（`trust_mtime: false`）連內容一起算 SHA-256；明確設定信任 stat 時只比大小、mtime、ctime、inode。
  - 工具鏈：PlatformIO 的 `packages/`、`platforms/`（`PLATFORMIO_CORE_DIR`；平台建置腳本與所有 `framework-*` 套件逐檔追蹤）、ESP-IDF 的 `IDF_PATH`（版本檔，加上 `components/`、`tools/` 逐檔追蹤）、IDF 自己的 Python 環境（`IDF_PYTHON_ENV_PATH`）、PATH 上的 xtensa/riscv 編譯器、`IDF_TOOLS_PATH`、rustup（`RUSTUP_TOOLCHAIN`、`settings.toml`、toolchains）、Arduino 核心（`packages/*/hardware/*/*` 逐檔追蹤）與 sketchbook 函式庫。**就地修補框架原始碼**（例如改了 ESP-IDF 某個元件）也會重建。
  - 專案外的小設定檔（`~/.npmrc`、`~/.tool-versions`、rustup 的 `settings.toml`、`pyvenv.cfg`、ESP-IDF 的版本檔、PlatformIO 套件的 `package.json`…）：預設連內容一起算（1 MiB 以內）。
  - shellcheck 讀的專案外設定：`SHELLCHECK_OPTS`、`~/.shellcheckrc`、`$XDG_CONFIG_HOME/shellcheckrc`、專案上層目錄的 `.shellcheckrc`（Stage 3.2 起。之前用這些設定關掉警告跑過一次，拿掉之後會一直沿用當時的「通過」——實測重現）。
  - 相關環境變數（`IDF_*`、`PLATFORMIO_*`、`PYTHONPATH`、`NODE_OPTIONS`…），以及 `.claude/verify.json` 的設定雜湊（設定改了，所有快取作廢）。
  - 相依目錄超過 `dep_scan_cap`（預設 30 萬個檔）、或問不到直譯器的套件目錄 → 那個驗證器每次都跑。
- **逐檔驗證器**（JSON、XML、plist、ACPI、xlsx、shellcheck、Python 語法）：只驗內容變了的檔案，其餘沿用上次的結果，**包括上次的失敗**。
- **專案層級驗證器**：只快取通過。追蹤分三種模式：
  - `precise`：韌體的原始碼、標頭、建置設定、宣告嵌入的檔案、被 `include()` 的專案內 `.cmake`。
  - `any`：任何專案檔案變更都重跑。適用 pytest、npm、cargo、aglint，以及用了 `file(GLOB)`、`configure_file` 的韌體專案。
  - `always`：每次都跑，也不走全綠快速路徑。適用三種情況：建置會執行任意程式（`execute_process`、`add_custom_command`、FetchContent、PlatformIO `extra_scripts`）；引用專案外的路徑（`include()`、`EXTRA_COMPONENT_DIRS`、`add_subdirectory`、`lib_extra_dirs`、`-I`、`lib_deps = file://`、嵌入檔）；或路徑含無法解析的變數。`${CMAKE_CURRENT_SOURCE_DIR}` 等已知變數，以及同檔 `set()` 的字面值，會先代換。
  - 任何檔案被刪除時，`precise` 模式也重建。
- **自訂驗證 `.claude/verify*`**：預設每次都跑（`override_cache: false`），因為它讀什麼、依賴什麼無從得知。設成 `true` 會把常見工具、相依目錄、工具鏈與**整份環境變數**（`CC`、`CFLAGS` 之類都算；只存雜湊，值不落地）算進環境，沒變就不重跑。有自訂驗證時，內建檢查預設照跑（`custom_verify_mode: "augment"`，見「自訂驗證標準」）。
- **快取本身的安全**：快取檔有 SHA-256 校驗碼，並記錄所在目錄、格式版本（目前 2）、kit 版本、設定雜湊。任一項不符（截斷、寫壞、欄位型別錯、手改、從別的目錄複製來）→ 整份作廢、完整驗證。全綠紀錄和快取存在同一個檔、同一次原子寫入，所以刪掉或寫壞快取，就不會走快速路徑。狀態檔的欄位型別也會檢查。hook 若仍在第一次執行時崩潰，會改用「不含快取」的完整驗證重試；兩次都失敗才放行，而且使用者看得到。**信任邊界**：校驗碼擋不住「改完連校驗碼一起重算」的刻意竄改（實測過：重算後的偽造快取會被接受），因為 hook 與被它管的程序權限相同——能這樣做的人也能改 `verify.json` 或建立 `skip-verify`。要防模型自己動這些檔，請在專案的 `.claude/settings.json` 加拒絕規則（見下方「保護驗證設定」）。
- **並行與終止**：多個 hook 同時跑時，快取用原子替換寫入（Windows 上目標檔被別的程序開著時會短暫重試）；結果以內容雜湊為鍵，舊的寫入不會套到不同的內容上。hook 不論怎麼結束都不留孤兒程序：SIGTERM／SIGINT 由訊號處理器收掉整棵子程序樹；SIGKILL（包括整個程序群組被 SIGKILL）由外部看門程序收掉（POSIX）、由 Job Object 收掉（Windows）。hook 的父程序（Claude Code）死掉時，hook 會停掉工具、不寫快取、直接結束。工具正常結束後留下的背景程序也會被收掉（`kill_leftover_processes: false` 可關）。被終止的那一次一定不會寫入快取。**實測範圍**：以上在 Linux 上實測。Windows：第一次跨平台 CI（2026-11-12，GitHub Actions 的 Windows runner）上，「hook 被終止時 Job Object 收掉工具」與「預設收掉留下的背景程序」的測試通過；`kill_leftover_processes: false` 當時**沒有作用**（關閉 job 時背景程序被一起結束），之後已修正，修正後的行為要看下一輪 CI。檔案被鎖時的重試只有程式與測試。以上都不是在使用者自己的 Windows／macOS 機器上實測。自己用 `setsid` 之類脫離程序群組的程序收不到。
- **平行**：彼此獨立的驗證器同時跑，最多 `max_parallel` 個，其中重型最多 `max_heavy` 個（預設 1）。
- **已知限制**：
  - 內容改了但大小與 mtime 都被還原（`touch -r`、`cp -p`、tar 解壓、`robocopy`）：預設的 `trust_mtime: false` 每次重算 SHA-256，所以擋得下，不靠任何時間戳記。Linux 上在 ext4、xfs、btrfs、tmpfs、overlay，以及 ntfs-3g、fusefat（FAT32）、exfat-fuse（exFAT）三個 FUSE 實作上實測。明確設 `"auto"` 時 Linux／macOS 改靠 ctime：**在 FAT32 與 exFAT 的 FUSE 實作上會漏判**（ctime 恆等於 mtime）。明確設 `true` 時 Windows 上也會漏判。見下方「trust_mtime 的安全取捨」。
  - 被略過的目錄（`dist/`、`build/`、`.cache/`…）裡的檔案，只有被建置設定明確引用時才會追蹤；測試若讀取那些目錄，請設 `"cache": false`。
  - Windows 上，`.sh` 檔的路徑含有 ANSI 碼頁（`GetACP()`）無法無損編碼的字（例如 ACP 1252 下的中文路徑）時，經由 hook 執行的 shellcheck 在寫輸出時自己崩潰（shellcheck 0.9.0：`hPutChar: invalid argument (invalid character)`）。hook 照樣擋下，但訊息不是 shellcheck 的檢查結果。跨平台 CI 在 Windows runner（ACP 1252、OEM 437、主控台 65001）上實測到：同一個 shellcheck 直接在主控台執行時正常報出 SC2086，經由 hook 執行（工具沒有主控台）才崩潰。原因是推定（shellcheck 沒有主控台時改用 ACP 寫輸出），沒有查證；這個限制也沒有根治。測試以 strict xfail 記錄，只在三個條件同時成立時才算：Windows，而且 ACP 無法無損編碼測試路徑（Python 的嚴格編碼與不接受 best-fit 替代字元的 `WideCharToMultiByte` 兩邊都說編不出）；擋下的原因就是那一句錯誤、沒有任何 SC 檢查結果；其他錯誤一律算失敗。ACP 能表示這個路徑的機器（例如 ACP 950）不套用，照一般測試判定（那種機器沒有實測過）。ASCII 路徑的對照組在每個平台都必須報出 SC2086。
  - 工具執行檔只看 stat、不讀內容，也不受 `trust_mtime` 影響；工具鏈目錄清單（PlatformIO 的 `packages/`、rustup 的 toolchains…）裡的子目錄也只看 stat。相依目錄與專案外的小設定檔預設讀內容（見上）。
- **相關設定**（`.claude/verify.json`）：`max_parallel`、`max_heavy`、`cache`、`override_cache`、`custom_verify_mode`（`"augment"`／`"replace"`）、`trust_mtime`（預設 `false`；`"auto"`／`true`）、`dep_scan_cap`、`kill_leftover_processes`、`debug`（或環境變數 `CLAUDE_VERIFY_DEBUG=1`）。紀錄檔 `.claude/.verify.log` 的每個工具結果會寫 `pid`、耗時、結束原因（`exit`／`signal N`／`timeout_killed`）。
- **效能**：`python3 bench/benchmark.py`。下面第一張表是 Stage 1–2 當時各自量的歷史紀錄（**不含 Stage 3 之後的版本**）；目前這一版的數字在第二張表。

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

同一個 719 檔專案，三個版本在同一台機器、同一次執行裡量（2026-11-11，各 5 次中位數；冷啟動只跑 1 次）：

| 情境 | Stage 2 驗收後 `2f94628` | Stage 3 Final `c7640eb`（預設 `"auto"`） | Stage 3.2（預設 `false`） |
|---|---|---|---|
| 無變更 | 0.13 | 0.14 | 0.76 |
| 只改一個 JSON | 0.73 | 0.75 | 1.37 |
| 改一個 Python 原始碼 | 0.77 | 0.75 | 1.39 |
| 改韌體原始碼 | 3.75 | 3.77 | 4.38 |
| 工具版本變更（只換 pio） | 3.17 | 3.15 | 3.79 |
| 刪掉快取（完整重驗） | 3.79 | 3.79 | 4.40 |
| 冷啟動 | 4.18 | 3.96 | 8.99 |

Stage 3.2 每一列都多出約 0.6 秒，**與專案大小無關**：預設 `trust_mtime: false` 每次 Stop 都重讀專案直譯器的相依環境（site-packages）的內容——量測的那台機器上是 8,711 個檔、595 MB。**冷啟動那一格（8.99 s）只有一個樣本，而且偏高，原因沒有確認（疑點）**：事後清掉頁面快取重做，小專案的冷啟動只從 1.22 s 變成 1.74 s，解釋不了多出來的 5 秒；推測是量測前機器剛重啟、那 595 MB 第一次從磁碟讀出。同一次量測裡「刪掉快取」那一列（4.40 s）是暖機之後的完整重驗，比較能代表平常的成本。

大型專案（約 1 萬個專案檔＋4 萬個 `node_modules` 檔，同一次量測，各 3 次中位數）：無變更 `2f94628` 0.51 s → `c7640eb` 0.61 s → Stage 3.2 1.61 s；只改一個 JSON 2.19 → 2.21 → 3.26 s。（Stage 2 當時量到的是：無變更 0.68 s——初版 `c202ef0` 是 0.22 s，多出來的是逐檔掃描 `node_modules`——只改一個 JSON 2.30 s，原版 `bf696de` 30.0 s。）

自訂驗證（`.claude/verify.sh`）的實際專案（351 個檔、54 MB）：`override_cache: false`（預設）每次約 16.5 s；設成 `true`，無變更時 Stage 3 Final 是 0.16 s、Stage 3.2 是 0.89 s（Stage 2 當時量到 0.13 s）。

Stage 3（SHA-256、ctime／inode、看門程序）對 Stage 2 驗收後（`2f94628`），5 萬個專案檔＋4 萬個 `node_modules` 檔，各 20 次的中位數：

| 情境 | Stage 2 驗收後 | Stage 3 |
|---|---|---|
| 無變更 | 1.68 | 1.80 |
| 改一個 JSON（pytest、node 是 any 模式，也會重跑） | 5.99 | 6.46 |
| 多檔（5 種驗證器並行） | 6.58 | 7.14 |
| 工具版本變更 | 2.83 | 3.21 |
| 刪掉快取（完整重驗） | 6.69 | 7.03 |
| 冷啟動 | 8.11 | 7.73 |

（上表是 Stage 3 Final 的預設 `trust_mtime: "auto"`；Stage 3.2 預設改為 `false` 之後的成本見下方「trust_mtime 的安全取捨」。）兩版的重建行為完全相同；Stage 3 多出的 5–13% 是快取檔變大（5.44 → 8.65 MB）與逐檔多比 ctime／inode 的成本。完整數據（P95、CPU、峰值記憶體、子程序數）：`python3 bench/stage3_perf.py`。


## 安全性：能力、限制與信任邊界

### SHA-256 能做到什麼、做不到什麼

| 用途 | 能不能 |
|---|---|
| 判斷檔案內容有沒有變（manifest 的每一筆、相依目錄的深度簽章） | 能 |
| 偵測快取檔被截斷、寫壞、手改、從別的目錄複製來（校驗碼＋結構檢查＋所在目錄） | 能 → 整份作廢、完整重驗 |
| 防止**有寫入權限、也讀得懂格式**的人（包括模型本身）改掉快取後重算校驗碼 | **不能**。實測過：重算後的偽造快取會被接受 |

SHA-256 證明的是「內容一致」，不是「是誰寫的」。能寫 `.claude/` 的人一樣能改 `verify.json`、建立 `skip-verify`、或直接改 `verify.py`；換更強的雜湊或加 HMAC 都不會改變這件事（金鑰存在本機，同一個使用者讀得到）。

### 快取的信任邊界

- **快取防的是**：意外損毀、過期沿用（檔案、工具、相依、環境、設定變了卻沿用舊的通過）、並行寫入、執行中被終止。這些都有測試與反向測試守著。
- **快取不防的是**：同一使用者的蓄意竄改。要限制模型動這些檔，用權限規則（下一節）；要作業系統層級的強制，開 Claude Code 的 sandbox。
- **執行隔離**：hook 用跟 Claude Code 相同的權限執行工具與自訂驗證。不要把不可信的程式碼放進 `verify.sh`。

### trust_mtime 的安全取捨

判斷「檔案沒變」時要不要信任 stat（大小、mtime、ctime、inode）。**Stage 3.2 起預設是 `false`**：

| 值 | Linux／macOS | Windows | 什麼時候用 |
|---|---|---|---|
| `false`（預設） | 每次重算 SHA-256：專案檔、超過 64 MB 的大檔、相依目錄、專案外的小設定檔都讀內容 | 同左 | 一般情況。不依賴任何時間戳記是否可靠 |
| `"auto"`（Stage 3 Final 的預設） | 信任 stat。**只看平台、不看檔案系統**，而 ctime 會不會隨寫入更新取決於檔案系統。實測會更新：ext4、xfs、btrfs、tmpfs、overlay、ntfs-3g。**實測不會、因而漏判：fusefat（FAT32）、exfat-fuse（exFAT）**。Linux 核心的 vfat／exfat 驅動、網路掛載（NFS、SMB…）、macOS 的任何檔案系統：未實測 | 不信任 → 每次重算（程式行為，**未在 Windows 實機驗證**） | 確定專案在上面「實測會更新」的本機檔案系統上，而且需要省下重算的時間 |
| `true` | 信任 stat（FAT32／exFAT 同樣漏判） | 信任 stat：**同大小、還原 mtime 的改寫會漏判** | 明確接受漏判風險時 |

FAT32／exFAT 的結果只來自兩個 FUSE 實作（fusefat、exfat-fuse），**不代表所有實體裝置或其他驅動**。不認得的值（例如字串 `"false"`）會被忽略並改用預設值，所以退路永遠是重算那一邊（Stage 3.2 之前退回的是 `"auto"`）。

**成本**（全部在 Linux、4 核的同一台虛擬機上量；Windows 與 macOS **未實測**，Windows 開檔比 Linux 慢）：

| 量什麼 | `"auto"` | `false`（預設） |
|---|---|---|
| 合成專案（5 萬專案檔＋4 萬 `node_modules` 檔，**每個檔都很小**）無變更的 Stop：兩個版本各用自己的預設值，各 10 次中位數（2026-11-04） | 1.83 s（`c7640eb`） | 3.28 s |
| 同一個合成專案、同一版程式只換設定，各 7 次中位數（2026-11-11；同一次量測裡 `c7640eb` 是 1.80 s、設成 `true` 是 1.74 s） | 1.78 s | 3.01 s |
| 真實的相依環境：3 個檔的專案，直譯器是裝了 numpy／scipy／pandas／matplotlib 的 venv（11,241 個檔、403 MB），無變更的 Stop，各 7 次中位數 | 0.18 s | 0.74 s |
| 真實工具鏈（各只有 1 個樣本；`"auto"` 那欄是 `c7640eb` 的驗收紀錄）：系統 Python＋使用者 site／ESP-IDF v5.4.4 專案，無變更的 Stop | 0.13 s／0.36 s | 0.76 s／1.04 s |

讀這張表要知道三件事：

- **同一版程式設成 `"auto"` 與 `c7640eb` 一樣快**（同一次量測 1.78 對 1.80 s），所以變慢完全來自這個設定本身，不是 Stage 3.2 的其他修改。第一列同一次量測的其他情境（`c7640eb` → Stage 3.2）：改一個 JSON 6.51 → 8.34 s、工具版本變更 3.03 → 5.11 s、冷啟動 8.27 → 10.29 s。
- **成本跟「位元組數」成正比，不只是檔案數。**合成專案的檔案都只有幾十個位元組，量到的主要是開檔的成本；真實的相依環境是幾百 MB 到幾 GB。上面那個 403 MB 的 venv 多出 0.56 s，約每 100 MB 多 0.14 s（內容已在頁面快取裡）；內容還沒進快取時（例如剛開機），同樣的 403 MB 算一遍 SHA-256 是 1.27 s，已在快取裡是 0.51 s（虛擬機的磁碟；一般硬碟會更慢，未實測）。**幾 GB 的環境（例如大型機器學習套件）沒有實測**，依這個比例每次 Stop 會多出數秒。
- 被重讀的包括：專案檔、`node_modules`、`managed_components`、`.pio/libdeps`、專案直譯器的所有 site-packages、ESP-IDF 的 `components/` 與 `tools/`、PlatformIO 與 Arduino 的框架套件。超過 `dep_scan_cap`（預設 30 萬個檔）的相依目錄不追蹤（那個驗證器改成每次都跑）；**沒有依位元組數設上限**。

環境很大、而且專案確定放在上表「實測會更新 ctime」的本機檔案系統上時，可以明確設 `"trust_mtime": "auto"` 換回原本的速度——代價是上面寫的漏判風險。重現：`python3 bench/stage3_perf.py --revs c7640eb,WORKTREE --repeat 10`。

### 平台支援狀態

| 平台 | 狀態 |
|---|---|
| Linux（x86_64，Python 3.8／3.11／3.12／3.13） | 實測通過。第一次跨平台 CI（2026-11-12，run 43137519055）的 ubuntu 格：3.11／3.12／3.13 的失敗都是測試本身的假設或競態（已修正）；3.8 那格因為 `ubuntu-latest`（26.04）沒有 Python 3.8 而沒有執行，之後只有那一格改用 `ubuntu-24.04` |
| Windows | **只在 CI 上跑過**（同一次，GitHub Actions 的 Windows runner）：4 格都有 8–10 條失敗——`kill_leftover_processes: false` 沒有作用、輸出路徑的反斜線、`.py` 自訂驗證印中文就失敗、shellcheck 在 cp1252 下遇到中文路徑崩潰，以及兩項測試本身的問題。前三項與測試本身的兩項已修正，shellcheck 那一項列為已知限制，修正後的結果要看下一輪 CI。安裝流程與 Windows 工具鏈未實測 |
| macOS | **只在 CI 上跑過**（同一次，GitHub Actions 的 macOS runner）：4 格全部通過（401 條中 343 通過、58 條依平台跳過）。實機與工具鏈未實測。程式碼走 POSIX 路徑，但沒有 `/proc`（判斷留下的背景程序時殭屍也算成員） |
| 檔案系統 | 只在 Linux 上實測：ext4、xfs、btrfs、tmpfs、overlay，以及 FUSE 的 ntfs-3g、fusefat（FAT32）、exfat-fuse（exFAT）。預設 `false` 在這 8 種上全部擋得下；明確設 `"auto"` 時 FAT32 與 exFAT 會漏判。`tests/test_stage32_fs_semantics.py` + `.github/scripts/mount_test_filesystems.py` |

### 工具鏈實際驗證範圍

| 真的建置／執行過（Linux） | 沒有實測 |
|---|---|
| Python venv＋pip＋pytest、系統 Python＋使用者 site、Node／npm／npx、Rust／cargo／rustup（toolchain 切換）、ShellCheck（版本切換）、CMake＋GCC／Clang、PlatformIO native、ESP-IDF v5.4.4（esp32） | ESP32 Arduino（arduino-cli 1.3.1 已核對檢查碼（只證明完整性，不是發行者簽章），但 esp32 core 依賴的 `arduino` packager 在 downloads.arduino.cc，驗收環境連不到）、PlatformIO＋espressif32、所有 Windows／macOS 工具鏈 |
| 第三方專案（低權限使用者、建置與測試時無網路）：itsdangerous 2.2.0、chalk v6.0.1、cJSON v1.7.19、platform-native v1.2.1 hello-world。來源核對的是 tag commit 與套件雜湊（pip 的 sha256、npm 的 sha512 integrity）——**這是完整性，不是發行者身分**；tag 簽章與 npm registry 簽章都沒有驗證 | |

驗收腳本：`bench/real_toolchains.py`、`bench/real_projects.py`（需要網路，不進 CI；第三方專案用 `--phase prepare` 有網路安裝、`--phase run` 在無網路環境建置與測試）。

**裝進既有專案時要知道**：預設的 `augment` 模式會照跑內建檢查，既有的 lint 債會在第一次 Stop 就擋下，而且只要那些檔案沒修，每次都擋（逐檔快取連失敗一起沿用）。實測 itsdangerous 2.2.0 與 cJSON v1.7.19 的上游 shell 腳本本來就有 shellcheck 警告。處理方式：修掉、在專案的 `.shellcheckrc` 停用特定代碼，或（有自訂驗證時）明確設 `custom_verify_mode: "replace"`。**注意**：停用代碼目前不受任何檢查——agent 也能用同樣的方法（行內 `# shellcheck disable`、`.shellcheckrc`、`SHELLCHECK_OPTS`）把自己新造成的警告關掉。可追蹤、可審查的 baseline 與防繞過機制已有設計（Stage 3.2），**尚未實作**。

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

- **CI**：`.github/workflows/tests.yml`，Ubuntu／Windows／macOS × Python 3.8／3.11／3.12／3.13，`fail-fast: false`。Ubuntu 的 Python 3.8 那一格固定在 `ubuntu-24.04`（`ubuntu-latest` 已是 26.04，setup-python 沒有 3.8 的建置），其他格用各平台的 `-latest`。第一次執行在 2026-11-12（run 43137519055）：16 個 job 中 7 綠 9 紅，原因與處置見上面的平台表；之後的結果以 Actions 頁面為準。綠燈的意思是「該跑的都跑了而且通過」，不只是「沒有測試失敗」：
  - 必要工具（shellcheck、node、npm；POSIX 另要第二個 Python）在測試之前檢查，缺了就在那一步紅——安裝失敗不會變成測試跳過。
  - 每格的「假綠燈檢查」（`.github/scripts/junit_summary.py check`）：跳過的原因必須是這個平台預期的。Linux 只能跳過 Windows 專屬；macOS 另可跳過 Linux 專屬（`/proc`）；Windows 可跳過 POSIX 與 Linux 專屬；三者都可跳過「需要測試用檔案系統掛載」。其他原因（工具沒裝…）一律紅。已知限制以 strict xfail 記錄，單獨計數、不算通過。
  - `summary`：12 格齊全、每格收集到的測試數相同、同一作業系統的各 Python 版本跳過的測試相同（依映像名稱的第一段分組：`ubuntu-24.04` 與 `ubuntu-latest` 是同一組）、所有上游 job 都成功。摘要腳本讀不到或解析失敗也算紅。
  - `fs-semantics`：在 Linux 上掛 8 種真的檔案系統跑 `tests/test_stage32_fs_semantics.py`，不允許任何跳過。
  - `workflow-lint`（actionlint）與 `py38-compat`（vermin）。
  - pytest 版本鎖在 `.github/ci-requirements.txt`：Python 3.10 以上用 9.2.0（含 CVE-2025-71176 的修正）；3.8 用 8.3.5——最後一個支援 3.8 的版本，**沒有那個修正，也不會再有新版**，所以每格都以 `--basetemp` 指到 job 專屬目錄，不使用 `/tmp/pytest-of-<user>`。
  假工具是 Python 實作、依平台產生 sh 或 `.cmd` 啟動器，所以絕大多數測試在 Windows 上也會跑，而且走的是真的 `.cmd` 執行路徑。以上這些防線本身在 Linux 上以合成的 JUnit 與逐步重放 workflow 驗證過。第一次在 GitHub 的 runner 上執行時照設計運作：9 個紅 job 沒有一個被摘要當成綠；其餘 11 格的必要工具都裝得起來，POSIX 格都找得到第二個 Python；8 種掛載在 runner 上都成功。
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
