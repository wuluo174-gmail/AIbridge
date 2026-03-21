"""
Bridge 持久化存储
================
SQLite 持久化层 (Python sqlite3 标准库)。

设计约束:
  - Python 是唯一写入方
  - 活动会话运行态不可持久化
  - 只存已完成会话的终态快照
  - 重启后活动会话不可恢复（未到终态的会话从不写入 DB）

并发模型:
  - 单连接 + check_same_thread=False
  - 内部 threading.Lock 保护所有操作（读+写）
  - WAL journal mode

事务安全:
  - 所有多语句写操作使用 with self._conn: 上下文管理器
  - 自动 commit（成功）/ rollback（异常）
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

    def _ensure_schema_up_to_date(self):
        """幂等升级旧库 schema，使其与当前代码对齐。"""
        with self._lock, self._conn:
            c = self._conn.cursor()
            c.execute("PRAGMA table_info(cli_tools)")
            existing = {row[1] for row in c.fetchall()}
            if "detected_installed" not in existing:
                if "installed" in existing:
                    c.execute(
                        "ALTER TABLE cli_tools RENAME COLUMN installed TO detected_installed"
                    )
                else:
                    c.execute(
                        "ALTER TABLE cli_tools ADD COLUMN detected_installed INTEGER DEFAULT 0"
                    )
            required = {
                "agent_name": "TEXT",
                "probe_error": "TEXT",
            }
            for col, ddl in required.items():
                if col not in existing:
                    c.execute(f"ALTER TABLE cli_tools ADD COLUMN {col} {ddl}")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_role_assignments_name "
                "ON role_assignments(name)"
            )

    # ═════════════════════════════════════════════════════════════════
    # Sessions
    # ═════════════════════════════════════════════════════════════════

    def save_session(self, sess):
        """保存已完成会话终态快照（sessions + session_history + review_history）。"""
        finished_at = datetime.now().isoformat()
        with self._lock, self._conn:
            c = self._conn.cursor()
            # 幂等：先清理旧的子表记录，再全量写入
            c.execute("DELETE FROM session_history WHERE session_id = ?",
                      (sess.session_id,))
            c.execute("DELETE FROM review_history WHERE session_id = ?",
                      (sess.session_id,))
            c.execute(
                """INSERT OR REPLACE INTO sessions
                   (id, task, project_path, max_rounds, final_status,
                    current_round, consensus, consensus_round,
                    planner_tool_id, reviewer_tool_id,
                    execution_result, created_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sess.session_id, sess.task, sess.project_path,
                 sess.max_rounds, sess.status, sess.current_round,
                 1 if sess.consensus else 0, sess.consensus_round,
                 getattr(sess, 'planner_tool_id', 'claude-code'),
                 getattr(sess, 'reviewer_tool_id', 'codex'),
                 sess.execution_result, sess.created_at, finished_at))
            for h in sess.history:
                c.execute(
                    """INSERT INTO session_history
                       (session_id, round, role, phase, content, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (sess.session_id, h.get("round", 0), h["role"],
                     h.get("phase", ""), h.get("content", ""),
                     h.get("timestamp", finished_at)))
            for h in sess.review_history:
                c.execute(
                    """INSERT INTO review_history
                       (session_id, round, role, phase, content, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (sess.session_id, h.get("round", 0), h["role"],
                     h.get("phase", ""), h.get("content", ""),
                     h.get("timestamp", finished_at)))

    def get_session(self, session_id):
        """按 session_id 查单个会话 → dict 或 None。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = c.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in c.description]
            return dict(zip(cols, row))

    def list_sessions(self, limit=50, offset=0):
        """按 finished_at DESC 分页查询已归档会话。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """SELECT id, task, project_path, final_status,
                          current_round, max_rounds, consensus,
                          consensus_round, planner_tool_id, reviewer_tool_id,
                          created_at, finished_at
                   FROM sessions ORDER BY finished_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset))
            cols = [d[0] for d in c.description]
            rows = []
            for row in c.fetchall():
                d = dict(zip(cols, row))
                d["session_id"] = d.pop("id")
                d["consensus"] = bool(d["consensus"])
                rows.append(d)
            return rows

    def get_session_history(self, session_id):
        """查询指定会话的协商历史条目。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """SELECT round, role, phase, content, timestamp
                   FROM session_history WHERE session_id = ?
                   ORDER BY id""",
                (session_id,))
            cols = [d[0] for d in c.description]
            return [dict(zip(cols, row)) for row in c.fetchall()]

    def get_session_review_history(self, session_id):
        """查询指定会话的审查历史条目。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """SELECT round, role, phase, content, timestamp
                   FROM review_history WHERE session_id = ?
                   ORDER BY id""",
                (session_id,))
            cols = [d[0] for d in c.description]
            return [dict(zip(cols, row)) for row in c.fetchall()]

    # ═════════════════════════════════════════════════════════════════
    # Prompts
    # ═════════════════════════════════════════════════════════════════

    def save_prompts(self, config):
        """保存提示词配置到 prompt_templates（使用 __global__ 哨兵值）。"""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            c = self._conn.cursor()
            c.execute("DELETE FROM prompt_templates WHERE tool_id = ?",
                      (GLOBAL_TOOL_ID,))
            for key, value in config.items():
                c.execute(
                    """INSERT INTO prompt_templates
                       (key, tool_id, value, updated_at) VALUES (?, ?, ?, ?)""",
                    (key, GLOBAL_TOOL_ID, value, now))

    def load_prompts(self):
        """加载全局提示词配置 → dict。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "SELECT key, value FROM prompt_templates WHERE tool_id = ?",
                (GLOBAL_TOOL_ID,))
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
                    (path, ts))

    def load_recent_paths(self):
        """加载最近路径列表（按 last_used_at DESC）。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "SELECT path FROM recent_paths ORDER BY last_used_at DESC")
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
                """INSERT INTO cli_tools
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
                     last_checked_at=excluded.last_checked_at""",
                (tool_id, display_name, agent_name,
                 1 if detected_installed else 0, executable_path,
                 version, probe_error, capabilities, last_checked_at))

    def list_tools(self):
        """列出已注册 CLI 工具。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """SELECT id, display_name, agent_name, detected_installed,
                          executable_path, version, probe_error,
                          capabilities_json, last_checked_at
                   FROM cli_tools ORDER BY id"""
            )
            return [{"id": row[0],
                     "display_name": row[1],
                     "agent_name": row[2],
                     "detected_installed": bool(row[3]),
                     "executable_path": row[4],
                     "version": row[5],
                     "probe_error": row[6],
                     "capabilities": _json.loads(row[7]) if row[7] else {},
                     "last_checked_at": row[8]}
                    for row in c.fetchall()]

    # ═════════════════════════════════════════════════════════════════
    # Role Config
    # ═════════════════════════════════════════════════════════════════

    def save_role_config(self, planner_tool_id, reviewer_tool_id):
        """保存活跃角色配置（单活跃模型：写入即覆盖）。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO role_assignments
                   (name, planner_tool_id, reviewer_tool_id, is_active)
                   VALUES ('default', ?, ?, 1)
                   ON CONFLICT(name) DO UPDATE SET
                     planner_tool_id=excluded.planner_tool_id,
                     reviewer_tool_id=excluded.reviewer_tool_id,
                     is_active=1""",
                (planner_tool_id, reviewer_tool_id))

    def load_role_config(self):
        """加载活跃角色配置 → dict 或 None。"""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """SELECT planner_tool_id, reviewer_tool_id
                   FROM role_assignments WHERE is_active = 1 LIMIT 1""")
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
            c.execute("SELECT value FROM _meta WHERE key = ?",
                      (f"{domain}_migrated",))
            row = c.fetchone()
            return row is not None and row[0] == "true"

    def mark_migration_complete(self, domain):
        """标记指定域的迁移已完成。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (f"{domain}_migrated", "true"))

    # ═════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═════════════════════════════════════════════════════════════════

    def close(self):
        """关闭数据库连接。"""
        self._conn.close()
