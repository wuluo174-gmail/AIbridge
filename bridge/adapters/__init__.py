"""
Bridge CLI Adapters
==================
CLI 工具适配器类导出 + AdapterRegistry + RoleConfig。

Step 3: 类导出和 ADAPTERS 映射。
Step 7: AdapterRegistry（带 DI 的懒实例化注册表）+ RoleConfig（frozen 角色配置）。
"""

import dataclasses
import threading

from .base import CLIAdapter
from .claude_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter

# 类映射表 — 保留向后兼容
ADAPTERS = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}


@dataclasses.dataclass(frozen=True)
class RoleConfig:
    """角色配置 — frozen 保证线程安全（替换引用而非修改字段）。"""
    planner_tool_id: str = "claude-code"
    reviewer_tool_id: str = "codex"


class AdapterRegistry:
    """Adapter 注册表 — 管理类注册、DI 实例化和发现。

    - register(): 注册 adapter 类 + DI 依赖，不立即实例化
    - get(): 懒实例化 + 缓存 + 线程安全
    - discover(): 返回所有工具信息（含 agent_name、安装状态、能力矩阵）
    - resolve_executor(): 返回具有 dangerous_mode 能力的 adapter
    """

    def __init__(self):
        self._recipes = {}      # {tool_id: (cls, di_kwargs)}
        self._instances = {}    # {tool_id: CLIAdapter}  懒缓存
        self._lock = threading.Lock()

    def register(self, tool_id, cls, **di_kwargs):
        """注册 adapter 类 + DI 依赖。不立即实例化。"""
        self._recipes[tool_id] = (cls, di_kwargs)
        self._instances.pop(tool_id, None)  # 重注册时清缓存

    def get(self, tool_id):
        """获取 adapter 实例（懒实例化 + 缓存）。"""
        inst = self._instances.get(tool_id)
        if inst is not None:
            return inst
        with self._lock:
            # Double-check after acquiring lock
            if tool_id in self._instances:
                return self._instances[tool_id]
            if tool_id not in self._recipes:
                raise KeyError(f"未注册的 adapter: {tool_id}")
            cls, kwargs = self._recipes[tool_id]
            inst = cls(**kwargs)
            self._instances[tool_id] = inst
            return inst

    def probe_all(self):
        """启动时探测所有已注册工具，将结果缓存到 adapter 实例。"""
        for tool_id in self._recipes:
            self.get(tool_id).probe()

    def discover(self):
        """返回所有已注册工具的缓存探测结果 + 能力矩阵。"""
        result = []
        for tool_id in self._recipes:
            result.append(self.get(tool_id).probe_snapshot())
        return result

    def list_tool_ids(self):
        """返回所有已注册 adapter ID。"""
        return list(self._recipes.keys())

    def resolve_executor(self, role_config):
        """返回具有 dangerous_mode 能力的 adapter。优先 planner，其次 reviewer。"""
        for tid in [role_config.planner_tool_id, role_config.reviewer_tool_id]:
            a = self.get(tid)
            if a.capabilities.get("dangerous_mode"):
                return a
        return self.get(role_config.planner_tool_id)  # fallback
