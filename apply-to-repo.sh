#!/usr/bin/env bash
# apply-to-repo.sh — 把驗證閘門套進指定 repo(專案層),commit,可選 push。
#
# 用法:
#   ./apply-to-repo.sh /path/to/repo            # 套用 + commit(不 push)
#   ./apply-to-repo.sh --push /path/to/repo     # 套用 + commit + push(用你自己的 git 認證)
#   ./apply-to-repo.sh --push ~/a ~/b ~/c       # 多個一次處理
#
# 注意:push 需要你本機已對該 repo 設好 git 認證(SSH key 或 token)。
#       本腳本只新增/覆寫 CLAUDE.md 與 .claude/,不碰你其他檔案。
set -euo pipefail

DO_PUSH=0
if [ "${1:-}" = "--push" ]; then DO_PUSH=1; shift; fi
[ "$#" -ge 1 ] || { echo "用法:$0 [--push] <repo路徑> [更多...]"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"

apply_one() {
  local repo="$1"
  echo "──────── $repo ────────"
  cd "$repo" 2>/dev/null || { echo "❌ 進不去,跳過"; return; }
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "❌ 不是 git repo,跳過"; return; }

  mkdir -p .claude/hooks
  cp "$SRC/CLAUDE.md"               ./CLAUDE.md
  cp "$SRC/.claude/settings.json"  ./.claude/settings.json
  cp "$SRC/.claude/hooks/verify.py" ./.claude/hooks/verify.py
  chmod +x .claude/hooks/verify.py 2>/dev/null || true

  touch .gitignore
  for line in ".claude/.last_verify_ok" ".claude/skip-verify"; do
    grep -qxF "$line" .gitignore || echo "$line" >> .gitignore
  done

  git add CLAUDE.md .claude/settings.json .claude/hooks/verify.py .gitignore
  if git diff --cached --quiet; then echo "ℹ️  無變更,略過。"; return; fi
  git commit -m "chore(claude): add Stop-hook verifier + CLAUDE.md working contract"
  echo "✅ 已 commit(本機)。"

  if [ "$DO_PUSH" = "1" ]; then
    echo "⤴️  push 中…"
    if git push; then echo "✅ push 完成:$repo"
    else echo "❌ push 失敗:$repo(commit 已在本機;檢查遠端/認證後重跑 git push)"; fi
  else
    echo "ℹ️  未 push。要推請加 --push,或自行 git push。"
  fi
}

for repo in "$@"; do ( apply_one "$repo" ) || echo "⚠️  $repo 出錯,繼續。"; done
echo "全部處理完畢。"
