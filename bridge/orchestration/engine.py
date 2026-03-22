"""
Bridge workflow engine
======================
固定四角色模板：planner / reviewer / executor / validator
"""

from __future__ import annotations

from datetime import datetime

from bridge.protocol import is_approved, is_closure
from bridge.session import (
    add_event,
    consume_interventions,
    lane_event_payload,
    last_complete_round,
    latest_artifact,
    publish_artifact,
    session_event_payload,
    touch_status,
)


def _set_status(sess, status, stage, *, msg, error=None, interrupt_reason=None):
    touch_status(
        sess,
        status=status,
        active_stage=stage,
        error=error,
        interrupt_reason=interrupt_reason,
    )
    add_event(
        sess,
        "session.status_changed",
        session_event_payload(sess, status=status, message=msg),
        source="workflow",
    )
    add_event(
        sess,
        "session.stage_changed",
        session_event_payload(sess, active_stage=stage, message=msg),
        source="workflow",
    )


def _preserve_or_mark_interrupted(sess, stage):
    with sess.status_lock:
        current = sess.status
    if current in {"paused", "aborted"}:
        return
    _set_status(sess, "interrupted", stage, msg="工作流已中断。", interrupt_reason="unexpected_stop")


def _append_intervention_section(prompt, texts):
    if not texts:
        return prompt
    lines = "\n".join(f"- {text}" for text in texts)
    return f"{prompt}\n\n## 用户新增约束\n{lines}"


def _run_role(sess, role_key, prompt, call_role, *, round_no, log_tag, bypass_permissions=False):
    sess.roles[role_key].lane_status = "busy"
    add_event(
        sess,
        "lane.status_changed",
        lane_event_payload(sess, role_key, status="busy", message=f"{role_key} 通道忙碌中"),
        role_key=role_key,
        source="workflow",
    )
    add_event(
        sess,
        "lane.thinking_started",
        lane_event_payload(sess, role_key, round=round_no, message=f"{role_key} 正在处理"),
        role_key=role_key,
        source="workflow",
    )
    try:
        return call_role(
            role_key,
            prompt,
            sess.project_path,
            sess,
            log_tag=log_tag,
            bypass_permissions=bypass_permissions,
        )
    finally:
        sess.roles[role_key].lane_status = "idle"
        add_event(
            sess,
            "lane.status_changed",
            lane_event_payload(sess, role_key, status="idle", message=f"{role_key} 通道空闲"),
            role_key=role_key,
            source="workflow",
        )


def run_negotiation(
    sess,
    *,
    start_round,
    call_role,
    role_adapters,
    build_planner_first_prompt,
    build_planner_revise_prompt,
    build_reviewer_first_prompt,
    build_reviewer_review_prompt,
):
    try:
        _set_status(sess, "running", "planning", msg="协商开始。")

        for rnd in range(start_round, sess.max_rounds + 1):
            if sess.stop_flag.is_set():
                _preserve_or_mark_interrupted(sess, sess.active_stage)
                return

            sess.current_round = rnd
            _set_status(sess, "running", "planning", msg=f"第 {rnd} 轮：规划中。")
            planner_inputs = consume_interventions(sess, "planner", rnd)
            if rnd == 1 and start_round == 1:
                prompt = build_planner_first_prompt(sess.task, sess.project_path)
            else:
                last_review = latest_artifact(sess, "review")
                prompt = build_planner_revise_prompt(
                    last_review["content"] if last_review else "",
                    planner_inputs,
                    sess.project_path,
                )
                planner_inputs = []
            prompt = _append_intervention_section(prompt, planner_inputs)
            plan = _run_role(
                sess,
                "planner",
                prompt,
                call_role,
                round_no=rnd,
                log_tag=f"planner_{rnd}",
            )
            if sess.stop_flag.is_set():
                _preserve_or_mark_interrupted(sess, "planning")
                return

            publish_artifact(
                sess,
                role_key="planner",
                round_no=rnd,
                phase="planning",
                artifact_kind="plan",
                content=plan,
            )

            _set_status(sess, "running", "reviewing", msg=f"第 {rnd} 轮：审查中。")
            reviewer_inputs = consume_interventions(sess, "reviewer", rnd)
            if rnd == 1 and start_round == 1:
                review_prompt = build_reviewer_first_prompt(sess.task, plan)
            else:
                review_prompt = build_reviewer_review_prompt(plan, reviewer_inputs)
                reviewer_inputs = []
            review_prompt = _append_intervention_section(review_prompt, reviewer_inputs)
            review = _run_role(
                sess,
                "reviewer",
                review_prompt,
                call_role,
                round_no=rnd,
                log_tag=f"reviewer_{rnd}",
            )
            if sess.stop_flag.is_set():
                _preserve_or_mark_interrupted(sess, "reviewing")
                return

            publish_artifact(
                sess,
                role_key="reviewer",
                round_no=rnd,
                phase="reviewing",
                artifact_kind="review",
                content=review,
            )

            if role_adapters["reviewer"].detect_approval(review):
                sess.consensus_round = rnd
                publish_artifact(
                    sess,
                    role_key="reviewer",
                    round_no=rnd,
                    phase="awaiting_execution",
                    artifact_kind="consensus_snapshot",
                    content=plan,
                )
                _set_status(sess, "consensus", "awaiting_execution", msg=f"第 {rnd} 轮达成共识。")
                return

        _set_status(sess, "max_rounds", "awaiting_execution", msg="达到最大协商轮次。")
    except Exception as exc:
        _set_status(sess, "error", sess.active_stage, msg=str(exc), error=str(exc))
        add_event(sess, "error.raised", {"message": str(exc)}, source="workflow")


def _run_validation(
    sess,
    *,
    call_role,
    role_adapters,
    approved_plan,
    build_post_review_prompt,
    round_no,
):
    _set_status(sess, "validating", "validating", msg=f"第 {round_no} 轮：校验中。")
    execution_artifact = latest_artifact(sess, "execution_summary", "executor")
    prompt = build_post_review_prompt(
        sess,
        sess.task,
        approved_plan,
        execution_artifact["content"] if execution_artifact else "",
    )
    validator_inputs = consume_interventions(sess, "validator", round_no)
    prompt = _append_intervention_section(prompt, validator_inputs)
    review = _run_role(
        sess,
        "validator",
        prompt,
        call_role,
        round_no=round_no,
        log_tag=f"validator_{round_no}",
    )
    publish_artifact(
        sess,
        role_key="validator",
        round_no=round_no,
        phase="validating",
        artifact_kind="validation_report",
        content=review,
    )
    if role_adapters["validator"].detect_closure(review):
        _set_status(sess, "done", "done", msg="任务收口成功。")
        return True
    _set_status(sess, "review_fix", "repairing", msg="校验未通过，等待修复。")
    return False


def run_execution(
    sess,
    *,
    call_role,
    role_adapters,
    _is_git_repo,
    capture_baseline_ref,
    capture_baseline_untracked,
    build_execution_prompt,
    build_post_review_prompt,
):
    try:
        _set_status(sess, "executing", "executing", msg="执行阶段开始。")
        sess.is_git_repo = _is_git_repo(sess.project_path)
        if sess.is_git_repo:
            sess.exec_baseline_ref = capture_baseline_ref(sess.project_path)
            sess.exec_baseline_untracked = capture_baseline_untracked(sess.project_path)

        final_plan = latest_artifact(sess, "plan", "planner")
        prompt = build_execution_prompt(
            sess.task,
            final_plan["content"] if final_plan else "",
            approved=bool(sess.consensus_round),
        )
        executor_inputs = consume_interventions(sess, "executor", max(sess.current_review_round, 1))
        prompt = _append_intervention_section(prompt, executor_inputs)
        result = _run_role(
            sess,
            "executor",
            prompt,
            call_role,
            round_no=max(sess.current_round, 1),
            log_tag=f"executor_{max(sess.current_round, 1)}",
            bypass_permissions=True,
        )
        if sess.stop_flag.is_set():
            _preserve_or_mark_interrupted(sess, "executing")
            return
        publish_artifact(
            sess,
            role_key="executor",
            round_no=max(sess.current_round, 1),
            phase="executing",
            artifact_kind="execution_summary",
            content=result,
        )
        sess.current_review_round = max(sess.current_review_round, 1)
        _run_validation(
            sess,
            call_role=call_role,
            role_adapters=role_adapters,
            approved_plan=final_plan["content"] if final_plan else "",
            build_post_review_prompt=build_post_review_prompt,
            round_no=sess.current_review_round,
        )
    except Exception as exc:
        _set_status(sess, "error", sess.active_stage, msg=str(exc), error=str(exc))
        add_event(sess, "error.raised", {"message": str(exc)}, source="workflow")


def run_review_fix_cycle(
    sess,
    *,
    call_role,
    role_adapters,
    build_fix_prompt,
    build_followup_prompt,
):
    try:
        next_round = sess.current_review_round + 1
        if next_round > sess.max_review_rounds:
            _set_status(sess, "review_max_rounds", "repairing", msg="达到最大修复轮次。")
            return

        sess.current_review_round = next_round
        _set_status(sess, "repairing", "repairing", msg=f"第 {next_round} 轮修复开始。")
        latest_validation = latest_artifact(sess, "validation_report", "validator")
        fix_prompt = build_fix_prompt(latest_validation["content"] if latest_validation else "")
        executor_inputs = consume_interventions(sess, "executor", next_round)
        fix_prompt = _append_intervention_section(fix_prompt, executor_inputs)
        fix_result = _run_role(
            sess,
            "executor",
            fix_prompt,
            call_role,
            round_no=next_round,
            log_tag=f"executor_fix_{next_round}",
            bypass_permissions=True,
        )
        if sess.stop_flag.is_set():
            _preserve_or_mark_interrupted(sess, "repairing")
            return
        publish_artifact(
            sess,
            role_key="executor",
            round_no=next_round,
            phase="repairing",
            artifact_kind="execution_summary",
            content=fix_result,
        )

        validator_prompt = build_followup_prompt(sess, fix_result)
        validator_inputs = consume_interventions(sess, "validator", next_round)
        validator_prompt = _append_intervention_section(validator_prompt, validator_inputs)
        review = _run_role(
            sess,
            "validator",
            validator_prompt,
            call_role,
            round_no=next_round,
            log_tag=f"validator_{next_round}",
        )
        publish_artifact(
            sess,
            role_key="validator",
            round_no=next_round,
            phase="validating",
            artifact_kind="validation_report",
            content=review,
        )
        if role_adapters["validator"].detect_closure(review):
            _set_status(sess, "done", "done", msg="任务收口成功。")
        elif next_round >= sess.max_review_rounds:
            _set_status(sess, "review_max_rounds", "repairing", msg="达到最大修复轮次。")
        else:
            _set_status(sess, "review_fix", "repairing", msg="校验仍未通过，等待继续修复。")
    except Exception as exc:
        _set_status(sess, "error", sess.active_stage, msg=str(exc), error=str(exc))
        add_event(sess, "error.raised", {"message": str(exc)}, source="workflow")


__all__ = [
    "is_approved",
    "is_closure",
    "last_complete_round",
    "run_execution",
    "run_negotiation",
    "run_review_fix_cycle",
]
