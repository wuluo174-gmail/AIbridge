#!/usr/bin/env python3
"""
Bridge v4
=========
四角色统一账本 + 会话级双模式工作台入口。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from bridge.adapters import AdapterRegistry, ClaudeCodeAdapter, CodexAdapter, FixtureAdapter
from bridge.git import _is_git_repo, capture_baseline_ref, capture_baseline_untracked, capture_execution_diff
from bridge.orchestration import engine as _engine
from bridge.orchestration.prompts import (
    PROMPTS_FILE,
    build_claude_first_prompt as _build_planner_first_prompt,
    build_claude_post_fix_prompt,
    build_claude_revise_prompt as _build_planner_revise_prompt,
    build_codex_first_prompt as _build_reviewer_first_prompt,
    build_codex_post_review_followup_prompt,
    build_codex_post_review_prompt,
    build_codex_review_prompt as _build_reviewer_review_prompt,
    build_execution_prompt,
    detect_project_context,
    load_prompts as _json_load_prompts,
    prompt_config,
    save_prompts as _json_save_prompts,
)
from bridge.persistence.store import Store
from bridge.projections import project_scene, project_terminal
from bridge.server import (
    BridgeHandler,
    RECENT_PATHS_FILE,
    ThreadedHTTPServer,
    load_recent_paths as _json_load_recent_paths,
    save_recent_paths as _json_save_recent_paths,
)
from bridge.session import (
    LOG_DIR,
    SessionState,
    add_event,
    add_intervention,
    event_snapshot,
    get_session,
    last_complete_round,
    latest_artifact,
    publish_artifact,
    register_session,
    remove_session,
    set_lane_viewport,
    session_event_payload,
    sessions,
    sessions_lock,
    set_persist_hook,
    touch_status,
)
from bridge.workflow import (
    ROLE_KEYS,
    VIEW_MODES,
    WorkflowConfig,
    normalize_workflow_config,
    role_tool_map,
    target_roles_for_stage,
    workflow_config_to_dict,
)


_registry = AdapterRegistry()
_registry.register("claude-code", ClaudeCodeAdapter)
_registry.register("codex", CodexAdapter)
_store = None
_default_workflow_config = normalize_workflow_config(None)
_fixture_tools_enabled = False


def enable_fixture_tools() -> None:
    global _fixture_tools_enabled
    if _fixture_tools_enabled:
        return
    _registry.register("fixture-cli", FixtureAdapter)
    _fixture_tools_enabled = True


def load_prompts():
    if _store is not None:
        data = _store.load_prompts()
        if data:
            return data
    return _json_load_prompts()


def save_prompts(data):
    prompt_config.clear()
    prompt_config.update(data)
    if _store is not None:
        _store.save_prompts(data)
    else:
        _json_save_prompts(data)


def load_recent_paths():
    if _store is not None:
        return _store.load_recent_paths()
    return _json_load_recent_paths()


def save_recent_paths(paths):
    if _store is not None:
        _store.save_recent_paths(paths)
    else:
        _json_save_recent_paths(paths)


def _persist_session(sess):
    if _store is None:
        return
    _store.save_session(sess)


def init_store(db_path=None):
    global _store, _default_workflow_config
    if _store is not None:
        _store.close()
    _store = Store(db_path)
    set_persist_hook(_store)
    _registry.probe_all()
    for tool in _registry.discover():
        _store.register_tool(
            tool["id"],
            tool["display_name"],
            tool["capabilities"],
            agent_name=tool["agent_name"],
            detected_installed=tool["detected_installed"],
            executable_path=tool["executable_path"],
            version=tool["version"],
            probe_error=tool["probe_error"],
            last_checked_at=tool["last_checked_at"],
        )
    stored_prompts = _store.load_prompts()
    if stored_prompts:
        prompt_config.clear()
        prompt_config.update(stored_prompts)
    workflow_data = _store.load_workflow_config()
    _default_workflow_config = normalize_workflow_config(workflow_data)
    _store.mark_incomplete_sessions_interrupted()


def list_tools():
    return _store.list_tools() if _store is not None else _registry.discover()


def _validate_workflow_config(config: WorkflowConfig):
    available = set(_registry.list_tool_ids())
    for role in config.roles:
        if role.tool_id not in available:
            raise ValueError(f"未知工具: {role.tool_id}")


def get_workflow_config(sess=None):
    if sess is not None:
        return dict(sess.workflow_config)
    return workflow_config_to_dict(_default_workflow_config)


def update_workflow_config(data):
    global _default_workflow_config
    merged = workflow_config_to_dict(_default_workflow_config)
    merged.update(data or {})
    if "roles" not in merged:
        merged["roles"] = workflow_config_to_dict(_default_workflow_config)["roles"]
    config = normalize_workflow_config(merged)
    _validate_workflow_config(config)
    _default_workflow_config = config
    payload = workflow_config_to_dict(config)
    if _store is not None:
        _store.save_workflow_config(payload)
    return payload


def _wire_role_metadata(sess):
    result = []
    for role_key in ROLE_KEYS:
        lane = sess.roles[role_key]
        adapter = _registry.get(lane.tool_id)
        item = lane.to_wire()
        item["display_name"] = adapter.display_name
        result.append(item)
    return result


def serialize_session_state(sess):
    return sess.to_public_state()


def empty_session_state():
    return {
        "session_id": None,
        "task": "",
        "project_path": "",
        "workflow_template": _default_workflow_config.workflow_template,
        "view_mode": _default_workflow_config.view_mode,
        "status": "idle",
        "active_stage": "planning",
        "current_round": 0,
        "current_review_round": 0,
        "consensus_round": 0,
        "max_rounds": _default_workflow_config.max_rounds,
        "max_review_rounds": _default_workflow_config.max_review_rounds,
        "error": None,
        "interrupt_reason": None,
        "created_at": None,
        "updated_at": None,
        "finished_at": None,
        "resume_available": False,
    }


def history_payload(sess):
    return {
        "session": serialize_session_state(sess),
        "roles": _wire_role_metadata(sess),
        "events": [event_snapshot(event) for event in sess.stream_events],
        "artifacts": list(sess.artifacts),
        "interventions": list(sess.interventions),
        "projections": {
            "terminal": project_terminal(sess.stream_events),
            "scene": project_scene(sess.stream_events, sess.artifacts, sess.interventions),
        },
        "lane_cursors": {role_key: lane.last_seq for role_key, lane in sess.roles.items()},
        "stream_cursor": len(sess.stream_events),
    }


def empty_history_payload():
    return {
        "session": empty_session_state(),
        "roles": [],
        "events": [],
        "artifacts": [],
        "interventions": [],
        "projections": {"terminal": {}, "scene": []},
        "lane_cursors": {},
        "stream_cursor": 0,
    }


def _restore_session(session_id):
    if _store is None:
        return None
    bundle = _store.get_session_bundle(session_id)
    if bundle is None:
        return None
    sess = SessionState.from_persisted(
        bundle["session"],
        bundle["workflow"],
        bundle["roles"],
        bundle["lanes"],
        bundle["events"],
        bundle["artifacts"],
        bundle["interventions"],
    )
    return sess


def get_or_load_session(session_id):
    if not session_id:
        return None
    sess = get_session(session_id)
    if sess is not None:
        return sess
    sess = _restore_session(session_id)
    if sess is not None:
        register_session(sess)
    return sess


def list_sessions(limit=50, offset=0):
    if _store is not None:
        return _store.list_sessions(limit, offset)
    with sessions_lock:
        return [
            {
                "session_id": sess.session_id,
                "task": sess.task,
                "project_path": sess.project_path,
                "view_mode": sess.view_mode,
                "status": sess.status,
                "active_stage": sess.active_stage,
                "current_round": sess.current_round,
                "current_review_round": sess.current_review_round,
                "max_rounds": sess.max_rounds,
                "max_review_rounds": sess.max_review_rounds,
                "updated_at": sess.updated_at,
                "created_at": sess.created_at,
                "finished_at": sess.finished_at,
                "interrupt_reason": sess.interrupt_reason,
            }
            for sess in sessions.values()
        ]


def _init_lane_resume_states(sess):
    for lane in sess.roles.values():
        adapter = _registry.get(lane.tool_id)
        if adapter.capabilities.get("session_resume"):
            lane.resume_state = {
                "has_session": False,
                "session_id": str(uuid.uuid4()),
            }
        else:
            lane.resume_state = {"has_session": False}


def _make_role_caller(sess):
    def call(role_key, prompt, cwd, sess_obj, **kwargs):
        lane = sess_obj.roles[role_key]
        adapter = _registry.get(lane.tool_id)
        state = lane.resume_state
        kwargs.setdefault("continue_session", state.get("has_session", False))
        kwargs.setdefault("resume_last", state.get("has_session", False))
        if state.get("session_id"):
            kwargs.setdefault("session_id", state["session_id"])
        kwargs["role_key"] = role_key
        result = adapter.run(prompt, cwd, sess_obj, **kwargs)
        lane.resume_state["has_session"] = True
        return result

    return call


def _role_adapters(sess):
    return {role_key: _registry.get(sess.roles[role_key].tool_id) for role_key in ROLE_KEYS}


def _build_planner_first(task, cwd, sess):
    adapter = _registry.get(sess.roles["planner"].tool_id)
    return _build_planner_first_prompt(
        task,
        cwd,
        planner_name=adapter.display_name,
        _detect_context=detect_project_context,
        _adapter=adapter,
    )


def _build_planner_revise(feedback, interventions, cwd, sess):
    adapter = _registry.get(sess.roles["planner"].tool_id)
    return _build_planner_revise_prompt(
        feedback,
        user_injects=interventions,
        cwd=cwd,
        _detect_context=detect_project_context,
        _adapter=adapter,
    )


def _build_validator_prompt(sess, task, approved_plan, execution_result):
    return build_codex_post_review_prompt(
        sess,
        task,
        approved_plan,
        execution_result,
        _capture_diff=capture_execution_diff,
    )


def _build_validator_followup(sess, fix_result):
    return build_codex_post_review_followup_prompt(
        sess,
        fix_result,
        _capture_diff=capture_execution_diff,
    )


def _run_negotiation_thread(sess, start_round):
    _engine.run_negotiation(
        sess,
        start_round=start_round,
        call_role=_make_role_caller(sess),
        role_adapters=_role_adapters(sess),
        build_planner_first_prompt=lambda task, cwd: _build_planner_first(task, cwd, sess),
        build_planner_revise_prompt=lambda feedback, interventions, cwd: _build_planner_revise(feedback, interventions, cwd, sess),
        build_reviewer_first_prompt=_build_reviewer_first_prompt,
        build_reviewer_review_prompt=_build_reviewer_review_prompt,
    )


def _run_execution_thread(sess):
    _engine.run_execution(
        sess,
        call_role=_make_role_caller(sess),
        role_adapters=_role_adapters(sess),
        _is_git_repo=_is_git_repo,
        capture_baseline_ref=capture_baseline_ref,
        capture_baseline_untracked=capture_baseline_untracked,
        build_execution_prompt=build_execution_prompt,
        build_post_review_prompt=_build_validator_prompt,
    )


def _run_review_fix_thread(sess):
    _engine.run_review_fix_cycle(
        sess,
        call_role=_make_role_caller(sess),
        role_adapters=_role_adapters(sess),
        build_fix_prompt=build_claude_post_fix_prompt,
        build_followup_prompt=_build_validator_followup,
    )


def start_session(project_path, task, body):
    merged = workflow_config_to_dict(_default_workflow_config)
    merged["max_rounds"] = int(body.get("max_rounds", merged["max_rounds"]))
    merged["max_review_rounds"] = int(body.get("max_review_rounds", merged["max_review_rounds"]))
    if body.get("view_mode"):
        merged["view_mode"] = body["view_mode"]
    if body.get("roles"):
        merged["roles"] = body["roles"]
    workflow = normalize_workflow_config(merged)
    _validate_workflow_config(workflow)
    sess = SessionState(uuid.uuid4().hex[:8], task, project_path, workflow)
    _init_lane_resume_states(sess)
    register_session(sess)
    save_recent_paths([project_path, *[path for path in load_recent_paths() if path != project_path]][:10])
    _persist_session(sess)
    threading.Thread(target=_run_negotiation_thread, args=(sess, 1), daemon=True).start()
    return sess


def pause_session(sess):
    touch_status(sess, status="paused", active_stage=sess.active_stage, interrupt_reason="user_paused")
    add_event(
        sess,
        "session.status_changed",
        session_event_payload(sess, status="paused", message="用户暂停了会话。"),
        source="workflow",
    )


def abort_session(sess):
    touch_status(sess, status="aborted", active_stage=sess.active_stage, interrupt_reason="user_aborted")
    add_event(
        sess,
        "session.status_changed",
        session_event_payload(sess, status="aborted", message="用户中止了会话。"),
        source="workflow",
    )


def execute_session(sess):
    if sess.status not in {"consensus", "max_rounds"}:
        return "当前状态不可执行"
    sess.stop_flag.clear()
    threading.Thread(target=_run_execution_thread, args=(sess,), daemon=True).start()
    return None


def continue_session(sess, body):
    if sess.status not in {"consensus", "max_rounds"}:
        return "当前状态不可继续协商"
    extra_rounds = max(1, min(int(body.get("extra_rounds", 3)), 20))
    reason = (body.get("message") or "").strip()
    if sess.status == "consensus" and not reason:
        return "驳回共识时必须提供理由"
    if reason:
        add_intervention(
            sess,
            origin_view=body.get("origin_view", "scene"),
            origin_role_key=body.get("role_key"),
            target_roles=("planner", "reviewer"),
            target_scope="negotiation",
            text=reason,
        )
    sess.stop_flag.clear()
    lcr = last_complete_round(sess)
    sess.max_rounds = lcr + extra_rounds
    touch_status(sess, status="running", active_stage="planning", interrupt_reason=None)
    add_event(
        sess,
        "session.status_changed",
        session_event_payload(sess, status="running", message="继续协商。"),
        source="workflow",
    )
    threading.Thread(target=_run_negotiation_thread, args=(sess, lcr + 1), daemon=True).start()
    return None


def review_fix_session(sess):
    if sess.status != "review_fix":
        return "当前状态不可修复"
    sess.stop_flag.clear()
    threading.Thread(target=_run_review_fix_thread, args=(sess,), daemon=True).start()
    return None


def review_skip_session(sess):
    touch_status(sess, status="done", active_stage="done", interrupt_reason=None)
    add_event(
        sess,
        "session.status_changed",
        session_event_payload(sess, status="done", message="用户跳过修复，直接结束。"),
        source="workflow",
    )


def review_continue_session(sess, body):
    if sess.status != "review_max_rounds":
        return "当前状态不可继续审查"
    extra = max(1, min(int(body.get("extra_rounds", 3)), 20))
    sess.max_review_rounds += extra
    sess.stop_flag.clear()
    threading.Thread(target=_run_review_fix_thread, args=(sess,), daemon=True).start()
    return None


def set_session_view_mode(sess, view_mode):
    if view_mode not in VIEW_MODES:
        return f"未知视图模式: {view_mode}"
    sess.view_mode = view_mode
    sess.workflow_config["view_mode"] = view_mode
    touch_status(sess, active_stage=sess.active_stage)
    add_event(
        sess,
        "session.view_mode_changed",
        session_event_payload(sess, view_mode=view_mode, message=f"切换到 {view_mode} 视图。"),
        source="workflow",
    )
    return None


def resize_terminal_viewport(sess, body):
    role_key = str(body.get("role_key") or "").strip()
    if role_key not in ROLE_KEYS:
        return {"ok": False, "error": "缺少有效的 role_key"}
    try:
        width_px = int(body.get("width_px", 0))
        height_px = int(body.get("height_px", 0))
        cols = int(body.get("cols", 0))
        rows = int(body.get("rows", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "terminal viewport 参数必须是整数"}
    if width_px <= 0 or height_px <= 0 or cols <= 0 or rows <= 0:
        return {"ok": False, "error": "terminal viewport 参数必须大于 0"}
    lane, changed = set_lane_viewport(
        sess,
        role_key,
        width_px=width_px,
        height_px=height_px,
        cols=cols,
        rows=rows,
        source="terminal",
    )
    return {"ok": True, "changed": changed, "lane": lane}


def resume_session(sess):
    if sess.status not in {"paused", "interrupted"}:
        return "当前会话不可恢复"
    sess.stop_flag.clear()
    if sess.active_stage in {"planning", "reviewing", "awaiting_execution"}:
        threading.Thread(target=_run_negotiation_thread, args=(sess, max(1, last_complete_round(sess) + 1)), daemon=True).start()
        return None
    if sess.active_stage in {"executing", "validating"}:
        threading.Thread(target=_run_execution_thread, args=(sess,), daemon=True).start()
        return None
    if sess.active_stage == "repairing":
        threading.Thread(target=_run_review_fix_thread, args=(sess,), daemon=True).start()
        return None
    return "未知会话阶段"


def _handle_command(sess, command, args, body):
    add_intervention(
        sess,
        origin_view=body.get("origin_view", sess.view_mode),
        origin_role_key=body.get("role_key"),
        target_roles=(),
        target_scope="control",
        text=(body.get("text") or "").strip(),
        command=command,
        status="acknowledged",
    )
    if command == "pause":
        pause_session(sess)
        return {"ok": True, "kind": "command", "command": command, "stop_process": True}
    if command == "stop":
        abort_session(sess)
        return {"ok": True, "kind": "command", "command": command, "stop_process": True}
    if command == "exec":
        err = execute_session(sess)
        return {"ok": err is None, "command": command, "error": err}
    if command == "fix":
        err = review_fix_session(sess)
        return {"ok": err is None, "command": command, "error": err}
    if command == "skip":
        review_skip_session(sess)
        return {"ok": True, "kind": "command", "command": command}
    if command == "review-continue":
        err = review_continue_session(sess, {"extra_rounds": int(args[0]) if args else 3})
        return {"ok": err is None, "command": command, "error": err}
    if command == "continue":
        reason = " ".join(args).strip()
        err = continue_session(
            sess,
            {
                "extra_rounds": 3,
                "message": reason,
                "origin_view": body.get("origin_view", "terminal"),
                "role_key": body.get("role_key"),
            },
        )
        return {"ok": err is None, "command": command, "error": err}
    if command == "switch-role":
        return {"ok": True, "kind": "command", "command": command, "message": "前端负责焦点切换，后端无需处理。"}
    return {"ok": False, "error": f"未知命令: /{command}"}


def handle_input(sess, body):
    raw_text = (body.get("text") or "").strip()
    command = (body.get("command") or "").strip()
    args = body.get("args") or []
    if not command and raw_text.startswith("/"):
        parts = raw_text[1:].split()
        command = parts[0] if parts else ""
        args = parts[1:]
    if command:
        return _handle_command(sess, command, args, body)
    if not raw_text:
        return {"ok": False, "error": "输入不能为空"}
    if sess.status == "consensus":
        return {"ok": False, "error": "共识状态下不接受普通文本输入，请使用 /continue 并附带理由，或直接执行。"}
    if sess.status in {"paused", "interrupted"}:
        return {"ok": False, "error": "当前会话已暂停或中断，请先恢复后再输入。"}
    if sess.status in {"done", "aborted", "error"}:
        return {"ok": False, "error": "当前会话已经结束，不能再追加输入。"}
    target_roles = target_roles_for_stage(sess.active_stage)
    intervention = add_intervention(
        sess,
        origin_view=body.get("origin_view", sess.view_mode),
        origin_role_key=body.get("role_key"),
        target_roles=target_roles,
        target_scope=sess.active_stage,
        text=raw_text,
    )
    return {"ok": True, "kind": "intervention", "intervention_id": intervention["id"]}


def _shutdown_all_sessions():
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


import bridge.server as _server

_server._b._ns = globals()


def main():
    parser = argparse.ArgumentParser(description="Bridge v4")
    parser.add_argument("--port", type=int, default=8686)
    parser.add_argument("--project", type=str)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--db-path", type=str)
    parser.add_argument("--log-dir", type=str)
    parser.add_argument("--enable-fixture-tools", action="store_true")
    args = parser.parse_args()

    if args.log_dir:
        import bridge.session as _sess_mod
        _sess_mod.LOG_DIR = Path(args.log_dir)

    if args.enable_fixture_tools or os.environ.get("BRIDGE_ENABLE_FIXTURE_TOOLS") == "1":
        enable_fixture_tools()

    init_store(args.db_path or os.environ.get("BRIDGE_DB_PATH"))
    import bridge.session as _sess_mod
    _sess_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadedHTTPServer(("0.0.0.0", args.port), BridgeHandler)

    def _sigterm_handler(signum, frame):
        pgids = _shutdown_all_sessions()
        _ensure_dead(pgids)
        server.shutdown()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    def _orphan_watchdog():
        original_ppid = os.getppid()
        while True:
            time.sleep(2)
            if os.getppid() != original_ppid:
                pgids = _shutdown_all_sessions()
                _ensure_dead(pgids)
                os._exit(0)

    threading.Thread(target=_orphan_watchdog, daemon=True).start()

    project_note = f"?project={urllib.parse.quote(args.project)}" if args.project else ""
    print(
        f"""
╔═══════════════════════════════════════════════╗
║  Bridge v4                                    ║
║  http://localhost:{args.port}/{project_note:<28}║
║  四角色统一账本 · SSE 工作台                  ║
╚═══════════════════════════════════════════════╝
"""
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pgids = _shutdown_all_sessions()
        _ensure_dead(pgids)
        server.server_close()
        if _store is not None:
            _store.close()


if __name__ == "__main__":
    main()
