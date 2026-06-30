#!/usr/bin/env bash
# install-global.sh — 把驗證閘門裝到 ~/.claude/(涵蓋所有專案,不進版控)。
# Windows 請改用 python(非 python3)並手動複製,或在 WSL 執行。
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${HOME}/.claude"

mkdir -p "$DEST/hooks" "$DEST/skills/codex-review"
cp "$SRC/CLAUDE.md"                    "$DEST/CLAUDE.md"
cp "$SRC/.claude/hooks/verify.py"      "$DEST/hooks/verify.py"
cp "$SRC/.claude/hooks/plan-review.py" "$DEST/hooks/plan-review.py"
cp "$SRC/.claude/skills/codex-review/SKILL.md" "$DEST/skills/codex-review/SKILL.md"
chmod +x "$DEST/hooks/verify.py" "$DEST/hooks/plan-review.py"

# 全域 settings.json:hook 指向 ~/.claude/hooks/verify.py
# 若已存在則備份,避免覆蓋你既有設定。
if [ -f "$DEST/settings.json" ]; then
  cp "$DEST/settings.json" "$DEST/settings.json.bak.$(date +%s)"
  echo "ℹ️  已備份原有 settings.json"
fi
cat > "$DEST/settings.json" <<'JSON'
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/plan-review.py",
            "timeout": 60
          },
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/verify.py",
            "timeout": 600
          }
        ]
      }
    ]
  }
}
JSON

echo "✅ 已裝到 $DEST。重啟 Claude Code,輸入 /hooks 應可見 Stop 事件。"
echo "   (若你原本 settings.json 有其他設定,請從 .bak 合併回 hooks 區塊。)"
