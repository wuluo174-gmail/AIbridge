#!/usr/bin/env python3
"""
Claude ↔ Codex 协商桥梁 v3 — 薄兼容 facade
=============================================
所有实现已迁入 bridge/ 子模块。
本文件保留:
  1. 模块级 re-exports（测试 self.mod.X 兼容）
  2. Plan 文件并发锁（测试直接 patch PLAN_LOCK_ACQUIRE_TIMEOUT）
  3. AdapterRegistry + 低层 adapter 单例 + CLI wrappers
  4. 高层角色 caller + 编排 wrappers（Step 7 角色化）
  5. Prompt DI wrappers（Universal DI Rule: 注入可 patch 的 helper）
  6. _BridgeProxy 接线 + main()

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
    get_session, add_event, add_history_event,
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
    build_claude_revise_prompt, build_codex_first_prompt,
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

# ── Plan 文件可靠关联 ──
import bridge.plan                                              # noqa: F401

# ── Orchestration engine ──
from bridge.orchestration import engine as _engine


# ═════════════════════════════════════════════════════════════════
# Plan 文件并发锁 — per-project 锁注册表
# ═════════════════════════════════════════════════════════════════
plan_file_locks = {}
plan_file_locks_lock = threading.Lock()
PLAN_LOCK_ACQUIRE_TIMEOUT = 0.1


def _get_plan_file_lock(project_path):
    with plan_file_locks_lock:
        lock = plan_file_locks.get(project_path)
        if lock is None:
            lock = threading.Lock()
            plan_file_locks[project_path] = lock
        return lock


def _acquire_plan_file_lock(project_path, stop_flag):
    lock = _get_plan_file_lock(project_path)
    while True:
        if lock.acquire(timeout=PLAN_LOCK_ACQUIRE_TIMEOUT):
            return lock
        if stop_flag.is_set():
            return None


# ═════════════════════════════════════════════════════════════════
# Step 7: AdapterRegistry + adapter 单例（模块级构造，import 时执行）
# ═════════════════════════════════════════════════════════════════
from bridge.adapters import (                                    # noqa: E402
    AdapterRegistry, RoleConfig,
    ClaudeCodeAdapter, CodexAdapter,
)

_registry = AdapterRegistry()
_registry.register("claude-code", ClaudeCodeAdapter,
                    plan_lock_acquire_fn=_acquire_plan_file_lock)
_registry.register("codex", CodexAdapter)

# adapter 单例 — 立即从 registry 取，与原 bridge.py:101-104 相同时序
_claude_adapter = _registry.get("claude-code")
_codex_adapter = _registry.get("codex")

_role_config = RoleConfig()  # 默认: planner=claude-code, reviewer=codex


# ═════════════════════════════════════════════════════════════════
# 低层 CLI Wrappers — 代码与现有完全一致，供测试和直接调用
# ═════════════════════════════════════════════════════════════════

def call_claude_streaming(prompt, cwd, sess, continue_session=False,
                          bypass_permissions=False, log_tag="claude",
                          skip_plan_detection=False):
    return _claude_adapter.run(
        prompt, cwd, sess, log_tag=log_tag,
        continue_session=continue_session,
        bypass_permissions=bypass_permissions,
        session_id=sess.claude_session_id,
        skip_plan_detection=skip_plan_detection,
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
    return sess


def get_role_config():
    """返回当前角色配置 dict。"""
    return dataclasses.asdict(_role_config)


def _load_role_config():
    """从 SQLite 加载角色配置。"""
    global _role_config
    if _store is not None:
        cfg = _store.load_role_config()
        if cfg:
            _role_config = RoleConfig(**cfg)


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


def _try_persist(sess):
    """终态时持久化到 SQLite。失败不阻断主流程。"""
    if _store is not None and sess.status in ("done", "error"):
        try:
            _store.save_session(sess)
        except Exception:
            pass


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

def list_archived_sessions(limit=50, offset=0):
    if _store is None:
        return []
    return _store.list_sessions(limit=limit, offset=offset)


def get_archived_session_history(session_id):
    if _store is None:
        return {"entries": [], "execution_result": None,
                "review_entries": [], "review_round": 0,
                "review_status": None, "event_cursor": 0,
                "planner_tool_id": "claude-code", "reviewer_tool_id": "codex"}
    sess_row = _store.get_session(session_id)
    raw_entries = _store.get_session_history(session_id)
    raw_review = _store.get_session_review_history(session_id)

    ptid = sess_row.get("planner_tool_id", "claude-code") if sess_row else "claude-code"
    rtid = sess_row.get("reviewer_tool_id", "codex") if sess_row else "codex"

    agent_to_tool = _build_agent_to_tool_map()

    def normalize_role(h):
        role = h["role"]
        if role in ("planner", "reviewer", "user"):
            return role
        # 旧数据: role 是 agent_name (如 "claude"/"codex")
        # 通过 registry 动态映射 agent_name → tool_id → planner/reviewer
        tid = agent_to_tool.get(role, role)
        if tid == ptid:
            return "planner"
        if tid == rtid:
            return "reviewer"
        return role

    entries = [{"round": h["round"], "role": normalize_role(h),
                "phase": h["phase"], "content": h["content"]}
               for h in raw_entries if h["role"] not in ("user",)]
    review_entries = [{"round": h["round"], "role": normalize_role(h),
                       "phase": h["phase"], "content": h["content"]}
                      for h in raw_review]
    review_round = max((h["round"] for h in raw_review), default=0)
    review_status = None
    if review_round > 0 and sess_row:
        review_status = {"round": review_round, "status": sess_row["final_status"]}
    return {
        "entries": entries,
        "execution_result": sess_row.get("execution_result") if sess_row else None,
        "review_entries": review_entries,
        "review_round": review_round,
        "review_status": review_status,
        "event_cursor": 0,
        "planner_tool_id": ptid,
        "reviewer_tool_id": rtid,
    }


# ═════════════════════════════════════════════════════════════════
# Prompt DI Wrappers (Universal DI Rule)
# ═════════════════════════════════════════════════════════════════
from bridge.orchestration.prompts import (                      # noqa: E402
    build_claude_first_prompt as _prompts_build_claude_first_prompt,
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
