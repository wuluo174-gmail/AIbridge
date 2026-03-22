"""
Bridge 持久化存储
================
SQLite 持久化层 (Python sqlite3 标准库)。

设计约束:
  - Python 是唯一写入方
  - 会话从创建开始即持久化，SQLite 是统一会话账本
  - 内存态（线程锁、进程句柄）不可持久化，重启后按账本重建可恢复会话

并发模型:
  - 单连接 + check_same_thread=False
  - 内部 threading.Lock 保护所有操作（读+写）
  - WAL journal mode
"""

import json as _json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path


_SCHEMA_FILE = Path(__file__).parent / "schema.sql"
_DEFAULT_DB_DIR = Path.home() / ".bridge"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "bridge.db"

GLOBAL_TOOL_ID = "__global__"
_ACTIVE_STATUSES = {"running", "executing", "review_pending"}


class Store:
    """SQLite 持久化存储。"""

    def __init__(self, db_path=None):
        self._db_path = str(db_path) if db_path else str(_DEFAULT_DB_PATH)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_db()

    # ═════════════════════════════════════════════════════════════════
    # Schema
    # ═════════════════════════════════════════════════════════════════

    def init_db(self):
        """使用 schema.sql 建表。"""
        schema_sql = _SCHEMA_FILE.read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(schema_sql)
        self._ensure_schema_up_to_date()

    def _add_column_if_missing(self, table, existing, col, ddl):
        if col not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def _ensure_schema_up_to_date(self):
        """幂等升级旧库 schema，使其与当前代码对齐。"""
        with self._lock, self._conn:
            c = self._conn.cursor()

            c.execute("PRAGMA table_info(cli_tools)")
            cli_existing = {row[1] for row in c.fetchall()}
            if "detected_installed" not in cli_existing:
                if "installed" in cli_existing:
                    c.execute(
                        "ALTER TABLE cli_tools RENAME COLUMN installed TO detected_installed"
                    )
                    cli_existing.remove("installed")
                    cli_existing.add("detected_installed")
                else:
                    c.execute(
                        "ALTER TABLE cli_tools ADD COLUMN detected_installed INTEGER DEFAULT 0"
                    )
            for col, ddl in {"agent_name": "TEXT", "probe_error": "TEXT"}.items():
                self._add_column_if_missing("cli_tools", cli_existing, col, ddl)

            c.execute("PRAGMA table_info(sessions)")
            session_existing = {row[1] for row in c.fetchall()}
            if "final_status" in session_existing:
                # 开发期无历史用户数据，旧 session ledger 直接作废并重建，
                # 避免继续维护 status/final_status 双真相源。
                self._reset_legacy_session_ledger()
                c.execute("PRAGMA table_info(sessions)")
                session_existing = {row[1] for row in c.fetchall()}
            session_cols = {
                "status": "TEXT",
                "phase": "TEXT DEFAULT 'negotiation'",
                "error": "TEXT",
                "review_round": "INTEGER DEFAULT 0",
                "max_review_rounds": "INTEGER DEFAULT 3",
                "interrupt_reason": "TEXT",
                "adapter_state_json": "TEXT DEFAULT '{}'",
                "updated_at": "TEXT",
            }
            for col, ddl in session_cols.items():
                self._add_column_if_missing("sessions", session_existing, col, ddl)

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    event_index INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    UNIQUE(session_id, event_index)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_events_sid "
                "ON session_events(session_id)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_role_assignments_name "
                "ON role_assignments(name)"
            )

            self._conn.execute(
                "UPDATE sessions SET status = COALESCE(status, 'done') "
                "WHERE status IS NULL OR status = ''"
            )
            self._conn.execute(
                """
                UPDATE sessions
                SET phase = CASE
                    WHEN status LIKE 'review_%' THEN 'review'
                    WHEN status = 'executing' THEN 'execution'
                    ELSE COALESCE(phase, 'negotiation')
                END
                WHERE phase IS NULL OR phase = ''
                """
            )
            self._conn.execute(
                """
                UPDATE sessions
                SET updated_at = COALESCE(updated_at, finished_at, created_at)
                WHERE updated_at IS NULL OR updated_at = ''
                """
            )

    def _reset_legacy_session_ledger(self):
        """删除开发期旧会话账本，按当前 schema 重建。

        旧模型把 status/final_status 作为双状态源，还和历史表通过外键耦合。
        在无历史用户数据前提下，直接清空旧会话账本比继续维护复杂迁移更可靠。
        """
        self._conn.execute("DROP TABLE IF EXISTS session_events")
        self._conn.execute("DROP TABLE IF EXISTS review_history")
        self._conn.execute("DROP TABLE IF EXISTS session_history")
        self._conn.execute("DROP TABLE IF EXISTS sessions")
        self._conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))

    # ═════════════════════════════════════════════════════════════════
    # Sessions
    # ═════════════════════════════════════════════════════════════════

    @staticmethod
    def _bool_to_int(value):
        return 1 if value else 0

    @staticmethod
    def _row_dict(cursor, row):
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    @staticmethod
    def _session_summary(row):
        d = dict(row)
        d["session_id"] = d.pop("id")
        d["round"] = d.pop("current_round")
        d["consensus"] = bool(d["consensus"])
        d["resume_available"] = d["status"] in {"paused", "interrupted"}
        return d

    def save_session(self, sess):
        """保存会话快照（创建即写入，生命周期内幂等覆盖）。"""
        now = datetime.now().isoformat()
        updated_at = getattr(sess, "updated_at", now) or now
        status = getattr(sess, "status", "running")
        finished_at = getattr(sess, "finished_at", None)
        if status in {"aborted", "done", "error"}:
            finished_at = finished_at or now
        else:
            finished_at = None

        adapter_state = getattr(sess, "adapter_state", {}) or {}
        phase = getattr(sess, "phase", "negotiation") or "negotiation"
        review_round = getattr(sess, "review_round", 0)
        max_review_rounds = getattr(sess, "max_review_rounds", 3)
        interrupt_reason = getattr(sess, "interrupt_reason", None)

        with self._lock, self._conn:
            c = self._conn.cursor()
            c.execute(
                """
                INSERT INTO sessions
                (id, task, project_path, max_rounds, status, phase,
                 current_round, consensus, consensus_round,
                 planner_tool_id, reviewer_tool_id,
                 execution_result, error, review_round, max_review_rounds,
                 interrupt_reason, adapter_state_json,
                 created_at, updated_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task=excluded.task,
                    project_path=excluded.project_path,
                    max_rounds=excluded.max_rounds,
                    status=excluded.status,
                    phase=excluded.phase,
                    current_round=excluded.current_round,
                    consensus=excluded.consensus,
                    consensus_round=excluded.consensus_round,
                    planner_tool_id=excluded.planner_tool_id,
                    reviewer_tool_id=excluded.reviewer_tool_id,
                    execution_result=excluded.execution_result,
                    error=excluded.error,
                    review_round=excluded.review_round,
                    max_review_rounds=excluded.max_review_rounds,
                    interrupt_reason=excluded.interrupt_reason,
                    adapter_state_json=excluded.adapter_state_json,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    finished_at=excluded.finished_at
                """,
                (
                    sess.session_id,
                    sess.task,
                    sess.project_path,
                    sess.max_rounds,
                    status,
                    phase,
                    sess.current_round,
                    self._bool_to_int(sess.consensus),
                    sess.consensus_round,
                    getattr(sess, "planner_tool_id", "claude-code"),
                    getattr(sess, "reviewer_tool_id", "codex"),
                    sess.execution_result,
                    sess.error,
                    review_round,
                    max_review_rounds,
                    interrupt_reason,
                    _json.dumps(adapter_state, ensure_ascii=False),
                    sess.created_at,
                    updated_at,
                    finished_at,
                ),
            )

            c.execute("DELETE FROM session_history WHERE session_id = ?", (sess.session_id,))
            c.execute("DELETE FROM review_history WHERE session_id = ?", (sess.session_id,))
            for h in sess.history:
                c.execute(
                    """
                    INSERT INTO session_history
                    (session_id, round, role, phase, content, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sess.session_id,
                        h.get("round", 0),
                        h["role"],
                        h.get("phase", ""),
                        h.get("content", ""),
                        h.get("timestamp", updated_at),
                    ),
                )
            for h in sess.review_history:
                c.execute(
                    """
                    INSERT INTO review_history
                    (session_id, round, role, phase, content, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sess.session_id,
                        h.get("round", 0),
                        h["role"],
                        h.get("phase", ""),
                        h.get("content", ""),
                        h.get("timestamp", updated_at),
                    ),
                )
            for evt in getattr(sess, "events", []):
                c.execute(
                    """
                    INSERT INTO session_events
                    (session_id, event_index, type, data_json, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, event_index) DO UPDATE SET
                        type=excluded.type,
                        data_json=excluded.data_json,
                        timestamp=excluded.timestamp
                    """,
                    (
                        sess.session_id,
                        evt["id"],
                        evt["type"],
                        _json.dumps(evt["data"], ensure_ascii=False),
                        evt["ts"],
                    ),
                )

    def get_session(self, session_id):
        """按 session_id 查单个会话 → dict 或 None。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = c.fetchone()
            if row is None:
                return None
            return self._row_dict(c, row)

    def list_sessions(self, limit=50, offset=0):
        """按 updated_at DESC 分页查询统一会话索引。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                SELECT id, task, project_path, status, phase,
                       current_round, max_rounds, consensus, consensus_round,
                       planner_tool_id, reviewer_tool_id, created_at, updated_at,
                       finished_at, interrupt_reason
                FROM sessions
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            return [self._session_summary(self._row_dict(c, row)) for row in c.fetchall()]

    def get_session_history(self, session_id):
        """查询指定会话的协商历史条目。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                SELECT round, role, phase, content, timestamp
                FROM session_history
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            )
            return [self._row_dict(c, row) for row in c.fetchall()]

    def get_session_review_history(self, session_id):
        """查询指定会话的审查历史条目。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                SELECT round, role, phase, content, timestamp
                FROM review_history
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            )
            return [self._row_dict(c, row) for row in c.fetchall()]

    def get_session_events(self, session_id):
        """查询指定会话的事件流。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                SELECT event_index, type, data_json, timestamp
                FROM session_events
                WHERE session_id = ?
                ORDER BY event_index
                """,
                (session_id,),
            )
            rows = []
            for row in c.fetchall():
                rows.append({
                    "id": row[0],
                    "type": row[1],
                    "data": _json.loads(row[2]) if row[2] else {},
                    "ts": row[3],
                })
            return rows

    def mark_incomplete_sessions_interrupted(self):
        """进程重启后，把活动中的会话统一标记为 interrupted。"""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
            self._conn.execute(
                f"""
                UPDATE sessions
                SET status = 'interrupted',
                    interrupt_reason = COALESCE(interrupt_reason, 'backend_restart'),
                    updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (now, *_ACTIVE_STATUSES),
            )

    # ═════════════════════════════════════════════════════════════════
    # Prompts
    # ═════════════════════════════════════════════════════════════════

    def save_prompts(self, config):
        """保存提示词配置到 prompt_templates（使用 __global__ 哨兵值）。"""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            c = self._conn.cursor()
            c.execute("DELETE FROM prompt_templates WHERE tool_id = ?", (GLOBAL_TOOL_ID,))
            for key, value in config.items():
                c.execute(
                    """
                    INSERT INTO prompt_templates
                    (key, tool_id, value, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, GLOBAL_TOOL_ID, value, now),
                )

    def load_prompts(self):
        """加载全局提示词配置 → dict。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "SELECT key, value FROM prompt_templates WHERE tool_id = ?",
                (GLOBAL_TOOL_ID,),
            )
            return {row[0]: row[1] for row in c.fetchall()}

    # ═════════════════════════════════════════════════════════════════
    # Recent Paths
    # ═════════════════════════════════════════════════════════════════

    def save_recent_paths(self, paths):
        """保存最近路径列表（单调递减时间戳保 MRU 位序）。"""
        base = datetime.now()
        with self._lock, self._conn:
            c = self._conn.cursor()
            c.execute("DELETE FROM recent_paths")
            for i, path in enumerate(paths):
                ts = (base - timedelta(microseconds=i)).isoformat()
                c.execute(
                    "INSERT INTO recent_paths (path, last_used_at) VALUES (?, ?)",
                    (path, ts),
                )

    def load_recent_paths(self):
        """加载最近路径列表（按 last_used_at DESC）。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT path FROM recent_paths ORDER BY last_used_at DESC")
            return [row[0] for row in c.fetchall()]

    # ═════════════════════════════════════════════════════════════════
    # CLI Tools
    # ═════════════════════════════════════════════════════════════════

    def register_tool(self, tool_id, display_name, capabilities,
                      agent_name=None, detected_installed=False,
                      executable_path=None, version=None,
                      probe_error=None, last_checked_at=None):
        """注册或更新 CLI 工具信息。"""
        if not isinstance(capabilities, str):
            capabilities = _json.dumps(capabilities, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO cli_tools
                (id, display_name, agent_name, detected_installed, executable_path,
                 version, probe_error, capabilities_json, last_checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name=excluded.display_name,
                    agent_name=excluded.agent_name,
                    detected_installed=excluded.detected_installed,
                    executable_path=excluded.executable_path,
                    version=excluded.version,
                    probe_error=excluded.probe_error,
                    capabilities_json=excluded.capabilities_json,
                    last_checked_at=excluded.last_checked_at
                """,
                (
                    tool_id,
                    display_name,
                    agent_name,
                    1 if detected_installed else 0,
                    executable_path,
                    version,
                    probe_error,
                    capabilities,
                    last_checked_at,
                ),
            )

    def list_tools(self):
        """列出已注册 CLI 工具。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                SELECT id, display_name, agent_name, detected_installed,
                       executable_path, version, probe_error,
                       capabilities_json, last_checked_at
                FROM cli_tools
                ORDER BY id
                """
            )
            return [{
                "id": row[0],
                "display_name": row[1],
                "agent_name": row[2],
                "detected_installed": bool(row[3]),
                "executable_path": row[4],
                "version": row[5],
                "probe_error": row[6],
                "capabilities": _json.loads(row[7]) if row[7] else {},
                "last_checked_at": row[8],
            } for row in c.fetchall()]

    # ═════════════════════════════════════════════════════════════════
    # Role Config
    # ═════════════════════════════════════════════════════════════════

    def save_role_config(self, planner_tool_id, reviewer_tool_id):
        """保存活跃角色配置（单活跃模型：写入即覆盖）。"""
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO role_assignments
                (name, planner_tool_id, reviewer_tool_id, is_active)
                VALUES ('default', ?, ?, 1)
                ON CONFLICT(name) DO UPDATE SET
                    planner_tool_id=excluded.planner_tool_id,
                    reviewer_tool_id=excluded.reviewer_tool_id,
                    is_active=1
                """,
                (planner_tool_id, reviewer_tool_id),
            )

    def load_role_config(self):
        """加载活跃角色配置 → dict 或 None。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                SELECT planner_tool_id, reviewer_tool_id
                FROM role_assignments
                WHERE is_active = 1
                LIMIT 1
                """
            )
            row = c.fetchone()
            if row:
                return {"planner_tool_id": row[0], "reviewer_tool_id": row[1]}
            return None

    # ═════════════════════════════════════════════════════════════════
    # Migration Markers (_meta)
    # ═════════════════════════════════════════════════════════════════

    def is_migration_complete(self, domain):
        """检查指定域的 JSON→SQLite 迁移是否已完成。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT value FROM _meta WHERE key = ?", (f"{domain}_migrated",))
            row = c.fetchone()
            return row is not None and row[0] == "true"

    def mark_migration_complete(self, domain):
        """标记指定域的迁移已完成。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (f"{domain}_migrated", "true"),
            )

    # ═════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═════════════════════════════════════════════════════════════════

    def close(self):
        """关闭数据库连接。"""
        self._conn.close()
