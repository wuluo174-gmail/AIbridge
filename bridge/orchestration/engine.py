"""
Bridge 编排引擎
===============
协商/执行/审查循环的核心逻辑。

所有外部依赖通过 keyword-only 参数显式传入，engine 模块无可变全局状态。
bridge.py 保留同签名薄 wrapper，在调用时从自身命名空间查找依赖注入。

Step 7: 参数从 call_claude/call_codex 重命名为 call_planner/call_reviewer/call_executor。
        事件 agent 字段使用 "planner"/"reviewer"，history role 同。
        执行阶段与协商角色解耦（executor 按能力由 bridge.py 注入）。
        Session 状态追踪由 caller 闭包内部管理，engine 不再直接读写 sess.xxx_has_session。
"""

from datetime import datetime
from bridge.session import add_event, add_history_event


# ═════════════════════════════════════════════════════════════════
# 纯函数 — 无外部依赖
# ═════════════════════════════════════════════════════════════════

def last_complete_round(history):
    """返回 history 中最后一个完整轮次号（同时有 planner 和 reviewer 记录）。无则返回 0。"""
    has_planner = set()
    has_reviewer = set()
    for h in history:
        r = h.get("round", 0)
        if h["role"] == "planner":
            has_planner.add(r)
        elif h["role"] == "reviewer":
            has_reviewer.add(r)
    complete = has_planner & has_reviewer
    return max(complete) if complete else 0


# ═════════════════════════════════════════════════════════════════
# 编排函数 — 外部依赖通过 keyword-only 参数传入
# ═════════════════════════════════════════════════════════════════

def run_negotiation(sess, *, start_round=1,
                    call_planner, call_reviewer, reviewer_adapter,
                    build_planner_first_prompt, build_planner_revise_prompt,
                    build_reviewer_first_prompt, build_reviewer_review_prompt,
                    collect_user_injects):
    """主协商循环。

    交替调用 Planner 和 Reviewer adapter:
    1. Planner 出方案 / 修订
    2. Reviewer 审查
    3. 检测共识 (reviewer_adapter.detect_approval)
    4. 达到 max_rounds 或共识后停止

    续接失败时回退到 last_complete_round。
    Session 状态 (has_session) 由 caller 闭包内部管理，engine 不直接读写。
    """
    task = sess.task
    cwd = sess.project_path
    max_rounds = sess.max_rounds

    try:
        with sess.status_lock:
            sess.status = "running"
        add_event(sess, "status_change", {"status": "running", "msg": "协商开始", "msg_key": "be.negotiation_start"})

        for rnd in range(start_round, max_rounds + 1):
            if sess.stop_flag.is_set():
                with sess.status_lock:
                    sess.status = "idle"
                add_event(sess, "status_change", {"status": "stopped", "msg": "用户中止", "msg_key": "be.user_stopped"})
                return

            sess.current_round = rnd
            add_event(sess, "round_start", {"round": rnd, "max": max_rounds})

            # ── A) Planner 出方案 / 修订 ─────────────────────
            add_event(sess, "agent_thinking", {"agent": "planner", "round": rnd})

            if rnd == 1 and start_round == 1:
                prompt_c = build_planner_first_prompt(task, cwd)
            else:
                last_review = ""
                for h in reversed(sess.history):
                    if h["role"] == "reviewer":
                        last_review = h["content"]
                        break
                user_injects = collect_user_injects(sess.history)
                prompt_c = build_planner_revise_prompt(last_review, user_injects)

            plan = call_planner(prompt_c, cwd, sess)

            entry_c = {
                "round": rnd, "role": "planner", "phase": "方案",
                "content": plan, "timestamp": datetime.now().isoformat(),
            }
            add_history_event(sess, sess.history, entry_c, "agent_response")

            if sess.stop_flag.is_set():
                return

            # ── B) Reviewer 审查 ───────────────────────────────
            add_event(sess, "agent_thinking", {"agent": "reviewer", "round": rnd})

            if rnd == 1 and start_round == 1:
                prompt_x = build_reviewer_first_prompt(task, plan)
            else:
                user_injects_x = collect_user_injects(sess.history)
                prompt_x = build_reviewer_review_prompt(plan, user_injects_x)

            review = call_reviewer(prompt_x, cwd, sess)

            entry_x = {
                "round": rnd, "role": "reviewer", "phase": "审查",
                "content": review, "timestamp": datetime.now().isoformat(),
            }
            add_history_event(sess, sess.history, entry_x, "agent_response")

            # ── C) 共识? ───────────────────────────────────
            if reviewer_adapter.detect_approval(review):
                with sess.status_lock:
                    sess.consensus = True
                    sess.consensus_round = rnd
                    sess.status = "consensus"
                add_event(sess, "consensus_reached", {
                    "round": rnd,
                    "msg": f"Reviewer 在第 {rnd} 轮认可了方案，等待你确认执行或继续协商。",
                    "msg_key": "be.consensus", "msg_params": {"round": rnd},
                })
                return

        with sess.status_lock:
            sess.status = "max_rounds"
        add_event(sess, "max_rounds_reached", {
            "round": max_rounds,
            "msg": f"已完成 {max_rounds} 轮协商（未获 APPROVED），可选择执行当前方案或继续协商。",
            "msg_key": "be.max_rounds", "msg_params": {"rounds": max_rounds},
        })

    except Exception as e:
        if start_round > 1:
            lcr = last_complete_round(sess.history)

            sess.history = [
                h for h in sess.history
                if h["role"] == "user" or h.get("round", 0) <= lcr
            ]

            sess.current_round = lcr
            sess.max_rounds = lcr

            restored_plan = ""
            for h in reversed(sess.history):
                if h["role"] == "planner" and h.get("round") == lcr:
                    restored_plan = h["content"]
                    break

            with sess.status_lock:
                sess.status = "max_rounds"
            add_event(sess, "rollback", {
                "round": lcr,
                "max": lcr,
                "plan": restored_plan,
                "msg": f"继续协商出错（{e}），已回退到第 {lcr} 轮状态，仍可执行或再次续接。",
                "msg_key": "be.rollback", "msg_params": {"detail": str(e), "round": lcr},
            })
        else:
            with sess.status_lock:
                sess.status = "error"
            sess.error = str(e)
            add_event(sess, "error", {"msg": str(e), "msg_key": "be.error", "msg_params": {"detail": str(e)}})


def run_execution(sess, *,
                  call_executor, executor_panel,
                  _is_git_repo, capture_baseline_ref,
                  capture_baseline_untracked, build_execution_prompt,
                  _run_first_review):
    """执行已审批的方案。

    1. 捕获 Git baseline (stash create + untracked)
    2. 调用 executor adapter 执行 (bypass_permissions=True)
    3. 自动触发 _run_first_review
    """
    try:
        add_event(sess, "status_change", {"status": "executing", "msg": "正在执行...", "msg_key": "be.executing"})

        sess.is_git_repo = _is_git_repo(sess.project_path)
        if sess.is_git_repo:
            sess.exec_baseline_ref = capture_baseline_ref(sess.project_path)
            sess.exec_baseline_untracked = capture_baseline_untracked(sess.project_path)

        final_plan = ""
        for h in reversed(sess.history):
            if h["role"] == "planner":
                final_plan = h["content"]
                break

        prompt = build_execution_prompt(sess.task, final_plan, approved=sess.consensus)

        result = call_executor(
            prompt, sess.project_path, sess,
            bypass_permissions=True,
            log_tag="executor",
            skip_plan_detection=True,
        )

        sess.execution_result = result
        add_event(sess, "execution_done", {"result": result, "executor_panel": executor_panel})

        if sess.stop_flag.is_set():
            with sess.status_lock:
                sess.status = "done"
            return

        _run_first_review(sess, final_plan)

    except Exception as e:
        with sess.status_lock:
            sess.status = "error"
            sess.error = str(e)
        add_event(sess, "error", {"msg": str(e), "msg_key": "be.error_exec", "msg_params": {"detail": str(e)}})


def run_first_review(sess, approved_plan, *,
                     call_exec_reviewer, exec_reviewer_panel,
                     exec_reviewer_adapter,
                     build_post_review_prompt):
    """执行完成后自动发起一轮评审。只评审不修复。"""
    try:
        with sess.status_lock:
            sess.status = "review_pending"
        sess.review_round = 1
        add_event(sess, "status_change", {"status": "review_pending", "msg": "正在评审执行结果...", "msg_key": "be.reviewing"})
        add_event(sess, "review_start", {"round": 1, "max": sess.max_review_rounds})
        add_event(sess, "agent_thinking", {"agent": exec_reviewer_panel, "round": 1})

        prompt = build_post_review_prompt(sess, sess.task, approved_plan, sess.execution_result)

        review = call_exec_reviewer(
            prompt, sess.project_path, sess,
            log_tag="exec_reviewer_1")

        if sess.stop_flag.is_set():
            return

        entry = {"round": 1, "role": exec_reviewer_panel, "phase": "执行审查",
                 "content": review, "timestamp": datetime.now().isoformat()}
        add_history_event(sess, sess.review_history, entry, "review_response")

        if exec_reviewer_adapter.detect_closure(review):
            with sess.status_lock:
                sess.status = "done"
            add_event(sess, "review_done", {"round": 1, "msg": "执行审查确认任务收口成功。", "success": True, "msg_key": "be.closure_ok"})
        else:
            with sess.status_lock:
                sess.status = "review_fix"
            add_event(sess, "review_needs_fix", {"round": 1, "msg": "执行审查发现问题，等待你确认是否修复。", "review": review, "msg_key": "be.needs_fix"})

    except Exception as e:
        with sess.status_lock:
            sess.status = "error"
            sess.error = str(e)
        add_event(sess, "error", {"msg": f"评审阶段出错: {e}", "msg_key": "be.error_review", "msg_params": {"detail": str(e)}})


def run_review_fix_cycle(sess, *,
                         call_executor, executor_panel,
                         call_exec_reviewer, exec_reviewer_panel,
                         exec_reviewer_adapter,
                         build_fix_prompt,
                         build_followup_prompt):
    """用户确认后：executor 修复 → exec_reviewer 再评审。单轮。"""
    try:
        rr = sess.review_round + 1
        if rr > sess.max_review_rounds:
            with sess.status_lock:
                sess.status = "review_max_rounds"
            add_event(sess, "review_max_rounds_reached", {
                "round": rr - 1,
                "msg": f"已完成 {sess.max_review_rounds} 轮审查修复（问题仍存），可选择继续审查或跳过。",
                "msg_key": "be.review_max", "msg_params": {"rounds": sess.max_review_rounds},
            })
            return

        sess.review_round = rr
        with sess.status_lock:
            sess.status = "review_pending"
        add_event(sess, "status_change", {"status": "review_pending", "msg": f"审查修复轮 {rr}...", "msg_key": "be.review_fix_round", "msg_params": {"round": rr}})
        add_event(sess, "review_round_start", {"round": rr, "max": sess.max_review_rounds})

        if sess.stop_flag.is_set():
            return

        # A) Executor 修复
        add_event(sess, "agent_thinking", {"agent": executor_panel, "round": rr})
        last_review = sess.review_history[-1]["content"] if sess.review_history else ""
        fix_result = call_executor(
            build_fix_prompt(last_review), sess.project_path, sess,
            bypass_permissions=True,
            log_tag=f"executor_fix_{rr}", skip_plan_detection=True)

        if sess.stop_flag.is_set():
            return

        fix_entry = {"round": rr, "role": executor_panel, "phase": "修复",
                     "content": fix_result, "timestamp": datetime.now().isoformat()}
        add_history_event(sess, sess.review_history, fix_entry, "review_response")
        sess.execution_result = fix_result

        if sess.stop_flag.is_set():
            return

        # B) Exec reviewer 再评审
        add_event(sess, "agent_thinking", {"agent": exec_reviewer_panel, "round": rr})
        review = call_exec_reviewer(
            build_followup_prompt(sess, fix_result),
            sess.project_path, sess,
            log_tag=f"exec_reviewer_{rr}")

        if sess.stop_flag.is_set():
            return

        review_entry = {"round": rr, "role": exec_reviewer_panel, "phase": "执行审查",
                        "content": review, "timestamp": datetime.now().isoformat()}
        add_history_event(sess, sess.review_history, review_entry, "review_response")

        if exec_reviewer_adapter.detect_closure(review):
            with sess.status_lock:
                sess.status = "done"
            add_event(sess, "review_done", {"round": rr, "msg": f"第 {rr} 轮确认任务收口成功。", "success": True, "msg_key": "be.closure_round_ok", "msg_params": {"round": rr}})
        else:
            with sess.status_lock:
                sess.status = "review_fix"
            add_event(sess, "review_needs_fix", {"round": rr, "msg": "仍发现问题，等待你确认是否继续修复。", "review": review, "msg_key": "be.still_needs_fix"})

    except Exception as e:
        with sess.status_lock:
            sess.status = "error"
            sess.error = str(e)
        add_event(sess, "error", {"msg": f"修复阶段出错: {e}", "msg_key": "be.error_fix", "msg_params": {"detail": str(e)}})
