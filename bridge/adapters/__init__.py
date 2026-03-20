"""
Bridge CLI Adapters
==================
CLI 工具适配器类导出 + 类映射表。

Step 3: 类导出和 ADAPTERS 映射。
Step 7: 实现带依赖注入的 AdapterRegistry。

注意: 不提供 get_adapter() 工厂函数 — ClaudeCodeAdapter 需要
plan_lock_acquire_fn 注入才能正确工作（NR-8），裸实例化会丢失
plan lock 语义。带依赖注入的完整 registry 是 Step 7 的工作。
"""

from .base import CLIAdapter
from .claude_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter

# 类映射表 — 供 Step 7 AdapterRegistry 使用，Step 3 不做实例化
ADAPTERS = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}
