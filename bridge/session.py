"""
Bridge Session 管理
==================
SessionState 类和事件管理函数。

依赖: bridge.protocol (EVENT_TYPES)

Step 7: adapter_state 泛化会话追踪。
"""

import threading
import uuid
from datetime import datetime
from pathlib import Path

from bridge.protocol import EVENT_TYPES

LOG_DIR = Path("/tmp/bridge-logs")

sessions = {}           # session_id → SessionState
sessions_lock = threading.Lock()
_persist_hook = None


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
        phase:          str     — 当前生命周期阶段 (negotiation/execution/review)
        updated_at:     str     — 最近更新时间
        finished_at:    str|None — 终态时间
        interrupt_reason: str|None — 中断/恢复原因
        events:         list    — 事件流
        event_lock:     Lock    — 事件流锁
        stop_flag:      Event   — 中止标记
        status_lock:    Lock    — 状态转换专用锁
        proc_lock:      Lock    — 保护 active_proc + active_pgid 原子读写
        active_proc:    Popen|None — 当前活跃子进程 (leader)
        active_pgid:    int|None — CLI 进程组 ID，独立于 leader 生命周期
        log_dir:        Path    — 日志目录
        review_round:   int     — 审查轮次
        max_review_rounds: int  — 最大审查轮数 (默认 3)
        review_history: list    — 审查历史条目
        exec_baseline_ref: str|None — Git baseline ref
        exec_baseline_untracked: set — 执行前 untracked 文件集
        is_git_repo:    bool    — 项目是否在 git 仓库中
        planner_tool_id: str    — Planner 工具 ID (Step 7)
        reviewer_tool_id: str   — Reviewer 工具 ID (Step 7)
        adapter_state:  dict    — 每工具逻辑会话追踪 {state_key: {...}}，持久化到 adapter_state_json

    不可持久化的纯内存字段:
        stop_flag, active_proc, active_pgid, proc_lock, event_lock, status_lock,
        exec_baseline_ref, exec_baseline_untracked, events
    """

    def __init__(self, session_id, task, project_path, max_rounds):
        self.session_id = session_id
        self.task = task
        self.project_path = project_path
        self.max_rounds = max_rounds
        self.status = "running"
        self.phase = "negotiation"
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
        self.status_lock = threading.Lock()           # 状态转换专用锁
        self.proc_lock = threading.Lock()             # 保护 active_proc + active_pgid 原子读写
        self.active_proc = None
        self.active_pgid = None                       # CLI 进程组 ID，独立于 active_proc 生命周期
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
        # 持久化字段：创建时间戳
        self.created_at = datetime.now().isoformat()
        self.updated_at = self.created_at
        self.finished_at = None
        self.interrupt_reason = None

        # ── Step 7: 角色配置 + 泛化会话追踪 ──
        self.planner_tool_id = "claude-code"
        self.reviewer_tool_id = "codex"
        self.planner_state_key = self.planner_tool_id
        self.reviewer_state_key = self.reviewer_tool_id
        # 默认初始化 adapter_state — 两个内置工具
        # create_session() 会按最新角色配置重新初始化，但这里先给出稳定默认值，
        # 这样直接实例化 SessionState 的调用方也拥有完整可恢复状态。
        _default_claude_sid = str(uuid.uuid4())
        self.adapter_state = {
            "claude-code": {"has_session": False, "session_id": _default_claude_sid},
            "codex": {"has_session": False},
        }
        self._default_claude_sid = _default_claude_sid

    # ── adapter_state 泛化访问器 ──

    def init_adapter_state(self, tool_id, capabilities):
        """（重新）初始化指定工具的会话状态。"""
        state = {"has_session": False}
        if capabilities.get("session_resume"):
            state["session_id"] = str(uuid.uuid4())
        self.adapter_state[tool_id] = state

    def get_adapter_has_session(self, state_key):
        return self.adapter_state.get(state_key, {}).get("has_session", False)

    def set_adapter_has_session(self, state_key, value):
        self.adapter_state.setdefault(state_key, {})["has_session"] = value

    def get_adapter_session_id(self, state_key):
        return self.adapter_state.get(state_key, {}).get("session_id")

    @property
    def resume_available(self):
        return self.status in {"paused", "interrupted"}

    @property
    def claude_session_id(self):
        return self.get_adapter_session_id("claude-code") or self._default_claude_sid


def get_session(sid):
    """按 session_id 查找会话，不存在返回 None。"""
    with sessions_lock:
        return sessions.get(sid)


def set_persist_hook(fn):
    global _persist_hook
    _persist_hook = fn


def _touch_session(sess):
    sess.updated_at = datetime.now().isoformat()
    if sess.status in {"aborted", "done", "error"}:
        sess.finished_at = sess.finished_at or sess.updated_at
    elif sess.status not in {"idle"}:
        sess.finished_at = None


def _persist_session(sess):
    if _persist_hook is None:
        return
    try:
        _persist_hook(sess)
    except Exception:
        pass


def add_event(sess, etype, data):
    if etype not in EVENT_TYPES:
        raise ValueError(f"未声明的事件类型: {etype}，请先在 bridge/protocol.py EVENT_TYPES 中注册")
    with sess.event_lock:
        _touch_session(sess)
        sess.events.append({
            "id": len(sess.events), "type": etype,
            "data": data, "ts": datetime.now().isoformat(),
        })
    _persist_session(sess)


def add_history_event(sess, history_list, entry, event_type):
    """原子地追加历史记录并发送事件（统一快照锁）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未声明的事件类型: {event_type}，请先在 bridge/protocol.py EVENT_TYPES 中注册")
    with sess.event_lock:
        _touch_session(sess)
        history_list.append(entry)
        sess.events.append({
            "id": len(sess.events), "type": event_type,
            "data": entry, "ts": datetime.now().isoformat(),
        })
    _persist_session(sess)
