"""
Bridge persistence store
========================
统一账本的 SQLite 持久化实现。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path


_SCHEMA_FILE = Path(__file__).parent / "schema.sql"
_DEFAULT_DB_DIR = Path.home() / ".bridge"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "bridge.db"
_DEFAULT_WORKFLOW_KEY = "default_workflow_config"
_ACTIVE_STATUSES = {"running", "executing", "validating", "repairing"}
_LEDGER_TABLES = (
    "role_events",
    "artifacts",
    "interventions",
    "role_lanes",
    "workflow_roles",
    "sessions",
)
_LEGACY_TABLES = (
    "session_events",
    "session_history",
    "review_history",
    "role_assignments",
)
_RESET_DROP_ORDER = (
    "role_events",
    "artifacts",
    "interventions",
    "role_lanes",
    "workflow_roles",
    "session_events",
    "session_history",
    "review_history",
    "role_assignments",
    "sessions",
)
_EXPECTED_LEDGER_COLUMNS = {
    "sessions": {
        "id",
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
    },
    "workflow_roles": {
        "id",
        "session_id",
        "role_key",
        "tool_id",
        "enabled",
        "sort_order",
        "resume_state_json",
        "created_at",
        "updated_at",
    },
    "role_lanes": {
        "id",
        "session_id",
        "role_key",
        "lane_status",
        "transport_kind",
        "viewport_json",
        "last_seq",
        "created_at",
        "updated_at",
    },
    "role_events": {
        "id",
        "session_id",
        "lane_id",
        "role_key",
        "seq",
        "source",
        "kind",
        "payload_json",
        "created_at",
    },
    "artifacts": {
        "id",
        "session_id",
        "lane_id",
        "role_key",
        "round",
        "phase",
        "artifact_kind",
        "content",
        "source_event_seq",
        "created_at",
    },
    "interventions": {
        "id",
        "session_id",
        "origin_view",
        "origin_role_key",
        "target_scope",
        "target_roles_json",
        "text",
        "command",
        "status",
        "consumed_by_roles_json",
        "created_at",
        "updated_at",
    },
}


class Store:
    def __init__(self, db_path=None):
        self._db_path = str(db_path) if db_path else str(_DEFAULT_DB_PATH)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_db()

    def init_db(self):
        schema_sql = _SCHEMA_FILE.read_text(encoding="utf-8")
        with self._lock:
            self._reset_legacy_schema_if_needed()
            self._drop_tables(_LEGACY_TABLES)
            with self._conn:
                self._conn.executescript(schema_sql)

    def _reset_legacy_schema_if_needed(self):
        if not self._ledger_schema_needs_reset():
            return
        self._drop_tables(_RESET_DROP_ORDER)

    def _existing_tables(self):
        cur = self._conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row[0] for row in cur.fetchall()}

    def _table_columns(self, table):
        cur = self._conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}

    def _ledger_schema_needs_reset(self):
        existing = self._existing_tables()
        tracked_existing = existing.intersection(_EXPECTED_LEDGER_COLUMNS)
        if not tracked_existing:
            return False
        if tracked_existing != set(_EXPECTED_LEDGER_COLUMNS):
            return True
        for table, expected in _EXPECTED_LEDGER_COLUMNS.items():
            if not expected.issubset(self._table_columns(table)):
                return True
        return False

    def _drop_tables(self, tables):
        existing = self._existing_tables()
        targets = [table for table in tables if table in existing]
        if not targets:
            return
        self._conn.commit()
        fk_enabled = bool(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])
        try:
            if fk_enabled:
                self._conn.execute("PRAGMA foreign_keys=OFF")
            with self._conn:
                for table in targets:
                    self._conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        finally:
            if fk_enabled:
                self._conn.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _row_dict(cursor, row):
        return {cursor.description[i][0]: value for i, value in enumerate(row)}

    @staticmethod
    def _event_payload_for_storage(event):
        payload = dict(event.get("data", {}))
        if isinstance(payload.get("projection"), dict):
            payload.pop("projection", None)
        return json.dumps(payload, ensure_ascii=False)

    def _upsert_session_row(self, sess):
        self._conn.execute(
            """
            INSERT INTO sessions
            (id, task, project_path, workflow_template, view_mode, status,
             active_stage, current_round, current_review_round, consensus_round,
             max_rounds, max_review_rounds, error, interrupt_reason,
             created_at, updated_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                task=excluded.task,
                project_path=excluded.project_path,
                workflow_template=excluded.workflow_template,
                view_mode=excluded.view_mode,
                status=excluded.status,
                active_stage=excluded.active_stage,
                current_round=excluded.current_round,
                current_review_round=excluded.current_review_round,
                consensus_round=excluded.consensus_round,
                max_rounds=excluded.max_rounds,
                max_review_rounds=excluded.max_review_rounds,
                error=excluded.error,
                interrupt_reason=excluded.interrupt_reason,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                finished_at=excluded.finished_at
            """,
            (
                sess.session_id,
                sess.task,
                sess.project_path,
                sess.workflow_template,
                sess.view_mode,
                sess.status,
                sess.active_stage,
                sess.current_round,
                sess.current_review_round,
                sess.consensus_round,
                sess.max_rounds,
                sess.max_review_rounds,
                sess.error,
                sess.interrupt_reason,
                sess.created_at,
                sess.updated_at,
                sess.finished_at,
            ),
        )

    def _upsert_role(self, sess, role_key, lane, now=None):
        now = now or datetime.now().isoformat()
        self._conn.execute(
            """
            INSERT INTO workflow_roles
            (session_id, role_key, tool_id, enabled, sort_order, resume_state_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, role_key) DO UPDATE SET
                tool_id=excluded.tool_id,
                enabled=excluded.enabled,
                sort_order=excluded.sort_order,
                resume_state_json=excluded.resume_state_json,
                updated_at=excluded.updated_at
            """,
            (
                sess.session_id,
                role_key,
                lane.tool_id,
                1 if lane.enabled else 0,
                lane.sort_order,
                json.dumps(lane.resume_state, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._conn.execute(
            """
            INSERT INTO role_lanes
            (id, session_id, role_key, lane_status, transport_kind, viewport_json, last_seq, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                lane_status=excluded.lane_status,
                transport_kind=excluded.transport_kind,
                viewport_json=excluded.viewport_json,
                last_seq=excluded.last_seq,
                updated_at=excluded.updated_at
            """,
            (
                lane.lane_id,
                sess.session_id,
                role_key,
                lane.lane_status,
                lane.transport_kind,
                json.dumps(lane.viewport, ensure_ascii=False),
                lane.last_seq,
                now,
                now,
            ),
        )

    def _upsert_artifact_row(self, sess, artifact):
        self._conn.execute(
            """
            INSERT INTO artifacts
            (id, session_id, lane_id, role_key, round, phase, artifact_kind, content, source_event_seq, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                lane_id=excluded.lane_id,
                role_key=excluded.role_key,
                round=excluded.round,
                phase=excluded.phase,
                artifact_kind=excluded.artifact_kind,
                content=excluded.content,
                source_event_seq=excluded.source_event_seq,
                created_at=excluded.created_at
            """,
            (
                artifact["id"],
                sess.session_id,
                artifact["lane_id"],
                artifact["role_key"],
                artifact["round"],
                artifact["phase"],
                artifact["artifact_kind"],
                artifact["content"],
                artifact.get("source_event_seq"),
                artifact["created_at"],
            ),
        )

    def _upsert_intervention_row(self, sess, intervention):
        self._conn.execute(
            """
            INSERT INTO interventions
            (id, session_id, origin_view, origin_role_key, target_scope, target_roles_json,
             text, command, status, consumed_by_roles_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                origin_view=excluded.origin_view,
                origin_role_key=excluded.origin_role_key,
                target_scope=excluded.target_scope,
                target_roles_json=excluded.target_roles_json,
                text=excluded.text,
                command=excluded.command,
                status=excluded.status,
                consumed_by_roles_json=excluded.consumed_by_roles_json,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at
            """,
            (
                intervention["id"],
                sess.session_id,
                intervention["origin_view"],
                intervention.get("origin_role_key"),
                intervention["target_scope"],
                json.dumps(intervention.get("target_roles", []), ensure_ascii=False),
                intervention.get("text", ""),
                intervention.get("command"),
                intervention["status"],
                json.dumps(intervention.get("consumed_by_roles", {}), ensure_ascii=False),
                intervention["created_at"],
                intervention["updated_at"],
            ),
        )

    def save_session_state(self, sess):
        with self._lock, self._conn:
            self._upsert_session_row(sess)
            now = datetime.now().isoformat()
            for role_key, lane in sess.roles.items():
                self._upsert_role(sess, role_key, lane, now)

    def upsert_lane(self, sess, role_key):
        with self._lock, self._conn:
            self._upsert_session_row(sess)
            lane = sess.roles[role_key]
            self._upsert_role(sess, role_key, lane)

    def append_event(self, sess, event, *, artifact=None, intervention=None):
        with self._lock, self._conn:
            self._upsert_session_row(sess)
            lane_id = None
            if event.get("role_key") and event["role_key"] in sess.roles:
                lane = sess.roles[event["role_key"]]
                lane_id = lane.lane_id
                self._upsert_role(sess, event["role_key"], lane)
            if artifact is not None:
                self._upsert_artifact_row(sess, artifact)
            if intervention is not None:
                self._upsert_intervention_row(sess, intervention)
            self._conn.execute(
                """
                INSERT INTO role_events
                (session_id, lane_id, role_key, seq, source, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, seq) DO UPDATE SET
                    lane_id=excluded.lane_id,
                    role_key=excluded.role_key,
                    source=excluded.source,
                    kind=excluded.kind,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (
                    sess.session_id,
                    lane_id,
                    event.get("role_key"),
                    event["id"],
                    event.get("source", "system"),
                    event["type"],
                    self._event_payload_for_storage(event),
                    event["ts"],
                ),
            )

    def upsert_artifact(self, sess, artifact):
        with self._lock, self._conn:
            self._upsert_session_row(sess)
            self._upsert_artifact_row(sess, artifact)

    def upsert_intervention(self, sess, intervention):
        with self._lock, self._conn:
            self._upsert_session_row(sess)
            self._upsert_intervention_row(sess, intervention)

    def save_session(self, sess):
        with self._lock, self._conn:
            self._upsert_session_row(sess)
            now = datetime.now().isoformat()
            for role_key, lane in sess.roles.items():
                self._upsert_role(sess, role_key, lane, now)

            for event in sess.stream_events:
                lane_id = None
                if event.get("role_key") and event["role_key"] in sess.roles:
                    lane_id = sess.roles[event["role_key"]].lane_id
                self._conn.execute(
                    """
                    INSERT INTO role_events
                    (session_id, lane_id, role_key, seq, source, kind, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, seq) DO UPDATE SET
                        lane_id=excluded.lane_id,
                        role_key=excluded.role_key,
                        source=excluded.source,
                        kind=excluded.kind,
                        payload_json=excluded.payload_json,
                        created_at=excluded.created_at
                    """,
                    (
                        sess.session_id,
                        lane_id,
                        event.get("role_key"),
                        event["id"],
                        event.get("source", "system"),
                        event["type"],
                        self._event_payload_for_storage(event),
                        event["ts"],
                    ),
                )

            for artifact in sess.artifacts:
                self._conn.execute(
                    """
                    INSERT INTO artifacts
                    (id, session_id, lane_id, role_key, round, phase, artifact_kind, content, source_event_seq, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        lane_id=excluded.lane_id,
                        role_key=excluded.role_key,
                        round=excluded.round,
                        phase=excluded.phase,
                        artifact_kind=excluded.artifact_kind,
                        content=excluded.content,
                        source_event_seq=excluded.source_event_seq,
                        created_at=excluded.created_at
                    """,
                    (
                        artifact["id"],
                        sess.session_id,
                        artifact["lane_id"],
                        artifact["role_key"],
                        artifact["round"],
                        artifact["phase"],
                        artifact["artifact_kind"],
                        artifact["content"],
                        artifact.get("source_event_seq"),
                        artifact["created_at"],
                    ),
                )

            for intervention in sess.interventions:
                self._conn.execute(
                    """
                    INSERT INTO interventions
                    (id, session_id, origin_view, origin_role_key, target_scope, target_roles_json,
                     text, command, status, consumed_by_roles_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        origin_view=excluded.origin_view,
                        origin_role_key=excluded.origin_role_key,
                        target_scope=excluded.target_scope,
                        target_roles_json=excluded.target_roles_json,
                        text=excluded.text,
                        command=excluded.command,
                        status=excluded.status,
                        consumed_by_roles_json=excluded.consumed_by_roles_json,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        intervention["id"],
                        sess.session_id,
                        intervention["origin_view"],
                        intervention.get("origin_role_key"),
                        intervention["target_scope"],
                        json.dumps(intervention.get("target_roles", []), ensure_ascii=False),
                        intervention.get("text", ""),
                        intervention.get("command"),
                        intervention["status"],
                        json.dumps(intervention.get("consumed_by_roles", {}), ensure_ascii=False),
                        intervention["created_at"],
                        intervention["updated_at"],
                    ),
                )

    def get_session_bundle(self, session_id):
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            session_row = c.fetchone()
            if not session_row:
                return None
            session_data = self._row_dict(c, session_row)

            c.execute(
                """
                SELECT role_key, tool_id, enabled, sort_order, resume_state_json
                FROM workflow_roles
                WHERE session_id = ?
                ORDER BY sort_order, role_key
                """,
                (session_id,),
            )
            roles = [self._row_dict(c, row) for row in c.fetchall()]

            c.execute(
                """
                SELECT id, role_key, lane_status, transport_kind, last_seq, viewport_json
                FROM role_lanes
                WHERE session_id = ?
                ORDER BY role_key
                """,
                (session_id,),
            )
            lanes = [self._row_dict(c, row) for row in c.fetchall()]

            c.execute(
                """
                SELECT seq, role_key, source, kind, payload_json, created_at
                FROM role_events
                WHERE session_id = ?
                ORDER BY seq
                """,
                (session_id,),
            )
            events = [
                {
                    "id": row[0],
                    "role_key": row[1],
                    "source": row[2],
                    "type": row[3],
                    "data": json.loads(row[4]) if row[4] else {},
                    "ts": row[5],
                }
                for row in c.fetchall()
            ]

            c.execute(
                """
                SELECT id, lane_id, role_key, round, phase, artifact_kind, content, source_event_seq, created_at
                FROM artifacts
                WHERE session_id = ?
                ORDER BY created_at
                """,
                (session_id,),
            )
            artifacts = [
                {
                    "id": row[0],
                    "session_id": session_id,
                    "lane_id": row[1],
                    "role_key": row[2],
                    "round": row[3],
                    "phase": row[4],
                    "artifact_kind": row[5],
                    "content": row[6],
                    "source_event_seq": row[7],
                    "created_at": row[8],
                }
                for row in c.fetchall()
            ]

            c.execute(
                """
                SELECT id, origin_view, origin_role_key, target_scope, target_roles_json,
                       text, command, status, consumed_by_roles_json, created_at, updated_at
                FROM interventions
                WHERE session_id = ?
                ORDER BY created_at
                """,
                (session_id,),
            )
            interventions = [
                {
                    "id": row[0],
                    "session_id": session_id,
                    "origin_view": row[1],
                    "origin_role_key": row[2],
                    "target_scope": row[3],
                    "target_roles": json.loads(row[4]) if row[4] else [],
                    "text": row[5],
                    "command": row[6],
                    "status": row[7],
                    "consumed_by_roles": json.loads(row[8]) if row[8] else {},
                    "created_at": row[9],
                    "updated_at": row[10],
                }
                for row in c.fetchall()
            ]

            return {
                "session": session_data,
                "workflow": {
                    "view_mode": session_data["view_mode"],
                    "workflow_template": session_data["workflow_template"],
                    "max_rounds": session_data["max_rounds"],
                    "max_review_rounds": session_data["max_review_rounds"],
                },
                "roles": roles,
                "lanes": lanes,
                "events": events,
                "artifacts": artifacts,
                "interventions": interventions,
            }

    def list_sessions(self, limit=50, offset=0):
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                SELECT id, task, project_path, view_mode, status, active_stage,
                       current_round, current_review_round, max_rounds, max_review_rounds,
                       updated_at, created_at, finished_at, interrupt_reason
                FROM sessions
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = []
            for row in c.fetchall():
                rows.append(
                    {
                        "session_id": row[0],
                        "task": row[1],
                        "project_path": row[2],
                        "view_mode": row[3],
                        "status": row[4],
                        "active_stage": row[5],
                        "current_round": row[6],
                        "current_review_round": row[7],
                        "max_rounds": row[8],
                        "max_review_rounds": row[9],
                        "updated_at": row[10],
                        "created_at": row[11],
                        "finished_at": row[12],
                        "interrupt_reason": row[13],
                    }
                )
            return rows

    def mark_incomplete_sessions_interrupted(self):
        with self._lock, self._conn:
            now = datetime.now().isoformat()
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

    def register_tool(self, tool_id, display_name, capabilities,
                      agent_name=None, detected_installed=False,
                      executable_path=None, version=None,
                      probe_error=None, last_checked_at=None):
        if not isinstance(capabilities, str):
            capabilities = json.dumps(capabilities, ensure_ascii=False)
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
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                SELECT id, display_name, agent_name, detected_installed, executable_path,
                       version, probe_error, capabilities_json, last_checked_at
                FROM cli_tools
                ORDER BY id
                """
            )
            return [
                {
                    "id": row[0],
                    "display_name": row[1],
                    "agent_name": row[2],
                    "detected_installed": bool(row[3]),
                    "executable_path": row[4],
                    "version": row[5],
                    "probe_error": row[6],
                    "capabilities": json.loads(row[7]) if row[7] else {},
                    "last_checked_at": row[8],
                }
                for row in c.fetchall()
            ]

    def save_prompts(self, config):
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM prompt_templates")
            for key, value in config.items():
                self._conn.execute(
                    "INSERT INTO prompt_templates (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )

    def load_prompts(self):
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT key, value FROM prompt_templates")
            return {row[0]: row[1] for row in c.fetchall()}

    def save_recent_paths(self, paths):
        base = datetime.now()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM recent_paths")
            for index, path in enumerate(paths):
                ts = (base - timedelta(microseconds=index)).isoformat()
                self._conn.execute(
                    "INSERT INTO recent_paths (path, last_used_at) VALUES (?, ?)",
                    (path, ts),
                )

    def load_recent_paths(self):
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT path FROM recent_paths ORDER BY last_used_at DESC")
            return [row[0] for row in c.fetchall()]

    def save_workflow_config(self, config: dict):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (_DEFAULT_WORKFLOW_KEY, json.dumps(config, ensure_ascii=False)),
            )

    def load_workflow_config(self):
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT value FROM _meta WHERE key = ?", (_DEFAULT_WORKFLOW_KEY,))
            row = c.fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def close(self):
        self._conn.close()
