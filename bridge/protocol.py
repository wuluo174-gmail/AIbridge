"""
Bridge 协议常量 — 唯一权威真相源
================================
事件类型、状态枚举、API 端点、提示词键、payload 结构的权威定义。

本模块是 contract tests 的断言基准。docs/PROTOCOL.md 是本模块的人类可读说明书。
当文档与本模块冲突时，以本模块为准。

所有常量从 bridge.py 代码逐行提取，行号标注基于 commit cdc4613。
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
    "status_change",        # L640,646,761,809,857,1138,1224
    "round_start",          # L650
    "agent_thinking",       # L653,682,811,864,884
    "cli_start",            # L220,357 — 前端未处理
    "agent_chunk",          # L194,277,285,388,399,404,410
    "chunk_boundary",       # L411
    "agent_stderr",         # L192
    "agent_result",         # L331,427 — 前端 case 为空 break
    "agent_response",       # L676,700,1185,1218 (via add_history_event)
    "consensus_reached",    # L708
    "max_rounds_reached",   # L716
    "warning",              # L319
    "rollback",             # L746
    "error",                # L756,800,841,912

    # 执行阶段 (1 种)
    "execution_done",       # L786

    # 审查循环 (5 种)
    "review_start",         # L810
    "review_round_start",   # L858
    "review_response",      # L826,877,897 (via add_history_event)
    "review_needs_fix",     # L835,906
    "review_done",          # L831,851,902,1161
})


# ═════════════════════════════════════════════════════════════════
# 提示词配置键 (11 个) — prompts.json + 前端 cfgKeys (L1910)
# ═════════════════════════════════════════════════════════════════

PROMPT_KEYS = (
    "claude_first",                  # L452
    "claude_revise",                 # L474
    "codex_first",                   # L480
    "codex_review",                  # L491
    "execution",                     # L502
    "execution_unapproved",          # L506
    "codex_post_review",             # L592
    "claude_post_fix",               # L598
    "codex_post_review_followup",    # L605
    "user_inject_label_claude",      # L472
    "user_inject_label_codex",       # L489
)

PROMPT_KEYS_SET = frozenset(PROMPT_KEYS)


# ═════════════════════════════════════════════════════════════════
# HTTP API 端点
# ═════════════════════════════════════════════════════════════════

GET_ENDPOINTS = (
    "/",                    # L947 — HTML UI
    "/api/events",          # L954 — 事件轮询
    "/api/state",           # L962 — 会话状态
    "/api/sessions",        # L977 — 会话列表
    "/api/history",         # L988 — 协商/审查历史
    "/api/browse",          # L1021 — 目录浏览
    "/api/complete",        # L1051 — 路径补全
    "/api/recent_paths",    # L1080 — 最近路径
    "/api/prompts",         # L1082 — 提示词配置
)

POST_ENDPOINTS = (
    "/api/start",           # L1089 — 启动协商
    "/api/execute",         # L1113 — 触发执行
    "/api/stop",            # L1125 — 停止
    "/api/review_fix",      # L1140 — 确认修复
    "/api/review_skip",     # L1152 — 跳过修复
    "/api/prompts",         # L1163 — 更新提示词
    "/api/inject",          # L1169 — 注入反馈
    "/api/continue",        # L1187 — 继续协商
)


# ═════════════════════════════════════════════════════════════════
# API 响应结构键 (用于 contract test 断言)
# ═════════════════════════════════════════════════════════════════

STATE_RESPONSE_KEYS = frozenset({
    "status", "round", "max_rounds", "consensus",
    "consensus_round", "history_len", "error",
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
    "execution_done":       {"result"},
    "review_start":         {"round", "max"},
    "review_round_start":   {"round", "max"},
    "review_response":      {"round", "role", "phase", "content"},
    "review_needs_fix":     {"round", "msg", "review"},
    "review_done":          {"round", "msg", "success"},
}
