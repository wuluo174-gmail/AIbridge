"""
Bridge 持久化存储
================
SQLite 持久化层 (Python sqlite3 标准库)。

当前阶段：类声明骨架。
Step 6 时实现完整逻辑。

设计约束:
  - Python 是唯一写入方
  - 活动会话运行态不可持久化
  - 只存已完成会话的终态快照
  - 重启后活动会话标记为"中断"，不尝试恢复
"""

from pathlib import Path


class Store:
    """SQLite 持久化存储。

    Step 6 时实现以下方法:
      - init_db(): 建表 (使用 schema.sql)
      - save_session(sess): 保存已完成会话快照
      - list_sessions(): 列出历史会话
      - get_session_history(session_id): 获取指定会话的协商历史
      - get_session_review_history(session_id): 获取审查历史
      - save_prompts(config): 保存提示词配置
      - load_prompts(): 加载提示词配置
      - save_recent_paths(paths): 保存最近路径
      - load_recent_paths(): 加载最近路径
      - register_tool(tool_id, display_name, capabilities): 注册 CLI 工具
      - list_tools(): 列出已注册工具
    """

    def __init__(self, db_path: str | Path = None):
        # Step 6 时实现
        # 默认 db_path: ~/.bridge/bridge.db
        raise NotImplementedError("骨架声明，Step 6 迁入实现")
