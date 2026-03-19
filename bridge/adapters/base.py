"""
Bridge CLIAdapter 基类
=====================
所有 CLI 工具适配器的抽象基类。
只在 Python 内实现，前端消费只读元数据 (capabilities)。

设计原则:
  1. 每个工具的差异在 adapter 内部隔离
  2. 编排引擎只通过基类接口调用
  3. 能力矩阵 (capabilities) 声明每个工具支持什么
  4. 认证能力全部标记为"待验证" — 当前代码无认证检测逻辑

对应 bridge.py (commit cdc4613):
  - call_claude_streaming: L199-335
  - call_codex_streaming: L337-437
  - _stderr_reader: L178-196 (共享)
"""

import re
from abc import ABC, abstractmethod


class CLIAdapter(ABC):
    """CLI 工具适配器基类。"""

    # ── 身份 ──

    @property
    @abstractmethod
    def id(self) -> str:
        """工具唯一标识，如 "claude-code", "codex"。"""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """工具显示名称，如 "Claude Code", "Codex"。"""
        ...

    # ── 能力矩阵 ──

    @property
    def capabilities(self) -> dict:
        """能力声明 — 前端只读消费，不做适配逻辑。

        所有认证相关字段标记为待验证。
        当前代码唯一的认证逻辑是 FileNotFoundError 报错 (L335-336, L431-432)。
        """
        return {
            "can_detect_install": True,     # 所有工具都应支持 which <tool>
            "can_detect_auth": False,       # 待验证
            "can_trigger_auth": False,      # 待验证
            "auth_method": "unknown",       # 待验证: browser_oauth / api_key_env / config_file / manual
            "plan_mode": False,             # 是否支持 plan-only 模式
            "dangerous_mode": False,        # 是否有跳过权限的 flag
            "stream_json": False,           # 是否支持结构化流输出
            "session_resume": False,        # 是否支持会话续接
        }

    # ── 生命周期 ──

    @abstractmethod
    def check_installed(self) -> bool:
        """检查工具是否已安装。"""
        ...

    # ── 执行 ──

    @abstractmethod
    def build_command(self, prompt: str, cwd: str, **kwargs) -> list[str]:
        """构建 CLI 命令行参数列表。

        kwargs 可包含:
          continue_session: bool — 是否续接会话
          bypass_permissions: bool — 是否跳过权限
          session_id: str — 会话 ID
          resume_last: bool — 是否恢复上次会话 (Codex)
        """
        ...

    @abstractmethod
    def parse_stream_line(self, line: str) -> dict | None:
        """解析一行流输出，返回归一化事件或 None。

        返回格式:
          {"type": "text_chunk", "text": "..."}
          {"type": "command_start", "command": "..."}
          {"type": "command_output", "output": "..."}
          {"type": "result", "text": "..."}
          {"type": "error", "message": "..."}
          None — 忽略该行
        """
        ...

    # ── 协议检测 ──

    def detect_approval(self, text: str) -> bool:
        """检测审查结果是否为 APPROVED。

        默认实现: 首行首词为 APPROVED (大小写不敏感)。
        对应 bridge.py L626-629 is_approved()。
        """
        if not text:
            return False
        first_line = text.strip().split("\n")[0]
        return bool(re.match(r'\s*APPROVED\b', first_line, re.IGNORECASE))

    def detect_closure(self, text: str) -> bool:
        """检测执行后审查结果是否为"任务收口成功"。

        默认实现: 首行含"任务收口成功"。
        对应 bridge.py L828, L899。
        """
        if not text:
            return False
        return "任务收口成功" in text.split("\n")[0]
