"""
Codex CLI 适配器
================
封装 call_codex_streaming 的完整逻辑。

Step 3 从 bridge.py 迁入:
  - build_command: L285-289 (命令构造)
  - parse_stream_line: L320-346 (JSONL 解析)

Codex 特有功能:
  - JSONL 流格式 (item.completed, item.started 事件)
  - codex exec --json resume --last (会话恢复)
  - 命令输出折叠 (chunk_boundary 事件)
"""

import json

from .base import CLIAdapter


class CodexAdapter(CLIAdapter):
    """Codex CLI 适配器。"""

    # ── 身份 ──

    @property
    def id(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "Codex"

    @property
    def cli_name(self) -> str:
        return "codex"

    # ── 能力矩阵 ──

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

    # ── 命令构造 ──

    def build_command(self, prompt, cwd, **kwargs):
        resume_last = kwargs.get("resume_last", False)
        cmd = ["codex"]
        if resume_last:
            cmd.extend(["exec", "--json", "resume", "--last", prompt])
        else:
            cmd.extend(["exec", "--json", prompt])
        return cmd

    # ── 流解析 ──

    def parse_stream_line(self, line):
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return {"type": "text_chunk", "text": line + "\n"}

        etype = evt.get("type", "")
        item = evt.get("item", {})
        item_type = item.get("type", "")

        if etype == "item.completed" and item_type == "agent_message":
            text = item.get("text", "")
            return {"type": "message", "text": text} if text else None

        elif etype == "item.started" and item_type == "command_execution":
            cmd_str = item.get("command", "")
            return {"type": "command_start", "command": cmd_str} if cmd_str else None

        elif etype == "item.completed" and item_type == "command_execution":
            cmd_output = item.get("aggregated_output", "")
            if cmd_output:
                display = (cmd_output if len(cmd_output) <= 2000
                           else cmd_output[:2000] + "\n...(truncated)\n")
                return {"type": "command_output", "output": display}
            return None

        return None

    # ── 可选钩子 ──

    def extract_result(self, stream_display, result_text):
        return result_text

    def format_process_error(self, returncode, log_file):
        return f"Codex CLI 错误 (code {returncode})"

    def format_not_found_error(self):
        return "未找到 'codex' 命令。请安装: npm install -g @openai/codex"
