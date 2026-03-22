CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    project_path TEXT NOT NULL,
    workflow_template TEXT NOT NULL,
    view_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    active_stage TEXT NOT NULL,
    current_round INTEGER NOT NULL DEFAULT 0,
    current_review_round INTEGER NOT NULL DEFAULT 0,
    consensus_round INTEGER NOT NULL DEFAULT 0,
    max_rounds INTEGER NOT NULL DEFAULT 5,
    max_review_rounds INTEGER NOT NULL DEFAULT 3,
    error TEXT,
    interrupt_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role_key TEXT NOT NULL,
    tool_id TEXT NOT NULL REFERENCES cli_tools(id),
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    resume_state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_roles_sid_role
ON workflow_roles(session_id, role_key);

CREATE TABLE IF NOT EXISTS role_lanes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role_key TEXT NOT NULL,
    lane_status TEXT NOT NULL,
    transport_kind TEXT NOT NULL,
    viewport_json TEXT NOT NULL DEFAULT '{}',
    last_seq INTEGER NOT NULL DEFAULT -1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_role_lanes_sid_role
ON role_lanes(session_id, role_key);

CREATE TABLE IF NOT EXISTS role_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    lane_id TEXT REFERENCES role_lanes(id) ON DELETE CASCADE,
    role_key TEXT,
    seq INTEGER NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_role_events_sid_seq
ON role_events(session_id, seq);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    lane_id TEXT NOT NULL REFERENCES role_lanes(id) ON DELETE CASCADE,
    role_key TEXT NOT NULL,
    round INTEGER NOT NULL,
    phase TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source_event_seq INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_sid_created
ON artifacts(session_id, created_at);

CREATE TABLE IF NOT EXISTS interventions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    origin_view TEXT NOT NULL,
    origin_role_key TEXT,
    target_scope TEXT NOT NULL,
    target_roles_json TEXT NOT NULL,
    text TEXT NOT NULL,
    command TEXT,
    status TEXT NOT NULL,
    consumed_by_roles_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interventions_sid_created
ON interventions(session_id, created_at);

CREATE TABLE IF NOT EXISTS cli_tools (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    agent_name TEXT,
    detected_installed INTEGER DEFAULT 0,
    executable_path TEXT,
    version TEXT,
    probe_error TEXT,
    capabilities_json TEXT DEFAULT '{}',
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS recent_paths (
    path TEXT PRIMARY KEY,
    last_used_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
