"""
Bridge 协议常量 — 唯一权威真相源
================================
事件类型、状态枚举、API 端点、提示词键、payload 结构的权威定义。

本模块是 contract tests 的断言基准。docs/PROTOCOL.md 是本模块的人类可读说明书。
当文档与本模块冲突时，以本模块为准。

所有常量从 bridge.py 代码逐行提取，行号标注基于 commit cdc4613。
Step 7: 新增 /api/tools, /api/role_config 端点 + 扩展响应键。
"""

import re


# ═════════════════════════════════════════════════════════════════
# 状态枚举 (9 种) — bridge.py L1891
# ═════════════════════════════════════════════════════════════════

STATES = frozenset({
    "idle",
    "running",
    "consensus",
    "max_rounds",
    "executing",
    "review_pending",
    "review_fix",
    "done",
    "error",
})

# 可从中触发执行的状态
EXECUTABLE_STATES = frozenset({"consensus", "max_rounds"})

# 可从中触发修复的状态
FIXABLE_STATES = frozenset({"review_fix"})

# 可从中继续协商的状态
CONTINUABLE_STATES = frozenset({"consensus", "max_rounds"})

# 终态 (轮询停止)
TERMINAL_STATES = frozenset({"idle", "done", "error"})


# ═════════════════════════════════════════════════════════════════
# 协议判定辅助 — 审批/收口检测的权威实现
# ═════════════════════════════════════════════════════════════════

def is_approved(text):
    """审查通过判定：首行首词为 APPROVED（大小写不敏感）。"""
    if not text:
        return False
    first_line = text.strip().split("\n")[0]
    return bool(re.match(r'\s*APPROVED\b', first_line, re.IGNORECASE))


# ═════════════════════════════════════════════════════════════════
# 事件类型 (20 种) — bridge.py add_event() 调用 + 前端 handle(e)
# ═════════════════════════════════════════════════════════════════

EVENT_TYPES = frozenset({
    # 协商阶段 (14 种)
    "status_change",
    "round_start",
    "agent_thinking",
    "cli_start",
    "agent_chunk",
    "chunk_boundary",
    "agent_stderr",
    "agent_result",
    "agent_response",
    "consensus_reached",
    "max_rounds_reached",
    "warning",
    "rollback",
    "error",

    # 执行阶段 (1 种)
    "execution_done",

    # 审查循环 (5 种)
    "review_start",
    "review_round_start",
    "review_response",
    "review_needs_fix",
    "review_done",
})


# ═════════════════════════════════════════════════════════════════
# 提示词配置键 (11 个) — prompts.json + 前端 cfgKeys
# ═════════════════════════════════════════════════════════════════

PROMPT_KEYS = (
    "claude_first",
    "claude_revise",
    "codex_first",
    "codex_review",
    "execution",
    "execution_unapproved",
    "codex_post_review",
    "claude_post_fix",
    "codex_post_review_followup",
    "user_inject_label_claude",
    "user_inject_label_codex",
)

PROMPT_KEYS_SET = frozenset(PROMPT_KEYS)


# ═════════════════════════════════════════════════════════════════
# HTTP API 端点
# ═════════════════════════════════════════════════════════════════

GET_ENDPOINTS = (
    "/",                    # HTML UI
    "/api/events",          # 事件轮询
    "/api/state",           # 会话状态
    "/api/sessions",        # 会话列表
    "/api/history",         # 协商/审查历史
    "/api/browse",          # 目录浏览
    "/api/complete",        # 路径补全
    "/api/recent_paths",    # 最近路径
    "/api/prompts",         # 提示词配置
    "/api/archived_sessions",         # 已归档会话列表
    "/api/archived_session_history",  # 已归档会话详细历史
    "/api/tools",           # Step 7: 已注册工具列表
    "/api/role_config",     # Step 7: 角色配置
)

POST_ENDPOINTS = (
    "/api/start",           # 启动协商
    "/api/execute",         # 触发执行
    "/api/stop",            # 停止
    "/api/review_fix",      # 确认修复
    "/api/review_skip",     # 跳过修复
    "/api/prompts",         # 更新提示词
    "/api/inject",          # 注入反馈
    "/api/continue",        # 继续协商
    "/api/role_config",     # Step 7: 更新角色配置
)


# ═════════════════════════════════════════════════════════════════
# API 响应结构键 (用于 contract test 断言)
# ═════════════════════════════════════════════════════════════════

STATE_RESPONSE_KEYS = frozenset({
    "status", "round", "max_rounds", "consensus",
    "consensus_round", "history_len", "error",
    "planner_tool_id", "reviewer_tool_id", "executor_panel",
})

HISTORY_RESPONSE_KEYS = frozenset({
    "entries", "execution_result", "review_entries",
    "review_round", "review_status", "event_cursor",
})

EVENTS_RESPONSE_KEYS = frozenset({"events", "next"})

SESSIONS_RESPONSE_KEYS = frozenset({"sessions"})

SESSION_LISTING_KEYS = frozenset({
    "session_id", "task", "project_path", "status", "round", "max_rounds",
})

BROWSE_RESPONSE_KEYS = frozenset({
    "current", "parent", "dirs", "is_git", "truncated",
})

COMPLETE_RESPONSE_KEYS = frozenset({"suggestions"})

RECENT_PATHS_RESPONSE_KEYS = frozenset({"paths"})

START_RESPONSE_KEYS = frozenset({"ok", "session_id"})

ARCHIVED_SESSIONS_RESPONSE_KEYS = frozenset({"sessions"})

ARCHIVED_SESSION_LISTING_KEYS = frozenset({
    "session_id", "task", "project_path", "final_status",
    "current_round", "max_rounds", "consensus", "consensus_round",
    "planner_tool_id", "reviewer_tool_id",
    "created_at", "finished_at",
})

ARCHIVED_HISTORY_RESPONSE_KEYS = frozenset({
    "entries", "execution_result", "review_entries",
    "review_round", "review_status", "event_cursor",
    "planner_tool_id", "reviewer_tool_id",
})

# Step 7: 工具/角色配置响应键
TOOLS_RESPONSE_KEYS = frozenset({"tools"})

TOOL_LISTING_KEYS = frozenset({
    "id", "display_name", "agent_name", "detected_installed",
    "executable_path", "version", "probe_error", "last_checked_at",
    "capabilities",
})

ROLE_CONFIG_RESPONSE_KEYS = frozenset({
    "planner_tool_id", "reviewer_tool_id", "executor_tool_id", "tools",
})


# ═════════════════════════════════════════════════════════════════
# 事件 payload 必需字段 (用于 contract test 断言)
# 从 bridge.py add_event 调用逐项提取
# ═════════════════════════════════════════════════════════════════

EVENT_PAYLOAD_REQUIRED_KEYS = {
    "status_change":        {"status", "msg"},
    "round_start":          {"round", "max"},
    "agent_thinking":       {"agent", "round"},
    "cli_start":            {"agent", "round"},
    "agent_chunk":          {"agent", "text"},       # chunk_type 可选
    "chunk_boundary":       {"agent", "boundary_type"},
    "agent_stderr":         {"agent", "text", "is_mcp"},
    "agent_result":         {"agent", "text"},
    "agent_response":       {"round", "role", "phase", "content"},
    "consensus_reached":    {"round", "msg"},
    "max_rounds_reached":   {"round", "msg"},
    "warning":              {"msg"},
    "rollback":             {"round", "max", "plan", "msg"},
    "error":                {"msg"},
    "execution_done":       {"result", "executor_panel"},
    "review_start":         {"round", "max"},
    "review_round_start":   {"round", "max"},
    "review_response":      {"round", "role", "phase", "content"},
    "review_needs_fix":     {"round", "msg", "review"},
    "review_done":          {"round", "msg", "success"},
}
