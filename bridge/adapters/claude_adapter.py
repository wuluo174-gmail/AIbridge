"""
Claude Code CLI 适配器
=====================
封装 call_claude_streaming 的逻辑。

当前阶段：骨架声明。
Step 3 时从 bridge.py L199-335 迁入完整实现。

Claude Code 特有功能:
  - stream-json 流格式 (text_delta 事件)
  - --session-id / --resume 会话绑定
  - --permission-mode plan (协商阶段)
  - --dangerously-skip-permissions (执行阶段)
  - Plan 文件快照差集检测 (L133-172)
  - stderr MCP 噪音过滤
"""

from .base import CLIAdapter


class ClaudeCodeAdapter(CLIAdapter):
    """Claude Code CLI 适配器。"""

    @property
    def id(self) -> str:
        return "claude-code"

    @property
    def display_name(self) -> str:
        return "Claude Code"

    @property
    def capabilities(self) -> dict:
        return {
            **super().capabilities,
            "can_detect_install": True,
            "can_detect_auth": False,       # 待验证
            "can_trigger_auth": False,       # 待验证
            "auth_method": "unknown",        # 待验证
            "plan_mode": True,              # --permission-mode plan
            "dangerous_mode": True,         # --dangerously-skip-permissions
            "stream_json": True,            # --output-format stream-json
            "session_resume": True,         # --session-id / --resume
        }

    def check_installed(self) -> bool:
        # Step 3 时实现: shutil.which("claude") is not None
        raise NotImplementedError("骨架声明，Step 3 迁入实现")

    def build_command(self, prompt: str, cwd: str, **kwargs) -> list[str]:
        # Step 3 时从 bridge.py L199-217 迁入
        raise NotImplementedError("骨架声明，Step 3 迁入实现")

    def parse_stream_line(self, line: str) -> dict | None:
        # Step 3 时从 bridge.py L240-290 迁入
        raise NotImplementedError("骨架声明，Step 3 迁入实现")
