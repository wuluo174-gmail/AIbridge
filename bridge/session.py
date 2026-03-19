"""
Bridge Session 管理
==================
SessionState 类和事件管理函数的接口声明。

当前阶段：骨架声明，不接入运行路径。
实际代码仍在 bridge.py L64-131。
Step 2 时将 bridge.py 中的实现迁入此文件。

对应 bridge.py 行号 (commit cdc4613):
  - SessionState: L74-107
  - add_event: L115-120
  - add_history_event: L123-130
  - get_session: L110-112
  - sessions dict: L69
  - sessions_lock: L70
  - plan_file_lock: L71
"""

import threading
import uuid
from datetime import datetime
from pathlib import Path


# Step 2 时从 bridge.py 迁入的全局状态
# sessions = {}           # session_id → SessionState
# sessions_lock = threading.Lock()
# plan_file_lock = threading.Lock()
# LOG_DIR = Path("/tmp/bridge-logs")


class SessionState:
    """协商会话状态 — 每个浏览器 Tab 独立。

    字段清单 (与 bridge.py L74-107 一一对应):
        session_id:     str     — uuid hex[:8]
        task:           str     — 用户任务描述
        project_path:   str     — 项目路径
        max_rounds:     int     — 最大协商轮数
        status:         str     — 状态机当前状态 (见 protocol.STATES)
        current_round:  int     — 当前轮次
        history:        list    — 协商历史条目
        consensus:      bool    — 是否达成共识
        consensus_round: int    — 共识轮次
        execution_result: str|None — 执行结果
        error:          str|None — 错误信息
        events:         list    — 事件流
        event_lock:     Lock    — 事件流锁
        stop_flag:      Event   — 中止标记
        claude_has_session: bool — Claude CLI 是否已建立会话
        claude_session_id: str  — Claude 会话 ID (uuid4, 全程绑定)
        status_lock:    Lock    — 状态转换专用锁
        codex_has_session: bool — Codex CLI 是否已建立会话
        active_proc:    Popen|None — 当前活跃子进程
        log_dir:        Path    — 日志目录
        review_round:   int     — 审查轮次
        max_review_rounds: int  — 最大审查轮数 (默认 3)
        review_history: list    — 审查历史条目
        exec_baseline_ref: str|None — Git baseline ref
        exec_baseline_untracked: set — 执行前 untracked 文件集
        is_git_repo:    bool    — 项目是否在 git 仓库中

    不可持久化的纯内存字段:
        stop_flag, active_proc, event_lock, status_lock,
        claude_session_id, claude_has_session, codex_has_session,
        exec_baseline_ref, exec_baseline_untracked, events
    """

    def __init__(self, session_id: str, task: str, project_path: str, max_rounds: int):
        # Step 2 时填入完整实现
        raise NotImplementedError("骨架声明，Step 2 迁入实现")
