# Loop Engineering — 由 harness 強制的自我疊代迴圈

把「重複性高、驗收明確」的任務交給 Agent 自我疊代,但**三個關鍵機制不靠模型自律,而是由 harness 強制**:

| 機制 | 錯誤做法(靠 prompt 自律) | 本 kit 做法(harness 強制) |
|---|---|---|
| 客觀驗收 | 叫模型「自評 rubric 1–5 分」 | `loop-gate.py` 跑 `loop.json.checks`,**全 exit 0 才算綠燈** |
| 硬性停止 | 叫模型「最多跑 3 輪」 | 輪次 counter 持久化在 `.loop_state.json`,達 `max_rounds` 硬停,模型無權繞過 |
| 無進展偵測 | 叫模型「自己判斷有沒有進步」 | 對失敗輸出取 hash,連續 `no_progress_limit` 輪同一 hash → 自動停 |

> 為什麼:Loop 不是免費的,容易變 token 黑洞;模型在長迴圈裡會數錯輪次、掉狀態、或自評放水(球員兼裁判)。把停止條件與驗收搬進 harness,才是真正的 Agent-in-the-loop。

## 用法

1. 複製 `.claude/loop.example.json` → `.claude/loop.json`,填你的二元檢查與邊界(見範例)。
2. 給 Claude 任務。它每次想收工,`loop-gate.py` 就跑 `checks`:
   - 全綠 → 放行。
   - 有紅 → 擋下、附上失敗輸出與邊界,逼它進下一輪修正(顯示第 N/max 輪)。
   - 達 `max_rounds` 或連續無進展 → **硬停**,並要它依格式「舉手提報」(執行狀態 / 最後驗收數據 / 成果 / 困惑與權衡),交還你介入。
3. 任務結束後刪掉 `.claude/loop.json` 解除迴圈模式。

## loop.json 欄位

| 欄位 | 說明 | 預設 |
|---|---|---|
| `checks` | 二元檢查清單(字串陣列);每條經 OS 預設 shell 執行,**全 exit 0 才綠燈**。Windows 走 cmd.exe,需 bash 語法請包 `bash -lc '...'` | 必填 |
| `max_rounds` | 最多疊代輪數(硬上限) | 3 |
| `no_progress_limit` | 連續幾輪錯誤 hash 不變即判停滯(0 = 關閉) | 2 |
| `boundaries` | 注入每輪提示的「精準外科手術式修改」邊界字串陣列 | 無 |

## 與 kit 其他閘門的分工

- **客觀二元驗收** → 本 `loop-gate.py`(build / test / typecheck exit 0)。
- **主觀品質 / 邏輯審查(裁判隔離)** → `codex-review` skill(由**不同模型 Codex** 當外部裁判,避免球員兼裁判)。二者可同時武裝。
- **一次性交付前把關** → `verify.py`(依產物類型的綠燈)。

## 誠實邊界

- `loop-gate.py` 只保證「你列的二元 checks 過了」。**結構過 ≠ 行為對**;涉及硬體/外部行為仍需人工實測。
- 主觀 rubric 分數本質不可由「同一個模型」客觀自評 —— 需要打分就交給 `codex-review`(外部模型),不要在同一 context 自打自的分。
- 停滯偵測靠「失敗輸出逐字 hash」;若錯誤訊息含時間戳/亂數會每輪不同而測不到停滯,這種 check 請讓輸出穩定,或改用 `max_rounds` 兜底。
