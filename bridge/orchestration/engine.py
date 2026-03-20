"""
Bridge 编排引擎
===============
协商/执行/审查循环的核心逻辑。

所有外部依赖通过 keyword-only 参数显式传入，engine 模块无可变全局状态。
bridge.py 保留同签名薄 wrapper，在调用时从自身命名空间查找依赖注入。

协议检测委托给 reviewer adapter 的 detect_approval() / detect_closure()，
不硬编码任何文本匹配规则。
"""

from datetime import datetime
from bridge.session import add_event, add_history_event


# ═════════════════════════════════════════════════════════════════
# 纯函数 — 无外部依赖
# ═════════════════════════════════════════════════════════════════

def last_complete_round(history):
    """返回 history 中最后一个完整轮次号（同时有 claude 和 codex 记录）。无则返回 0。"""
    has_claude = set()
    has_codex = set()
    for h in history:
        r = h.get("round", 0)
        if h["role"] == "claude":
            has_claude.add(r)
        elif h["role"] == "codex":
            has_codex.add(r)
    complete = has_claude & has_codex
    return max(complete) if complete else 0


# ═════════════════════════════════════════════════════════════════
# 编排函数 — 外部依赖通过 keyword-only 参数传入
# ═════════════════════════════════════════════════════════════════

def run_negotiation(sess, *, start_round=1,
                    call_claude, call_codex, reviewer,
                    build_claude_first_prompt, build_claude_revise_prompt,
                    build_codex_first_prompt, build_codex_review_prompt,
                    collect_user_injects):
    """主协商循环。

    交替调用 Planner 和 Reviewer adapter:
    1. Planner 出方案 / 修订
    2. Reviewer 审查
    3. 检测共识 (reviewer.detect_approval)
    4. 达到 max_rounds 或共识后停止

    续接失败时回退到 last_complete_round。
    """
    task = sess.task
    cwd = sess.project_path
    max_rounds = sess.max_rounds

    try:
        with sess.status_lock:
            sess.status = "running"
        add_event(sess, "status_change", {"status": "running", "msg": "协商开始"})

        for rnd in range(start_round, max_rounds + 1):
            if sess.stop_flag.is_set():
                with sess.status_lock:
                    sess.status = "idle"
                add_event(sess, "status_change", {"status": "stopped", "msg": "用户中止"})
                return

            sess.current_round = rnd
            add_event(sess, "round_start", {"round": rnd, "max": max_rounds})

            # ── A) Claude 出方案 / 修订 ─────────────────────
            add_event(sess, "agent_thinking", {"agent": "claude", "round": rnd})

            if rnd == 1 and start_round == 1:
                prompt_c = build_claude_first_prompt(task, cwd)
            else:
                last_codex = ""
                for h in reversed(sess.history):
                    if h["role"] == "codex":
                        last_codex = h["content"]
                        break
                user_injects = collect_user_injects(sess.history)
                prompt_c = build_claude_revise_prompt(last_codex, user_injects)

            plan = call_claude(
                prompt_c, cwd, sess,
                continue_session=sess.claude_has_session,
            )
            sess.claude_has_session = True

            entry_c = {
                "round": rnd, "role": "claude", "phase": "方案",
                "content": plan, "timestamp": datetime.now().isoformat(),
            }
            add_history_event(sess, sess.history, entry_c, "agent_response")

            if sess.stop_flag.is_set():
                return

            # ── B) Codex 审查 ───────────────────────────────
            add_event(sess, "agent_thinking", {"agent": "codex", "round": rnd})

            if rnd == 1 and start_round == 1:
                prompt_x = build_codex_first_prompt(task, plan)
            else:
                user_injects_x = collect_user_injects(sess.history)
                prompt_x = build_codex_review_prompt(plan, user_injects_x)

            review = call_codex(
                prompt_x, cwd, sess,
                resume_last=sess.codex_has_session,
            )
            sess.codex_has_session = True

            entry_x = {
                "round": rnd, "role": "codex", "phase": "审查",
                "content": review, "timestamp": datetime.now().isoformat(),
            }
            add_history_event(sess, sess.history, entry_x, "agent_response")

            # ── C) 共识? ───────────────────────────────────
            if reviewer.detect_approval(review):
                with sess.status_lock:
                    sess.consensus = True
                    sess.consensus_round = rnd
                    sess.status = "consensus"
                add_event(sess, "consensus_reached", {
                    "round": rnd,
                    "msg": f"Codex 在第 {rnd} 轮认可了方案，等待你确认执行或继续协商。",
                })
                return

        with sess.status_lock:
            sess.status = "max_rounds"
        add_event(sess, "max_rounds_reached", {
            "round": max_rounds,
            "msg": f"已完成 {max_rounds} 轮协商（未获 APPROVED），可选择执行当前方案或继续协商。",
        })

    except Exception as e:
        if start_round > 1:
            # ── 续接失败：彻底回退到最后完整轮次 ──
            lcr = last_complete_round(sess.history)

            # 1) 裁剪 history：移除不完整轮次条目，保留 user 注入
            sess.history = [
                h for h in sess.history
                if h["role"] == "user" or h.get("round", 0) <= lcr
            ]

            # 2) 归位 current_round 和 max_rounds
            sess.current_round = lcr
            sess.max_rounds = lcr

            # 3) 找到最后完整轮次的 Claude 方案，用于恢复前端
            restored_plan = ""
            for h in reversed(sess.history):
                if h["role"] == "claude" and h.get("round") == lcr:
                    restored_plan = h["content"]
                    break

            # 4) 设状态并通知前端
            with sess.status_lock:
                sess.status = "max_rounds"
            add_event(sess, "rollback", {
                "round": lcr,
                "max": lcr,
                "plan": restored_plan,
                "msg": f"继续协商出错（{e}），已回退到第 {lcr} 轮状态，仍可执行或再次续接。",
            })
        else:
            with sess.status_lock:
                sess.status = "error"
            sess.error = str(e)
            add_event(sess, "error", {"msg": str(e)})


def run_execution(sess, *,
                  call_claude, _is_git_repo, capture_baseline_ref,
                  capture_baseline_untracked, build_execution_prompt,
                  _run_first_review):
    """执行已审批的方案。

    1. 捕获 Git baseline (stash create + untracked)
    2. 调用 Planner adapter 执行 (--dangerously-skip-permissions)
    3. 自动触发 _run_first_review
    """
    try:
        add_event(sess, "status_change", {"status": "executing", "msg": "Claude 正在执行..."})

        # 记录执行前基线（tracked ref + untracked 文件快照）
        sess.is_git_repo = _is_git_repo(sess.project_path)
        if sess.is_git_repo:
            sess.exec_baseline_ref = capture_baseline_ref(sess.project_path)
            sess.exec_baseline_untracked = capture_baseline_untracked(sess.project_path)

        final_plan = ""
        for h in reversed(sess.history):
            if h["role"] == "claude":
                final_plan = h["content"]
                break

        prompt = build_execution_prompt(sess.task, final_plan, approved=sess.consensus)

        result = call_claude(
            prompt, sess.project_path, sess,
            continue_session=True,
            bypass_permissions=True,
            log_tag="claude_exec",
            skip_plan_detection=True,
        )

        sess.execution_result = result
        add_event(sess, "execution_done", {"result": result})

        # 自动触发第一轮 Codex 评审（只读，不修复）
        if sess.stop_flag.is_set():
            with sess.status_lock:
                sess.status = "done"
            return

        _run_first_review(sess, final_plan)

    except Exception as e:
        with sess.status_lock:
            sess.status = "error"
            sess.error = str(e)
        add_event(sess, "error", {"msg": str(e)})


def run_first_review(sess, approved_plan, *,
                     call_codex, reviewer,
                     build_codex_post_review_prompt):
    """执行完成后自动发起一轮 Codex 评审。只评审不修复。"""
    try:
        with sess.status_lock:
            sess.status = "review_pending"
        sess.review_round = 1
        add_event(sess, "status_change", {"status": "review_pending", "msg": "Codex 正在评审执行结果..."})
        add_event(sess, "review_start", {"round": 1, "max": sess.max_review_rounds})
        add_event(sess, "agent_thinking", {"agent": "codex", "round": 1})

        prompt = build_codex_post_review_prompt(sess, sess.task, approved_plan, sess.execution_result)

        review = call_codex(
            prompt, sess.project_path, sess,
            resume_last=sess.codex_has_session, log_tag="codex_review_1")
        sess.codex_has_session = True

        # stop guard
        if sess.stop_flag.is_set():
            return

        entry = {"round": 1, "role": "codex", "phase": "执行审查",
                 "content": review, "timestamp": datetime.now().isoformat()}
        add_history_event(sess, sess.review_history, entry, "review_response")

        if reviewer.detect_closure(review):
            with sess.status_lock:
                sess.status = "done"
            add_event(sess, "review_done", {"round": 1, "msg": "Codex 确认任务收口成功。", "success": True})
        else:
            with sess.status_lock:
                sess.status = "review_fix"
            add_event(sess, "review_needs_fix", {"round": 1, "msg": "Codex 发现问题，等待你确认是否修复。", "review": review})

    except Exception as e:
        with sess.status_lock:
            sess.status = "error"
            sess.error = str(e)
        add_event(sess, "error", {"msg": f"评审阶段出错: {e}"})


def run_review_fix_cycle(sess, *,
                         call_claude, call_codex, reviewer,
                         build_claude_post_fix_prompt,
                         build_codex_post_review_followup_prompt):
    """用户确认后：Claude 修复 → Codex 再评审。单轮。"""
    try:
        rr = sess.review_round + 1
        if rr > sess.max_review_rounds:
            with sess.status_lock:
                sess.status = "done"
            add_event(sess, "review_done", {"round": rr - 1, "msg": f"已达最大审查轮次 ({sess.max_review_rounds})。", "success": False})
            return

        sess.review_round = rr
        with sess.status_lock:
            sess.status = "review_pending"
        add_event(sess, "status_change", {"status": "review_pending", "msg": f"审查修复轮 {rr}..."})
        add_event(sess, "review_round_start", {"round": rr, "max": sess.max_review_rounds})

        if sess.stop_flag.is_set():
            return

        # A) Claude 修复
        add_event(sess, "agent_thinking", {"agent": "claude", "round": rr})
        last_review = sess.review_history[-1]["content"] if sess.review_history else ""
        fix_result = call_claude(
            build_claude_post_fix_prompt(last_review), sess.project_path, sess,
            continue_session=True, bypass_permissions=True,
            log_tag=f"claude_fix_{rr}", skip_plan_detection=True)

        # stop guard
        if sess.stop_flag.is_set():
            return

        fix_entry = {"round": rr, "role": "claude", "phase": "修复",
                     "content": fix_result, "timestamp": datetime.now().isoformat()}
        add_history_event(sess, sess.review_history, fix_entry, "review_response")
        sess.execution_result = fix_result

        if sess.stop_flag.is_set():
            return

        # B) Codex 再评审
        add_event(sess, "agent_thinking", {"agent": "codex", "round": rr})
        review = call_codex(
            build_codex_post_review_followup_prompt(sess, fix_result),
            sess.project_path, sess,
            resume_last=sess.codex_has_session, log_tag=f"codex_review_{rr}")
        sess.codex_has_session = True

        # stop guard
        if sess.stop_flag.is_set():
            return

        review_entry = {"round": rr, "role": "codex", "phase": "执行审查",
                        "content": review, "timestamp": datetime.now().isoformat()}
        add_history_event(sess, sess.review_history, review_entry, "review_response")

        if reviewer.detect_closure(review):
            with sess.status_lock:
                sess.status = "done"
            add_event(sess, "review_done", {"round": rr, "msg": f"Codex 在第 {rr} 轮确认任务收口成功。", "success": True})
        else:
            with sess.status_lock:
                sess.status = "review_fix"
            add_event(sess, "review_needs_fix", {"round": rr, "msg": "Codex 仍发现问题，等待你确认是否继续修复。", "review": review})

    except Exception as e:
        with sess.status_lock:
            sess.status = "error"
            sess.error = str(e)
        add_event(sess, "error", {"msg": f"修复阶段出错: {e}"})
