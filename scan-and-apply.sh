#!/usr/bin/env bash
# scan-and-apply.sh — 掃出本機所有 git repo,回報哪些沒套/過期,可一鍵補齊。
#
# 為什麼需要:新專案不會自己套 kit,kit 修 bug 後各 repo 也不會自己更新。
# 2026-08-30 就發現兩個新 repo 完全沒套、且兩個 repo 的閘門因 kit bug 長期假綠。
#
# 用法(限 Bash 工具/Git Bash 執行):
#   ./scan-and-apply.sh                 # 只掃描回報,不動任何東西(預設)
#   ./scan-and-apply.sh --apply         # 套用 + commit(不 push)
#   ./scan-and-apply.sh --apply --push  # 套用 + commit + push
#   ./scan-and-apply.sh --root ~/work   # 自訂掃描根目錄(可重複)
#
# 分類:
#   EMPTY    零 commit(別的工具剛建/clone 中)→ 只回報,絕不套用
#   NO_KIT   完全沒套 → 會跑 apply-to-repo.sh
#   STALE    有套但 hooks 版本與 kit 不同 → 會更新
#   CURRENT  三支 hook 都與 kit 同版 → 略過
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
HOOKS="$SRC/.claude/hooks"
MODE=dry
PUSH=""
DEPTH=3
ROOTS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) MODE=apply ;;
    --push)  PUSH="--push" ;;
    --root)  shift; ROOTS+=("$1") ;;
    --depth) shift; DEPTH="$1" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知參數:$1(用 --help)"; exit 1 ;;
  esac
  shift
done

# 預設掃描根目錄:涵蓋使用者已知的專案落點。
if [ "${#ROOTS[@]}" -eq 0 ]; then
  ROOTS=("$HOME/Documents" "$HOME/projects" "/c/SMC_Trade")
fi

for h in verify plan-review loop-gate; do
  [ -f "$HOOKS/$h.py" ] || { echo "❌ kit 不完整:缺 $HOOKS/$h.py"; exit 1; }
done

# ── 1. 探索 ────────────────────────────────────────────────────────────────
repos=()
for root in "${ROOTS[@]}"; do
  [ -d "$root" ] || continue
  while IFS= read -r g; do
    repos+=("$(dirname "$g")")
  done < <(find "$root" -maxdepth "$DEPTH" -name .git -type d 2>/dev/null)
done
# 去重排序(路徑可能含空格,用 NUL 分隔)
if [ "${#repos[@]}" -gt 0 ]; then
  mapfile -d '' repos < <(printf '%s\0' "${repos[@]}" | sort -zu)
fi

[ "${#repos[@]}" -eq 0 ] && { echo "找不到任何 git repo(掃描根目錄:${ROOTS[*]})"; exit 0; }

# ── 2. 分類 ────────────────────────────────────────────────────────────────
md5of() { md5sum "$1" 2>/dev/null | cut -d' ' -f1; }

# 顯示用路徑:家目錄換成 ~。不可只印 basename —— 同名 repo(例:同一專案在
# 兩處各有一份)會無法分辨,曾差點把「另一份空殼」誤診成「主 repo 的閘門被拆」。
show() { local p="$1"; echo "${p/#$HOME/\~}"; }

classify() {  # $1=repo 路徑 → 印 EMPTY / NO_KIT / STALE / CURRENT
  local r="$1"
  # 零 commit 的 repo 不套:多半是別的工具(ChatGPT/Codex 等)剛建的工作區或 clone 進行中,
  # 在裡面建第一個 commit 會污染它。
  git -C "$r" rev-parse --verify -q HEAD >/dev/null 2>&1 || { echo EMPTY; return; }
  [ -f "$r/.claude/hooks/plan-review.py" ] || { echo NO_KIT; return; }
  local same=0
  for h in verify plan-review loop-gate; do
    [ "$(md5of "$HOOKS/$h.py")" = "$(md5of "$r/.claude/hooks/$h.py")" ] && same=$((same+1))
  done
  [ "$same" -eq 3 ] && echo CURRENT || echo STALE
}

todo=(); n_cur=0
echo "── 掃描結果(${#repos[@]} 個 repo)──"
for r in "${repos[@]}"; do
  st="$(classify "$r")"
  case "$st" in
    CURRENT) n_cur=$((n_cur+1)) ;;
    EMPTY)   printf '  %-8s %s(零 commit,略過不套)\n' "$st" "$(show "$r")" ;;
    *) todo+=("$r"); printf '  %-8s %s\n' "$st" "$(show "$r")" ;;
  esac
done
echo "  CURRENT  $n_cur 個(已是最新,略過)"

[ "${#todo[@]}" -eq 0 ] && { echo "✅ 全部已是最新,無需動作。"; exit 0; }

if [ "$MODE" = dry ]; then
  echo
  echo "以上 ${#todo[@]} 個需要處理。這是 dry-run,未改動任何東西。"
  # 提示要能原樣重跑,故把本次的 --root 一併帶上(否則範圍會變成預設值)。
  hint=""
  for r in "${ROOTS[@]}"; do hint="$hint --root \"$r\""; done
  echo "要實際套用:$0$hint --apply${PUSH:+ --push}"
  exit 0
fi

# ── 3. 套用 ────────────────────────────────────────────────────────────────
echo
echo "── 套用中 ──"
# shellcheck disable=SC2086
"$SRC/apply-to-repo.sh" ${PUSH} "${todo[@]}"

# ── 4. 套用後驗收(本 session 踩過的坑,自動查一遍)────────────────────────
echo
echo "── 驗收 ──"
fail=0
for r in "${todo[@]}"; do
  n="$(show "$r")"
  cd "$r" || continue

  bad=""
  # (a) hooks 是否真的同版
  for h in verify plan-review loop-gate; do
    [ "$(md5of "$HOOKS/$h.py")" = "$(md5of "$r/.claude/hooks/$h.py")" ] || bad="$bad hooks:$h"
  done
  # (b) hooks 能不能編譯(壞掉的 hook 會讓每次收工出錯)
  for h in verify plan-review loop-gate; do
    python3 -m py_compile "$r/.claude/hooks/$h.py" 2>/dev/null || bad="$bad compile:$h"
  done
  # (c) settings.json 被 gitignore 擋掉 → clone 拿不到閘門註冊(常見於無錨定的
  #     `settings.json` 規則)。若是專案刻意擋(例:整個 .claude/ 被封),只提示不算錯。
  if git check-ignore -q .claude/settings.json 2>/dev/null; then
    if git check-ignore -v .claude/settings.json 2>/dev/null | grep -q ':.claude/'; then
      echo "  ℹ️  $n:settings.json 被專案自訂規則刻意排除(磁碟有,clone 無)"
    else
      bad="$bad settings.json被gitignore擋(疑似未錨定規則)"
    fi
  fi

  if [ -n "$bad" ]; then echo "  ❌ $n:$bad"; fail=1; else echo "  ✅ $n"; fi
done

echo
if [ "$fail" = 1 ]; then
  echo "⚠️  有項目未通過驗收,請逐一處理。"
else
  echo "✅ 驗收全過。"
fi
cat <<'NOTE'

提醒:驗收只證明「閘門裝好了」,不證明「閘門會擋」。
每個新套的 repo 請各做一次負向測試(見 ops/20-JUDGMENT.md §5):
  故意弄壞一個檢查 → 收工時閘門必須 block;還原後必須放行。
  先確認「弄壞後直接跑該檢查真的回非 0」,再拿去測閘門。
NOTE
