#!/bin/zsh
# 双击启动 Claude ↔ Codex Bridge
# 浏览器打开后：填项目路径 + 任务 → 点开始

export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.npm-global/bin:$PATH"
source "$HOME/.zshrc" 2>/dev/null; true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for cmd in python3 claude codex; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "❌ 未找到 $cmd"
        [[ "$cmd" == "claude" ]] && echo "   npm install -g @anthropic-ai/claude-code"
        [[ "$cmd" == "codex"  ]] && echo "   npm install -g @openai/codex"
        read "?按回车退出"; exit 1
    fi
done

lsof -ti:8686 | xargs kill -9 2>/dev/null; true

(sleep 2 && open "http://localhost:8686") &

python3 "$SCRIPT_DIR/bridge.py" --port 8686
