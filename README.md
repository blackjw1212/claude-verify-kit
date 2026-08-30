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

Stop hook 從 stdin 讀事件 JSON，失敗時回 `{"decision":"block","reason":...}`（Stop 事件用 top-level `decision`/`reason`，非 `hookSpecificOutput`）讓 Claude 續跑修正。防無限迴圈靠 `stop_hook_active`：第一次 Stop 被擋後 Claude 重跑修正，下一次 Stop 事件的 `stop_hook_active` 為 true，hook 立即 exit 0 放行 —— 每段工作只擋一次。

> 注意：Claude Code **並無**固定的「連續 block 上限」數字；唯一可靠的防迴圈就是 hook 自己檢查 `stop_hook_active`（本 kit 已於 `verify.py` 第一步實作）。若 hook 不做此檢查，曾有回報導致無限迴圈跑到吃光整個 session。

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
