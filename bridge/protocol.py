"""
Bridge protocol constants
=========================
四角色统一账本与双模式工作台的权威协议定义。
"""

from __future__ import annotations

import re

from bridge.workflow import ROLE_KEYS, VIEW_MODES


SESSION_STATUSES = frozenset({
    "idle",
    "running",
    "consensus",
    "max_rounds",
    "executing",
    "validating",
    "review_fix",
    "review_max_rounds",
    "repairing",
    "paused",
    "interrupted",
    "aborted",
    "done",
    "error",
})

ACTIVE_STAGES = frozenset({
    "planning",
    "reviewing",
    "awaiting_execution",
    "executing",
    "validating",
    "repairing",
    "done",
})

INTERVENTION_STATUSES = frozenset({
    "queued",
    "acknowledged",
    "consumed",
    "cancelled",
    "rejected",
})

STREAM_EVENT_TYPES = frozenset({
    "session.status_changed",
    "session.stage_changed",
    "session.view_mode_changed",
    "lane.status_changed",
    "lane.viewport_changed",
    "lane.thinking_started",
    "lane.cli_started",
    "lane.stdout_chunk",
    "lane.stderr_chunk",
    "lane.command_started",
    "lane.command_output",
    "lane.result_emitted",
    "artifact.published",
    "intervention.received",
    "intervention.consumed",
    "warning.raised",
    "error.raised",
})

ARTIFACT_KINDS = frozenset({
    "plan",
    "review",
    "execution_summary",
    "validation_report",
    "consensus_snapshot",
})


def is_approved(text: str) -> bool:
    if not text:
        return False
    first_line = text.strip().split("\n")[0]
    return bool(re.match(r"\s*APPROVED\b", first_line, re.IGNORECASE))


def is_closure(text: str) -> bool:
    if not text:
        return False
    first_line = text.strip().split("\n")[0]
    return bool(re.match(r"\s*任务收口成功(?:\s|[，。：:,.!！]|$)", first_line))


GET_ENDPOINTS = (
    "/",
    "/api/tools",
    "/api/workflow_config",
    "/api/session/state",
    "/api/history",
    "/api/sessions",
    "/api/stream",
    "/api/browse",
    "/api/complete",
    "/api/recent_paths",
    "/api/prompts",
)

POST_ENDPOINTS = (
    "/api/workflow_config",
    "/api/session/start",
    "/api/session/pause",
    "/api/session/resume",
    "/api/session/stop",
    "/api/session/exec",
    "/api/session/continue",
    "/api/session/review_fix",
    "/api/session/review_skip",
    "/api/session/review_continue",
    "/api/session/view_mode",
    "/api/input",
    "/api/terminal/resize",
    "/api/prompts",
)


SESSION_STATE_KEYS = frozenset({
    "session_id",
    "task",
    "project_path",
    "workflow_template",
    "view_mode",
    "status",
    "active_stage",
    "current_round",
    "current_review_round",
    "consensus_round",
    "max_rounds",
    "max_review_rounds",
    "error",
    "interrupt_reason",
    "created_at",
    "updated_at",
    "finished_at",
    "resume_available",
})

HISTORY_KEYS = frozenset({
    "session",
    "roles",
    "events",
    "artifacts",
    "interventions",
    "projections",
    "lane_cursors",
    "stream_cursor",
})

WORKFLOW_CONFIG_KEYS = frozenset({
    "view_mode",
    "workflow_template",
    "max_rounds",
    "max_review_rounds",
    "roles",
})

ROLE_BINDING_KEYS = frozenset({
    "role_key",
    "tool_id",
    "enabled",
    "sort_order",
})


__all__ = [
    "ACTIVE_STAGES",
    "ARTIFACT_KINDS",
    "GET_ENDPOINTS",
    "HISTORY_KEYS",
    "INTERVENTION_STATUSES",
    "POST_ENDPOINTS",
    "ROLE_BINDING_KEYS",
    "ROLE_KEYS",
    "SESSION_STATE_KEYS",
    "SESSION_STATUSES",
    "STREAM_EVENT_TYPES",
    "VIEW_MODES",
    "WORKFLOW_CONFIG_KEYS",
    "is_approved",
    "is_closure",
]
