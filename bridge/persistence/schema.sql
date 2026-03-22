-- Bridge SQLite Schema
-- ====================
-- 只含可持久化项。线程锁、进程句柄等纯内存运行态不可持久化，
-- 重启后由统一会话账本重建可恢复会话。
--
-- Step 6 时由 Python sqlite3 执行建表。

-- 会话账本快照（创建即写入，生命周期内持续更新）
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    project_path TEXT NOT NULL,
    max_rounds INTEGER DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'running',
    phase TEXT NOT NULL DEFAULT 'negotiation',
    current_round INTEGER DEFAULT 0,
    consensus INTEGER DEFAULT 0,
    consensus_round INTEGER DEFAULT 0,
    planner_tool_id TEXT DEFAULT 'claude-code',
    reviewer_tool_id TEXT DEFAULT 'codex',
    execution_result TEXT,
    error TEXT,
    review_round INTEGER DEFAULT 0,
    max_review_rounds INTEGER DEFAULT 3,
    interrupt_reason TEXT,
    adapter_state_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
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

-- 事件日志（用于状态/历史回放）
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    event_index INTEGER NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE(session_id, event_index)
);
CREATE INDEX IF NOT EXISTS idx_session_events_sid ON session_events(session_id);

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
