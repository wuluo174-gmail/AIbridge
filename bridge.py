#!/usr/bin/env python3
"""
Claude ↔ Codex 协商桥梁 v3 — 薄兼容 facade
=============================================
所有实现已迁入 bridge/ 子模块。
本文件保留:
  1. 模块级 re-exports（测试 self.mod.X 兼容）
  2. Plan 文件并发锁（测试直接 patch PLAN_LOCK_ACQUIRE_TIMEOUT）
  3. Adapter 单例 + CLI wrappers
  4. 编排 wrappers（LOAD_GLOBAL 解析依赖，支持测试 monkey-patch）
  5. Prompt DI wrappers（Universal DI Rule: 注入可 patch 的 helper）
  6. _BridgeProxy 接线 + main()

用法:
  python3 bridge.py                            # Web UI 模式 (默认)
  python3 bridge.py --port 9090                # 自定义端口
"""

import argparse
import threading
import urllib.parse

# ── re-exports: bridge.protocol ──
from bridge.protocol import (
    EXECUTABLE_STATES, FIXABLE_STATES, CONTINUABLE_STATES,
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

# ── re-exports: bridge.orchestration.prompts (直接 re-export 的安全函数) ──
from bridge.orchestration.prompts import (
    prompt_config, load_prompts, save_prompts,
    detect_claude_md, collect_user_injects,
    build_claude_revise_prompt, build_codex_first_prompt,
    build_codex_review_prompt, build_execution_prompt,
    build_claude_post_fix_prompt,
)

# ── re-exports: bridge.server ──
from bridge.server import (
    ThreadedHTTPServer, BridgeHandler,
    load_recent_paths, save_recent_paths,
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
# Adapter 单例 + CLI Wrappers
# ═════════════════════════════════════════════════════════════════
from bridge.adapters.claude_adapter import ClaudeCodeAdapter    # noqa: E402
from bridge.adapters.codex_adapter import CodexAdapter          # noqa: E402

_claude_adapter = ClaudeCodeAdapter(
    plan_lock_acquire_fn=_acquire_plan_file_lock,
)
_codex_adapter = CodexAdapter()


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
# Prompt DI Wrappers (Universal DI Rule)
# ═════════════════════════════════════════════════════════════════
from bridge.orchestration.prompts import (                      # noqa: E402
    build_claude_first_prompt as _prompts_build_claude_first_prompt,
    build_codex_post_review_prompt as _prompts_build_codex_post_review_prompt,
    build_codex_post_review_followup_prompt as _prompts_build_codex_post_review_followup_prompt,
)


def build_claude_first_prompt(task, cwd):
    return _prompts_build_claude_first_prompt(
        task, cwd,
        _detect_claude_md=detect_claude_md)


def build_codex_post_review_prompt(sess, task, approved_plan, execution_result):
    return _prompts_build_codex_post_review_prompt(
        sess, task, approved_plan, execution_result,
        _capture_diff=capture_execution_diff)


def build_codex_post_review_followup_prompt(sess, fix_result):
    return _prompts_build_codex_post_review_followup_prompt(
        sess, fix_result,
        _capture_diff=capture_execution_diff)


# ═════════════════════════════════════════════════════════════════
# Orchestration Wrappers — LOAD_GLOBAL 从本模块 namespace 解析依赖
# ═════════════════════════════════════════════════════════════════
last_complete_round = _engine.last_complete_round


def run_first_review(sess, approved_plan):
    _engine.run_first_review(
        sess, approved_plan,
        call_codex=call_codex_streaming,
        reviewer=_codex_adapter,
        build_codex_post_review_prompt=build_codex_post_review_prompt,
    )


def run_negotiation(sess, start_round=1):
    _engine.run_negotiation(
        sess, start_round=start_round,
        call_claude=call_claude_streaming,
        call_codex=call_codex_streaming,
        reviewer=_codex_adapter,
        build_claude_first_prompt=build_claude_first_prompt,
        build_claude_revise_prompt=build_claude_revise_prompt,
        build_codex_first_prompt=build_codex_first_prompt,
        build_codex_review_prompt=build_codex_review_prompt,
        collect_user_injects=collect_user_injects,
    )


def run_execution(sess):
    _engine.run_execution(
        sess,
        call_claude=call_claude_streaming,
        _is_git_repo=_is_git_repo,
        capture_baseline_ref=capture_baseline_ref,
        capture_baseline_untracked=capture_baseline_untracked,
        build_execution_prompt=build_execution_prompt,
        _run_first_review=run_first_review,
    )


def run_review_fix_cycle(sess):
    _engine.run_review_fix_cycle(
        sess,
        call_claude=call_claude_streaming,
        call_codex=call_codex_streaming,
        reviewer=_codex_adapter,
        build_claude_post_fix_prompt=build_claude_post_fix_prompt,
        build_codex_post_review_followup_prompt=build_codex_post_review_followup_prompt,
    )


# ═════════════════════════════════════════════════════════════════
# _BridgeProxy 接线 — 将本模块 globals() 注入 server.py
# ═════════════════════════════════════════════════════════════════
import bridge.server as _server                                 # noqa: E402
_server._b._ns = globals()


# ═════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Claude ↔ Codex 协商桥梁")
    parser.add_argument("--port", type=int, default=8686)
    parser.add_argument("--project", type=str, help="预设项目路径")
    args = parser.parse_args()

    project_note = ""
    if args.project:
        project_note = f"?project={urllib.parse.quote(args.project)}"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadedHTTPServer(("0.0.0.0", args.port), BridgeHandler)

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
        print("\n已退出。")
        server.shutdown()


if __name__ == "__main__":
    main()
