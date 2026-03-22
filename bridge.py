#!/usr/bin/env python3
"""
Claude ↔ Codex 协商桥梁 v3 — 薄兼容 facade
=============================================
所有实现已迁入 bridge/ 子模块。
本文件保留:
  1. 模块级 re-exports（测试 self.mod.X 兼容）
  2. AdapterRegistry + 低层 adapter 单例 + CLI wrappers
  3. 高层角色 caller + 编排 wrappers（Step 7 角色化）
  4. Prompt DI wrappers（Universal DI Rule: 注入可 patch 的 helper）
  5. _BridgeProxy 接线 + main()

用法:
  python3 bridge.py                            # Web UI 模式 (默认)
  python3 bridge.py --port 9090                # 自定义端口
"""

import argparse
import dataclasses
import json as _json_mod
import os
import signal
import threading
import time
import uuid as _uuid_mod
import urllib.parse
from datetime import datetime
from pathlib import Path

# ── re-exports: bridge.protocol ──
from bridge.protocol import (
    EXECUTABLE_STATES, FIXABLE_STATES, CONTINUABLE_STATES,
    REVIEW_CONTINUABLE_STATES, REVIEW_SKIPPABLE_STATES,
    is_approved,
)

# ── re-exports: bridge.session ──
from bridge.session import (
    LOG_DIR, SessionState, sessions, sessions_lock,
    get_session, add_event, add_history_event, set_persist_hook,
)

# ── re-exports: bridge.git ──
from bridge.git import (
    _is_git_repo, capture_baseline_ref,
    capture_baseline_untracked, capture_execution_diff,
)

# ── re-exports: bridge.orchestration.prompts ──
from bridge.orchestration.prompts import (
    prompt_config,
    load_prompts as _json_load_prompts,
    save_prompts as _json_save_prompts,
    PROMPTS_FILE,
    detect_project_context, collect_user_injects,
    build_codex_first_prompt,
    build_codex_review_prompt, build_execution_prompt,
    build_claude_post_fix_prompt,
)
# 向后兼容别名
detect_claude_md = detect_project_context

# ── re-exports: bridge.server ──
from bridge.server import (
    ThreadedHTTPServer, BridgeHandler,
    load_recent_paths as _json_load_recent_paths,
    save_recent_paths as _json_save_recent_paths,
    RECENT_PATHS_FILE,
)

# ── Orchestration engine ──
from bridge.orchestration import engine as _engine


# ═════════════════════════════════════════════════════════════════
# Step 7: AdapterRegistry + adapter 单例（模块级构造，import 时执行）
# ═════════════════════════════════════════════════════════════════
from bridge.adapters import (                                    # noqa: E402
    AdapterRegistry, RoleConfig,
    ClaudeCodeAdapter, CodexAdapter,
)

_registry = AdapterRegistry()
_registry.register("claude-code", ClaudeCodeAdapter)
_registry.register("codex", CodexAdapter)

# adapter 单例 — 立即从 registry 取，与原 bridge.py:101-104 相同时序
_claude_adapter = _registry.get("claude-code")
_codex_adapter = _registry.get("codex")

_role_config = RoleConfig()  # 默认: planner=claude-code, reviewer=codex


# ═════════════════════════════════════════════════════════════════
# 低层 CLI Wrappers — 代码与现有完全一致，供测试和直接调用
# ═════════════════════════════════════════════════════════════════

def call_claude_streaming(prompt, cwd, sess, continue_session=False,
                          bypass_permissions=False, log_tag="claude"):
    return _claude_adapter.run(
        prompt, cwd, sess, log_tag=log_tag,
        continue_session=continue_session,
        bypass_permissions=bypass_permissions,
        session_id=sess.claude_session_id,
    )


def call_codex_streaming(prompt, cwd, sess, resume_last=False, log_tag="codex"):
    return _codex_adapter.run(
        prompt, cwd, sess, log_tag=log_tag,
        resume_last=resume_last,
    )


# ═════════════════════════════════════════════════════════════════
# 高层角色 caller — 供编排 wrappers 使用
# ═════════════════════════════════════════════════════════════════

def _make_role_caller(adapter, panel_label, state_key):
    """创建角色调用函数。

    - panel_label: "planner"/"reviewer"，控制事件中 agent 字段 → 前端面板路由
    - state_key: adapter_state 中的键，控制 session 复用
    - session 状态 (has_session) 由 caller 内部自动管理
    """
    def call(prompt, cwd, sess, **kwargs):
        state = sess.adapter_state.get(state_key, {})
        kwargs.setdefault("continue_session", state.get("has_session", False))
        kwargs.setdefault("resume_last", state.get("has_session", False))
        sid = state.get("session_id")
        if sid:
            kwargs.setdefault("session_id", sid)
        kwargs["agent_label"] = panel_label
        result = adapter.run(prompt, cwd, sess, **kwargs)
        sess.adapter_state.setdefault(state_key, {})["has_session"] = True
        return result
    return call


# ═════════════════════════════════════════════════════════════════
# 会话工厂 + 角色配置管理
# ═════════════════════════════════════════════════════════════════

def create_session(sid, task, project, rounds):
    """创建会话，按当前 _role_config 初始化 adapter 状态。"""
    sess = SessionState(sid, task, project, rounds)
    planner = _registry.get(_role_config.planner_tool_id)
    reviewer = _registry.get(_role_config.reviewer_tool_id)
    sess.planner_tool_id = planner.id
    sess.reviewer_tool_id = reviewer.id
    sess.planner_state_key = planner.id
    sess.reviewer_state_key = (
        f"{reviewer.id}:reviewer" if reviewer.id == planner.id
        else reviewer.id
    )
    sess.init_adapter_state(sess.planner_state_key, planner.capabilities)
    sess.init_adapter_state(sess.reviewer_state_key, reviewer.capabilities)
    sess.phase = "negotiation"
    sess.interrupt_reason = None
    return sess


def get_role_config():
    """返回当前角色配置 dict。"""
    return dataclasses.asdict(_role_config)


def _is_valid_role_config(cfg):
    """角色配置必须引用已注册工具，且至少一个工具具备执行能力。"""
    try:
        planner = _registry.get(cfg.planner_tool_id)
        reviewer = _registry.get(cfg.reviewer_tool_id)
    except KeyError:
        return False
    return any(
        adapter.capabilities.get("dangerous_mode")
        for adapter in (planner, reviewer)
    )


def _load_role_config():
    """从 SQLite 加载角色配置。"""
    global _role_config
    if _store is not None:
        cfg = _store.load_role_config()
        if cfg:
            loaded = RoleConfig(**cfg)
            if _is_valid_role_config(loaded):
                _role_config = loaded
            else:
                _role_config = RoleConfig()
                _store.save_role_config(
                    _role_config.planner_tool_id,
                    _role_config.reviewer_tool_id,
                )


def _update_role_config(planner_id, reviewer_id):
    """更新角色配置（全局 + 持久化）。"""
    global _role_config
    _role_config = RoleConfig(planner_tool_id=planner_id, reviewer_tool_id=reviewer_id)
    if _store is not None:
        _store.save_role_config(planner_id, reviewer_id)


def _resolve_executor_panel(sess):
    """返回 executor 面板标签。"""
    executor = _registry.resolve_executor(
        RoleConfig(sess.planner_tool_id, sess.reviewer_tool_id))
    return "planner" if executor.id == sess.planner_tool_id else "reviewer"


def _build_agent_to_tool_map():
    """从 registry 构建 agent_name → tool_id 映射。"""
    return {_registry.get(tid).agent_name: tid
            for tid in _registry.list_tool_ids()}


# ═════════════════════════════════════════════════════════════════
# 幂等执行角色解析 — 首次计算，后续缓存
# ═════════════════════════════════════════════════════════════════

def _resolve_execution_roles(sess):
    """解析执行阶段的角色分配 + 会话状态键。

    幂等：首次调用计算并缓存在 session 上，后续调用返回缓存值。
    adapter_state 条目只创建一次，不会被覆盖。
    """
    if getattr(sess, '_exec_roles_resolved', False):
        return (
            _registry.get(sess._exec_executor_id),
            sess._exec_executor_panel,
            sess._exec_state_key,
            _registry.get(sess._exec_reviewer_id),
            sess._exec_reviewer_panel,
            sess._exec_reviewer_state_key,
        )

    rc = RoleConfig(sess.planner_tool_id, sess.reviewer_tool_id)
    if not sess.planner_state_key:
        sess.planner_state_key = sess.planner_tool_id
    if not sess.reviewer_state_key:
        sess.reviewer_state_key = (
            sess.reviewer_tool_id
            if sess.reviewer_tool_id != sess.planner_tool_id
            else sess.planner_state_key
        )
    executor = _registry.resolve_executor(rc)
    exec_reviewer_id = (sess.reviewer_tool_id
                        if executor.id == sess.planner_tool_id
                        else sess.planner_tool_id)
    exec_reviewer = _registry.get(exec_reviewer_id)

    executor_panel = "planner" if executor.id == sess.planner_tool_id else "reviewer"
    er_panel = "reviewer" if exec_reviewer_id == sess.reviewer_tool_id else "planner"

    # 会话状态键: executor==planner → 复用协商 session; 否则独立 session
    if executor.id == sess.planner_tool_id:
        exec_state_key = sess.planner_state_key
    else:
        exec_state_key = f"{executor.id}:exec"
        if exec_state_key not in sess.adapter_state:
            sess.adapter_state[exec_state_key] = {
                "has_session": False,
                "session_id": str(_uuid_mod.uuid4()) if executor.capabilities.get("session_resume") else None,
            }

    if exec_reviewer_id == sess.reviewer_tool_id:
        er_state_key = sess.reviewer_state_key
    else:
        er_state_key = f"{exec_reviewer_id}:exec_review"
        if er_state_key not in sess.adapter_state:
            sess.adapter_state[er_state_key] = {
                "has_session": False,
                "session_id": str(_uuid_mod.uuid4()) if exec_reviewer.capabilities.get("session_resume") else None,
            }

    sess._exec_executor_id = executor.id
    sess._exec_executor_panel = executor_panel
    sess._exec_state_key = exec_state_key
    sess._exec_reviewer_id = exec_reviewer_id
    sess._exec_reviewer_panel = er_panel
    sess._exec_reviewer_state_key = er_state_key
    sess._exec_roles_resolved = True

    return (executor, executor_panel, exec_state_key,
            exec_reviewer, er_panel, er_state_key)


# ═════════════════════════════════════════════════════════════════
# SQLite 持久化 — Store 单例 + 迁移 + load/save wrappers
# ═════════════════════════════════════════════════════════════════
from bridge.persistence.store import Store                         # noqa: E402

_store = None  # 懒初始化，main() 中调用 init_store()


def _log_tool_probe_drift(old_tools, new_tools):
    """输出工具安装状态/版本变化日志。"""
    old_by_id = {tool["id"]: tool for tool in old_tools}
    for tool in new_tools:
        old = old_by_id.get(tool["id"])
        if old is None:
            continue

        if bool(old.get("detected_installed")) != bool(tool.get("detected_installed")):
            prev = "已安装" if old.get("detected_installed") else "未安装"
            cur = "已安装" if tool.get("detected_installed") else "未安装"
            print(f"[bridge] 工具 {tool['id']} 安装状态变化: {prev} -> {cur}")

        if old.get("version") != tool.get("version"):
            prev_ver = old.get("version") or "(unknown)"
            cur_ver = tool.get("version") or "(unknown)"
            print(f"[bridge] 工具 {tool['id']} 版本变化: {prev_ver} -> {cur_ver}")


def init_store(db_path=None):
    """初始化持久化层。在 main() 中调用，测试可传 :memory:。"""
    global _store
    if _store is not None:
        _store.close()
    _store = Store(db_path)
    set_persist_hook(_persist_session)
    old_tool_snapshots = _store.list_tools()
    _registry.probe_all()
    tool_snapshots = _registry.discover()
    _log_tool_probe_drift(old_tool_snapshots, tool_snapshots)
    for tool in tool_snapshots:
        _store.register_tool(
            tool["id"],
            tool["display_name"],
            _json_mod.dumps(tool["capabilities"]),
            agent_name=tool["agent_name"],
            detected_installed=tool["detected_installed"],
            executable_path=tool["executable_path"],
            version=tool["version"],
            probe_error=tool["probe_error"],
            last_checked_at=tool["last_checked_at"],
        )
    # 加载角色配置
    _load_role_config()
    # JSON → SQLite 迁移（每个域独立，仅未完成时尝试）
    _migrate_json_to_sqlite()
    _store.mark_incomplete_sessions_interrupted()
    # 从权威源重建 prompt_config 内存状态
    if _store.is_migration_complete("prompts"):
        prompt_config.clear()
        prompt_config.update(_store.load_prompts())


def _migrate_json_to_sqlite():
    """将 JSON 文件导入 SQLite。按域独立，成功才标记，失败可重试。"""
    import json
    # prompts 域
    if not _store.is_migration_complete("prompts"):
        try:
            if PROMPTS_FILE.exists():
                data = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
                if data:
                    _store.save_prompts(data)
            _store.mark_migration_complete("prompts")
        except Exception:
            pass
    # recent_paths 域
    if not _store.is_migration_complete("recent_paths"):
        try:
            if RECENT_PATHS_FILE.exists():
                paths = json.loads(RECENT_PATHS_FILE.read_text(encoding="utf-8"))
                if paths:
                    _store.save_recent_paths(paths)
            _store.mark_migration_complete("recent_paths")
        except Exception:
            pass


def _persist_session(sess):
    """将 session 快照写入统一会话账本。"""
    if _store is None:
        return
    try:
        _store.save_session(sess)
    except Exception:
        pass


def _try_persist(sess):
    """兼容旧调用点，统一落到全生命周期持久化。"""
    _persist_session(sess)


# ── load/save wrappers：基于 is_migration_complete 判定权威源 ──

def load_prompts():
    if _store is not None and _store.is_migration_complete("prompts"):
        return _store.load_prompts()
    return _json_load_prompts()


def save_prompts(data):
    if _store is not None:
        _store.save_prompts(data)
        if not _store.is_migration_complete("prompts"):
            _store.mark_migration_complete("prompts")
        return
    _json_save_prompts(data)


def load_recent_paths():
    if _store is not None and _store.is_migration_complete("recent_paths"):
        return _store.load_recent_paths()
    return _json_load_recent_paths()


def save_recent_paths(paths):
    if _store is not None:
        _store.save_recent_paths(paths)
        if not _store.is_migration_complete("recent_paths"):
            _store.mark_migration_complete("recent_paths")
        return
    _json_save_recent_paths(paths)


# ── 归档查询 ──

def _normalize_role(role, planner_tool_id, reviewer_tool_id):
    if role in ("planner", "reviewer", "user"):
        return role
    agent_to_tool = _build_agent_to_tool_map()
    tid = agent_to_tool.get(role, role)
    if tid == planner_tool_id:
        return "planner"
    if tid == reviewer_tool_id:
        return "reviewer"
    return role


def _session_history_payload(sess):
    ptid = getattr(sess, "planner_tool_id", "claude-code")
    rtid = getattr(sess, "reviewer_tool_id", "codex")
    entries = [{
        "round": h["round"],
        "role": _normalize_role(h["role"], ptid, rtid),
        "phase": h["phase"],
        "content": h["content"],
    } for h in sess.history if h["role"] not in ("user",)]
    review_entries = [{
        "round": h["round"],
        "role": _normalize_role(h["role"], ptid, rtid),
        "phase": h["phase"],
        "content": h["content"],
    } for h in sess.review_history]
    review_status = None
    if sess.phase == "review" or sess.review_round > 0 or str(sess.status).startswith("review_"):
        review_status = {"round": sess.review_round, "status": sess.status}
    return {
        "entries": entries,
        "execution_result": sess.execution_result,
        "review_entries": review_entries,
        "review_round": sess.review_round,
        "review_status": review_status,
        "event_cursor": len(sess.events),
        "planner_tool_id": ptid,
        "reviewer_tool_id": rtid,
    }


def _serialize_session_state(sess):
    return {
        "status": sess.status,
        "round": sess.current_round,
        "max_rounds": sess.max_rounds,
        "consensus": sess.consensus,
        "consensus_round": sess.consensus_round,
        "history_len": len(sess.history),
        "error": sess.error,
        "planner_tool_id": sess.planner_tool_id,
        "reviewer_tool_id": sess.reviewer_tool_id,
        "executor_panel": _resolve_executor_panel(sess),
        "review_round": sess.review_round,
        "max_review_rounds": sess.max_review_rounds,
        "phase": getattr(sess, "phase", "negotiation"),
        "updated_at": getattr(sess, "updated_at", None),
        "finished_at": getattr(sess, "finished_at", None),
        "interrupt_reason": getattr(sess, "interrupt_reason", None),
        "resume_available": getattr(sess, "resume_available", False),
    }


def _restore_session(session_id):
    if _store is None:
        return None
    row = _store.get_session(session_id)
    if row is None:
        return None

    sess = SessionState(row["id"], row["task"], row["project_path"], row["max_rounds"])
    sess.status = row.get("status") or "done"
    sess.phase = row.get("phase") or "negotiation"
    sess.current_round = row.get("current_round", 0)
    sess.consensus = bool(row.get("consensus"))
    sess.consensus_round = row.get("consensus_round", 0)
    sess.execution_result = row.get("execution_result")
    sess.error = row.get("error")
    sess.review_round = row.get("review_round", 0)
    sess.max_review_rounds = row.get("max_review_rounds", 3)
    sess.interrupt_reason = row.get("interrupt_reason")
    sess.created_at = row.get("created_at") or sess.created_at
    sess.updated_at = row.get("updated_at") or sess.created_at
    sess.finished_at = row.get("finished_at")
    sess.planner_tool_id = row.get("planner_tool_id", "claude-code")
    sess.reviewer_tool_id = row.get("reviewer_tool_id", "codex")
    sess.planner_state_key = sess.planner_tool_id
    sess.reviewer_state_key = (
        f"{sess.reviewer_tool_id}:reviewer"
        if sess.reviewer_tool_id == sess.planner_tool_id else sess.reviewer_tool_id
    )
    adapter_state_raw = row.get("adapter_state_json") or "{}"
    try:
        sess.adapter_state = _json_mod.loads(adapter_state_raw)
    except Exception:
        sess.adapter_state = {}
    if sess.planner_state_key not in sess.adapter_state:
        sess.init_adapter_state(sess.planner_state_key, _registry.get(sess.planner_tool_id).capabilities)
    if sess.reviewer_state_key not in sess.adapter_state:
        sess.init_adapter_state(sess.reviewer_state_key, _registry.get(sess.reviewer_tool_id).capabilities)
    sess.history = _store.get_session_history(session_id)
    sess.review_history = _store.get_session_review_history(session_id)
    sess.events = _store.get_session_events(session_id)
    return sess


def get_or_load_session(session_id, cache=True):
    if not session_id:
        return None
    sess = get_session(session_id)
    if sess is not None:
        return sess
    sess = _restore_session(session_id)
    if sess is not None and cache:
        with sessions_lock:
            sessions[session_id] = sess
    return sess


def list_sessions(limit=50, offset=0):
    if _store is None:
        with sessions_lock:
            return [{
                "session_id": s.session_id,
                "task": s.task,
                "project_path": s.project_path,
                "status": s.status,
                "phase": getattr(s, "phase", "negotiation"),
                "round": s.current_round,
                "max_rounds": s.max_rounds,
                "updated_at": getattr(s, "updated_at", s.created_at),
                "finished_at": getattr(s, "finished_at", None),
                "interrupt_reason": getattr(s, "interrupt_reason", None),
                "resume_available": getattr(s, "resume_available", False),
                "planner_tool_id": s.planner_tool_id,
                "reviewer_tool_id": s.reviewer_tool_id,
                "consensus": s.consensus,
                "consensus_round": s.consensus_round,
                "created_at": s.created_at,
            } for s in sessions.values()]
    rows = []
    for row in _store.list_sessions(limit=limit, offset=offset):
        rows.append({
            "session_id": row["session_id"],
            "task": row["task"],
            "project_path": row["project_path"],
            "status": row["status"],
            "phase": row["phase"],
            "round": row["round"],
            "max_rounds": row["max_rounds"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "interrupt_reason": row["interrupt_reason"],
            "resume_available": row["resume_available"],
            "planner_tool_id": row["planner_tool_id"],
            "reviewer_tool_id": row["reviewer_tool_id"],
            "consensus": row["consensus"],
            "consensus_round": row["consensus_round"],
            "created_at": row["created_at"],
        })
    return rows


# ═════════════════════════════════════════════════════════════════
# Prompt DI Wrappers (Universal DI Rule)
# ═════════════════════════════════════════════════════════════════
from bridge.orchestration.prompts import (                      # noqa: E402
    build_claude_first_prompt as _prompts_build_claude_first_prompt,
    build_claude_revise_prompt as _prompts_build_claude_revise_prompt,
    build_codex_post_review_prompt as _prompts_build_codex_post_review_prompt,
    build_codex_post_review_followup_prompt as _prompts_build_codex_post_review_followup_prompt,
)


def build_claude_first_prompt(task, cwd):
    adapter = _registry.get(_role_config.planner_tool_id)
    return _prompts_build_claude_first_prompt(
        task, cwd,
        planner_name=adapter.display_name,
        _detect_context=detect_project_context,
        _adapter=adapter)


def build_claude_revise_prompt(codex_feedback, user_injects=None, cwd=None):
    adapter = _registry.get(_role_config.planner_tool_id)
    return _prompts_build_claude_revise_prompt(
        codex_feedback, user_injects=user_injects, cwd=cwd,
        _detect_context=detect_project_context,
        _adapter=adapter)


def build_codex_post_review_prompt(sess, task, approved_plan, execution_result):
    return _prompts_build_codex_post_review_prompt(
        sess, task, approved_plan, execution_result,
        _capture_diff=capture_execution_diff)


def build_codex_post_review_followup_prompt(sess, fix_result):
    return _prompts_build_codex_post_review_followup_prompt(
        sess, fix_result,
        _capture_diff=capture_execution_diff)


# ═════════════════════════════════════════════════════════════════
# Orchestration Wrappers — 角色化编排
# ═════════════════════════════════════════════════════════════════
last_complete_round = _engine.last_complete_round


def run_negotiation(sess, start_round=1):
    planner = _registry.get(sess.planner_tool_id)
    reviewer_adapter = _registry.get(sess.reviewer_tool_id)

    _engine.run_negotiation(
        sess, start_round=start_round,
        call_planner=_make_role_caller(planner, "planner", sess.planner_state_key),
        call_reviewer=_make_role_caller(reviewer_adapter, "reviewer", sess.reviewer_state_key),
        reviewer_adapter=reviewer_adapter,
        build_planner_first_prompt=build_claude_first_prompt,
        build_planner_revise_prompt=build_claude_revise_prompt,
        build_reviewer_first_prompt=build_codex_first_prompt,
        build_reviewer_review_prompt=build_codex_review_prompt,
        collect_user_injects=collect_user_injects,
    )
    _try_persist(sess)


def run_execution(sess):
    (executor, ep, esk, exec_reviewer, erp, ersk) = _resolve_execution_roles(sess)

    def _first_review_bound(s, plan):
        _engine.run_first_review(
            s, plan,
            call_exec_reviewer=_make_role_caller(exec_reviewer, erp, ersk),
            exec_reviewer_panel=erp,
            exec_reviewer_adapter=exec_reviewer,
            build_post_review_prompt=build_codex_post_review_prompt,
        )

    _engine.run_execution(
        sess,
        call_executor=_make_role_caller(executor, ep, esk),
        executor_panel=ep,
        _is_git_repo=_is_git_repo,
        capture_baseline_ref=capture_baseline_ref,
        capture_baseline_untracked=capture_baseline_untracked,
        build_execution_prompt=build_execution_prompt,
        _run_first_review=_first_review_bound,
    )
    _try_persist(sess)


def run_review_fix_cycle(sess):
    (executor, ep, esk, exec_reviewer, erp, ersk) = _resolve_execution_roles(sess)
    _engine.run_review_fix_cycle(
        sess,
        call_executor=_make_role_caller(executor, ep, esk),
        executor_panel=ep,
        call_exec_reviewer=_make_role_caller(exec_reviewer, erp, ersk),
        exec_reviewer_panel=erp,
        exec_reviewer_adapter=exec_reviewer,
        build_fix_prompt=build_claude_post_fix_prompt,
        build_followup_prompt=build_codex_post_review_followup_prompt,
    )
    _try_persist(sess)


def _latest_planner_plan(sess):
    for h in reversed(sess.history):
        if h["role"] == "planner":
            return h["content"]
    return ""


def run_first_review_only(sess):
    (_, _, _, exec_reviewer, erp, ersk) = _resolve_execution_roles(sess)
    _engine.run_first_review(
        sess,
        _latest_planner_plan(sess),
        call_exec_reviewer=_make_role_caller(exec_reviewer, erp, ersk),
        exec_reviewer_panel=erp,
        exec_reviewer_adapter=exec_reviewer,
        build_post_review_prompt=build_codex_post_review_prompt,
    )
    _try_persist(sess)


def run_review_followup_only(sess):
    (_, _, _, exec_reviewer, erp, ersk) = _resolve_execution_roles(sess)
    try:
        with sess.status_lock:
            sess.status = "review_pending"
            sess.phase = "review"
            sess.interrupt_reason = None
        add_event(sess, "status_change", {
            "status": "review_pending",
            "msg": f"审查修复轮 {sess.review_round}...",
            "msg_key": "be.review_fix_round",
            "msg_params": {"round": sess.review_round},
        })
        add_event(sess, "agent_thinking", {"agent": erp, "round": sess.review_round})
        review = _make_role_caller(exec_reviewer, erp, ersk)(
            build_codex_post_review_followup_prompt(sess, sess.execution_result or ""),
            sess.project_path,
            sess,
            log_tag=f"exec_reviewer_{sess.review_round}",
        )
        if sess.stop_flag.is_set():
            return
        review_entry = {
            "round": sess.review_round,
            "role": erp,
            "phase": "执行审查",
            "content": review,
            "timestamp": datetime.now().isoformat(),
        }
        add_history_event(sess, sess.review_history, review_entry, "review_response")
        if exec_reviewer.detect_closure(review):
            with sess.status_lock:
                sess.status = "done"
                sess.phase = "review"
            add_event(sess, "review_done", {
                "round": sess.review_round,
                "msg": f"第 {sess.review_round} 轮确认任务收口成功。",
                "success": True,
                "msg_key": "be.closure_round_ok",
                "msg_params": {"round": sess.review_round},
            })
        else:
            with sess.status_lock:
                sess.status = "review_fix"
                sess.phase = "review"
            add_event(sess, "review_needs_fix", {
                "round": sess.review_round,
                "msg": "仍发现问题，等待你确认是否继续修复。",
                "review": review,
                "msg_key": "be.still_needs_fix",
            })
    except Exception as e:
        with sess.status_lock:
            sess.status = "error"
            sess.error = str(e)
            sess.phase = "review"
        add_event(sess, "error", {
            "msg": f"修复阶段出错: {e}",
            "msg_key": "be.error_fix",
            "msg_params": {"detail": str(e)},
        })
    _try_persist(sess)


def resume_session(sess):
    """恢复 paused / interrupted 会话到最近的可执行检查点。"""
    if sess.status not in {"paused", "interrupted"}:
        raise RuntimeError("当前会话不可恢复")

    sess.stop_flag.clear()
    sess.interrupt_reason = None

    if sess.phase == "negotiation":
        start_round = max(1, last_complete_round(sess.history) + 1)
        threading.Thread(
            target=run_negotiation,
            args=(sess,),
            kwargs={"start_round": start_round},
            daemon=True,
        ).start()
        return

    if sess.phase == "execution":
        threading.Thread(target=run_execution, args=(sess,), daemon=True).start()
        return

    if sess.phase == "review":
        last_review_entry = sess.review_history[-1] if sess.review_history else None
        if last_review_entry and last_review_entry["role"] == "planner":
            threading.Thread(target=run_review_followup_only, args=(sess,), daemon=True).start()
        else:
            threading.Thread(target=run_first_review_only, args=(sess,), daemon=True).start()
        return

    raise RuntimeError(f"未知会话阶段: {sess.phase}")


# ═════════════════════════════════════════════════════════════════
# _BridgeProxy 接线 — 将本模块 globals() 注入 server.py
# ═════════════════════════════════════════════════════════════════
import bridge.server as _server                                 # noqa: E402
_server._b._ns = globals()


# ═════════════════════════════════════════════════════════════════
# 进程清理 — 所有退出路径统一入口
# ═════════════════════════════════════════════════════════════════

def _shutdown_all_sessions():
    """SIGTERM 每个活跃 CLI 进程组，返回 pgid 列表供 _ensure_dead 使用。"""
    pgids = []
    with sessions_lock:
        for sess in sessions.values():
            sess.stop_flag.set()
            with sess.proc_lock:
                pgid = sess.active_pgid
            if pgid:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    pgids.append(pgid)
                except (ProcessLookupError, PermissionError):
                    with sess.proc_lock:
                        sess.active_pgid = None
    return pgids


def _ensure_dead(pgids, timeout=3):
    """等待 SIGTERM 生效，SIGKILL 存活者。组级探测，确定性保证。"""
    deadline = time.time() + timeout
    remaining = list(pgids)
    while remaining and time.time() < deadline:
        time.sleep(0.3)
        alive = []
        for pgid in remaining:
            try:
                os.killpg(pgid, 0)
                alive.append(pgid)
            except (ProcessLookupError, PermissionError):
                pass
        remaining = alive
    for pgid in remaining:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ═════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Claude ↔ Codex 协商桥梁")
    parser.add_argument("--port", type=int, default=8686)
    parser.add_argument("--project", type=str, help="预设项目路径")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器（桌面壳模式）")
    parser.add_argument("--log-dir", type=str,
                        help="日志目录（桌面壳传入，覆盖默认 /tmp/bridge-logs）")
    args = parser.parse_args()

    if args.log_dir:
        import bridge.session
        bridge.session.LOG_DIR = Path(args.log_dir)

    project_note = ""
    if args.project:
        project_note = f"?project={urllib.parse.quote(args.project)}"

    init_store()
    import bridge.session as _sess_mod
    _sess_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadedHTTPServer(("0.0.0.0", args.port), BridgeHandler)

    # SIGTERM handler — Tauri 正常退出时发送
    def _sigterm_handler(signum, frame):
        pgids = _shutdown_all_sessions()
        _ensure_dead(pgids)
        server.shutdown()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # 孤儿检测 watchdog — Tauri Force Quit 场景
    def _orphan_watchdog():
        original_ppid = os.getppid()
        while True:
            time.sleep(2)
            if os.getppid() != original_ppid:
                pgids = _shutdown_all_sessions()
                _ensure_dead(pgids)
                os._exit(0)

    threading.Thread(target=_orphan_watchdog, daemon=True).start()

    dist_dir = Path(__file__).parent / "frontend" / "dist"
    if not dist_dir.is_dir():
        import sys
        print("[WARN] frontend/dist not found, serving embedded HTML_UI fallback", file=sys.stderr)

    print(f"""
╔═══════════════════════════════════════════════╗
║  Claude ↔ Codex Bridge  v3                    ║
║  http://localhost:{args.port}/{project_note:<28}║
║  多 Tab 并发协商 · Ctrl+C 退出               ║
╚═══════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pgids = _shutdown_all_sessions()
        _ensure_dead(pgids)
        print("\n已退出。")
        server.shutdown()


if __name__ == "__main__":
    main()
