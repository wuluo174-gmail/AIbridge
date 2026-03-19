"""
Codex CLI 适配器
================
封装 call_codex_streaming 的逻辑。

当前阶段：骨架声明。
Step 3 时从 bridge.py L337-437 迁入完整实现。

Codex 特有功能:
  - JSONL 流格式 (item.completed, item.started 事件)
  - codex exec --json resume --last (会话恢复)
  - 命令输出折叠 (chunk_boundary 事件)
"""

from .base import CLIAdapter


class CodexAdapter(CLIAdapter):
    """Codex CLI 适配器。"""

    @property
    def id(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "Codex"

    @property
    def capabilities(self) -> dict:
        return {
            **super().capabilities,
            "can_detect_install": True,
            "can_detect_auth": False,       # 待验证
            "can_trigger_auth": False,       # 待验证
            "auth_method": "unknown",        # 待验证
            "plan_mode": False,
            "dangerous_mode": False,
            "stream_json": False,           # JSONL 格式，非 stream-json
            "session_resume": True,         # codex exec --json resume --last
        }

    def check_installed(self) -> bool:
        # Step 3 时实现: shutil.which("codex") is not None
        raise NotImplementedError("骨架声明，Step 3 迁入实现")

    def build_command(self, prompt: str, cwd: str, **kwargs) -> list[str]:
        # Step 3 时从 bridge.py L346-367 迁入
        raise NotImplementedError("骨架声明，Step 3 迁入实现")

    def parse_stream_line(self, line: str) -> dict | None:
        # Step 3 时从 bridge.py L370-415 迁入
        raise NotImplementedError("骨架声明，Step 3 迁入实现")
