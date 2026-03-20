"""
Bridge Session 管理
==================
SessionState 类和事件管理函数。

依赖: bridge.protocol (EVENT_TYPES)
"""

import threading
import uuid
from datetime import datetime
from pathlib import Path

from bridge.protocol import EVENT_TYPES

LOG_DIR = Path("/tmp/bridge-logs")

sessions = {}           # session_id → SessionState
sessions_lock = threading.Lock()


class SessionState:
    """协商会话状态 — 每个浏览器 Tab 独立。

    字段清单:
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

    def __init__(self, session_id, task, project_path, max_rounds):
        self.session_id = session_id
        self.task = task
        self.project_path = project_path
        self.max_rounds = max_rounds
        self.status = "running"
        self.current_round = 0
        self.history = []
        self.consensus = False
        self.consensus_round = 0
        self.execution_result = None
        self.error = None
        # 事件流（每会话独立）
        self.events = []
        self.event_lock = threading.Lock()
        # 进程控制
        self.stop_flag = threading.Event()
        self.claude_has_session = False
        self.claude_session_id = str(uuid.uuid4())   # 创建时生成，全程绑定
        self.status_lock = threading.Lock()           # 状态转换专用锁
        self.codex_has_session = False
        self.active_proc = None
        # 日志目录（每会话独立）
        self.log_dir = LOG_DIR / session_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # 执行后审查（Issue 4）
        self.review_round = 0
        self.max_review_rounds = 3
        self.review_history = []
        self.exec_baseline_ref = None
        self.exec_baseline_untracked = set()
        self.is_git_repo = False


def get_session(sid):
    """按 session_id 查找会话，不存在返回 None。"""
    with sessions_lock:
        return sessions.get(sid)


def add_event(sess, etype, data):
    if etype not in EVENT_TYPES:
        raise ValueError(f"未声明的事件类型: {etype}，请先在 bridge/protocol.py EVENT_TYPES 中注册")
    with sess.event_lock:
        sess.events.append({
            "id": len(sess.events), "type": etype,
            "data": data, "ts": datetime.now().isoformat(),
        })


def add_history_event(sess, history_list, entry, event_type):
    """原子地追加历史记录并发送事件（统一快照锁）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未声明的事件类型: {event_type}，请先在 bridge/protocol.py EVENT_TYPES 中注册")
    with sess.event_lock:
        history_list.append(entry)
        sess.events.append({
            "id": len(sess.events), "type": event_type,
            "data": entry, "ts": datetime.now().isoformat(),
        })
