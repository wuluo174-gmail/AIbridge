-- Bridge SQLite Schema
-- ====================
-- 只含可持久化项。活动会话的运行态 (stop_flag, active_proc,
-- claude_session_id 等) 不可持久化，重启后不可续接。
--
-- Step 6 时由 Python sqlite3 执行建表。

-- 已完成会话快照
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    project_path TEXT NOT NULL,
    max_rounds INTEGER DEFAULT 5,
    final_status TEXT NOT NULL,            -- 只存终态: done / error
    current_round INTEGER DEFAULT 0,
    consensus INTEGER DEFAULT 0,
    consensus_round INTEGER DEFAULT 0,
    planner_tool_id TEXT DEFAULT 'claude-code',
    reviewer_tool_id TEXT DEFAULT 'codex',
    execution_result TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

-- 协商历史条目
CREATE TABLE IF NOT EXISTS session_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    round INTEGER NOT NULL,
    role TEXT NOT NULL,                     -- "claude" / "codex" / "user"
    phase TEXT NOT NULL,
    content TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_history_sid ON session_history(session_id);

-- 审查历史条目
CREATE TABLE IF NOT EXISTS review_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    round INTEGER NOT NULL,
    role TEXT NOT NULL,
    phase TEXT NOT NULL,
    content TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_history_sid ON review_history(session_id);

-- CLI 工具注册
CREATE TABLE IF NOT EXISTS cli_tools (
    id TEXT PRIMARY KEY,                   -- "claude-code", "codex"
    display_name TEXT NOT NULL,
    agent_name TEXT,
    detected_installed INTEGER DEFAULT 0,
    executable_path TEXT,
    version TEXT,
    probe_error TEXT,
    capabilities_json TEXT DEFAULT '{}',   -- 能力矩阵 JSON 快照
    last_checked_at TEXT
);

-- 角色分配
CREATE TABLE IF NOT EXISTS role_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT 'default',
    planner_tool_id TEXT NOT NULL REFERENCES cli_tools(id),
    reviewer_tool_id TEXT NOT NULL REFERENCES cli_tools(id),
    is_active INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_role_assignments_name
ON role_assignments(name);

-- 提示词模板
CREATE TABLE IF NOT EXISTS prompt_templates (
    key TEXT NOT NULL,                     -- 11 个键 (见 protocol.PROMPT_KEYS)
    tool_id TEXT,                          -- NULL=通用, 非空=工具特定覆盖
    value TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (key, tool_id)
);

-- 最近路径
CREATE TABLE IF NOT EXISTS recent_paths (
    path TEXT PRIMARY KEY,
    last_used_at TEXT NOT NULL
);

-- 内部元数据（迁移标记等）
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
