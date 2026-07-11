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

  mkdir -p .claude/hooks .claude/skills/codex-review

  # 契約檔:repo 已有 CLAUDE.md 或 AGENTS.md 就保留不覆蓋(專案自己的契約優先)。
  ADD_CONTRACT=""
  if [ ! -f CLAUDE.md ] && [ ! -f AGENTS.md ]; then
    cp "$SRC/CLAUDE.md" ./CLAUDE.md
    ADD_CONTRACT="CLAUDE.md"
  else
    echo "ℹ️  已有 CLAUDE.md/AGENTS.md,保留既有契約檔不覆蓋。"
  fi

  # settings.json:已存在則先備份,再只合併 hooks.Stop 鍵,其餘設定原樣保留。
  if [ -f .claude/settings.json ]; then
    cp .claude/settings.json ".claude/settings.json.bak.$(date +%s)"
    SRC_DIR="$SRC" python3 - <<'PY'
import json, os
kit = json.load(open(os.environ["SRC_DIR"] + "/.claude/settings.json", encoding="utf-8-sig"))
cur = json.load(open(".claude/settings.json", encoding="utf-8-sig"))
cur.setdefault("hooks", {})["Stop"] = kit["hooks"]["Stop"]
open(".claude/settings.json", "w", encoding="utf-8").write(json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
PY
    echo "ℹ️  既有 settings.json 已備份(.bak.*)並合併 hooks.Stop,其餘設定未動。"
  else
    cp "$SRC/.claude/settings.json" ./.claude/settings.json
  fi

  cp "$SRC/.claude/hooks/verify.py"     ./.claude/hooks/verify.py
  cp "$SRC/.claude/hooks/plan-review.py" ./.claude/hooks/plan-review.py
  cp "$SRC/.claude/hooks/loop-gate.py"   ./.claude/hooks/loop-gate.py
  cp "$SRC/.claude/loop.example.json"    ./.claude/loop.example.json
  cp "$SRC/.claude/skills/codex-review/SKILL.md" ./.claude/skills/codex-review/SKILL.md
  chmod +x .claude/hooks/verify.py .claude/hooks/plan-review.py .claude/hooks/loop-gate.py 2>/dev/null || true

  touch .gitignore
  for line in ".claude/.last_verify_ok" ".claude/skip-verify" ".claude/plan.md" \
              ".claude/loop.json" ".claude/.loop_state.json" ".claude/settings.json.bak.*"; do
    grep -qxF "$line" .gitignore || echo "$line" >> .gitignore
  done

  git add .claude/settings.json .claude/hooks/verify.py \
          .claude/hooks/plan-review.py .claude/hooks/loop-gate.py \
          .claude/loop.example.json .claude/skills/codex-review/SKILL.md .gitignore
  if [ -n "$ADD_CONTRACT" ]; then git add CLAUDE.md; fi
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
