"""
Bridge v4 contract tests.

这些测试不再为旧的二角色 history/inject 架构背书，而是直接校验：

- 四角色工作流常量
- 统一账本模型
- SQLite round-trip
- 协商/执行/校验主链路
- 统一输入入口
"""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

import bridge.session as session_mod
from bridge.orchestration.engine import run_execution, run_negotiation
from bridge.persistence.store import Store
from bridge.protocol import (
    ACTIVE_STAGES,
    ARTIFACT_KINDS,
    GET_ENDPOINTS,
    HISTORY_KEYS,
    INTERVENTION_STATUSES,
    POST_ENDPOINTS,
    ROLE_BINDING_KEYS,
    SESSION_STATE_KEYS,
    SESSION_STATUSES,
    STREAM_EVENT_TYPES,
    WORKFLOW_CONFIG_KEYS,
    is_approved,
    is_closure,
)
from bridge.session import (
    SessionState,
    add_event,
    add_intervention,
    consume_interventions,
    event_snapshot,
    publish_artifact,
    session_event_payload,
    set_persist_hook,
    touch_status,
)
from bridge.workflow import (
    ROLE_KEYS,
    VIEW_MODES,
    default_workflow_config,
    normalize_workflow_config,
    target_roles_for_stage,
)


ROOT = Path(__file__).resolve().parent.parent
BRIDGE_PY = ROOT / "bridge.py"


def load_bridge_module():
    spec = importlib.util.spec_from_file_location(f"bridge_app_{uuid.uuid4().hex}", BRIDGE_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeAdapter:
    def detect_approval(self, text: str) -> bool:
        return is_approved(text)

    def detect_closure(self, text: str) -> bool:
        return is_closure(text)


class ProtocolContractTests(unittest.TestCase):
    def test_roles_modes_and_statuses_are_v4(self):
        self.assertEqual(ROLE_KEYS, ("planner", "reviewer", "executor", "validator"))
        self.assertEqual(VIEW_MODES, ("terminal", "scene"))
        self.assertIn("repairing", SESSION_STATUSES)
        self.assertIn("validating", SESSION_STATUSES)
        self.assertNotIn("review_pending", SESSION_STATUSES)
        self.assertIn("awaiting_execution", ACTIVE_STAGES)

    def test_endpoints_are_v4_only(self):
        self.assertIn("/api/workflow_config", GET_ENDPOINTS)
        self.assertIn("/api/session/start", POST_ENDPOINTS)
        self.assertIn("/api/session/view_mode", POST_ENDPOINTS)
        self.assertIn("/api/input", POST_ENDPOINTS)
        self.assertIn("/api/stream", GET_ENDPOINTS)
        self.assertNotIn("/api/inject", POST_ENDPOINTS)
        self.assertNotIn("/api/role_config", GET_ENDPOINTS)
        self.assertNotIn("/api/events", GET_ENDPOINTS)

    def test_schema_key_sets_exist(self):
        self.assertEqual(
            SESSION_STATE_KEYS,
            {
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
            },
        )
        self.assertEqual(HISTORY_KEYS, {"session", "roles", "events", "artifacts", "interventions", "projections", "lane_cursors", "stream_cursor"})
        self.assertEqual(WORKFLOW_CONFIG_KEYS, {"view_mode", "workflow_template", "max_rounds", "max_review_rounds", "roles"})
        self.assertEqual(ROLE_BINDING_KEYS, {"role_key", "tool_id", "enabled", "sort_order"})
        self.assertIn("consumed", INTERVENTION_STATUSES)
        self.assertIn("artifact.published", STREAM_EVENT_TYPES)
        self.assertIn("session.view_mode_changed", STREAM_EVENT_TYPES)
        self.assertIn("lane.viewport_changed", STREAM_EVENT_TYPES)
        self.assertIn("validation_report", ARTIFACT_KINDS)

    def test_approval_and_closure_detection(self):
        self.assertTrue(is_approved("APPROVED\nlooks good"))
        self.assertTrue(is_approved("approved"))
        self.assertFalse(is_approved("not approved"))
        self.assertTrue(is_closure("任务收口成功：可以结束"))
        self.assertFalse(is_closure("还没有收口"))


class WorkflowContractTests(unittest.TestCase):
    def test_default_workflow_has_four_roles(self):
        cfg = default_workflow_config()
        self.assertEqual([role.role_key for role in cfg.roles], list(ROLE_KEYS))
        self.assertEqual(cfg.view_mode, "scene")

    def test_normalize_workflow_backfills_missing_roles_and_clamps_ranges(self):
        cfg = normalize_workflow_config(
            {
                "view_mode": "invalid",
                "max_rounds": 99,
                "max_review_rounds": 0,
                "roles": [
                    {"role_key": "reviewer", "tool_id": "claude-code", "enabled": True, "sort_order": 0},
                ],
            }
        )
        self.assertEqual(cfg.view_mode, "scene")
        self.assertEqual(cfg.max_rounds, 20)
        self.assertEqual(cfg.max_review_rounds, 1)
        self.assertEqual(set(role.role_key for role in cfg.roles), set(ROLE_KEYS))
        self.assertEqual(next(role.tool_id for role in cfg.roles if role.role_key == "reviewer"), "claude-code")

    def test_stage_to_target_role_mapping(self):
        self.assertEqual(target_roles_for_stage("planning"), ("planner", "reviewer"))
        self.assertEqual(target_roles_for_stage("reviewing"), ("planner", "reviewer"))
        self.assertEqual(target_roles_for_stage("executing"), ("executor", "validator"))
        self.assertEqual(target_roles_for_stage("repairing"), ("executor", "validator"))


class SessionLedgerTests(unittest.TestCase):
    def test_events_artifacts_and_interventions_are_first_class(self):
        sess = SessionState("sess1", "task", "/tmp", default_workflow_config())

        evt = add_event(
            sess,
            "lane.stdout_chunk",
            {"text": "hello"},
            role_key="planner",
            source="claude-code",
        )
        self.assertEqual(evt["id"], 0)
        self.assertEqual(sess.roles["planner"].last_seq, 0)
        self.assertNotIn("projection", evt["data"])
        public_evt = event_snapshot(evt, include_projection=True)
        self.assertEqual(public_evt["data"]["projection"]["terminal"]["planner"], "hello")

        artifact = publish_artifact(
            sess,
            role_key="planner",
            round_no=1,
            phase="planning",
            artifact_kind="plan",
            content="# plan",
            source_event_seq=evt["id"],
        )
        self.assertEqual(artifact["artifact_kind"], "plan")
        self.assertEqual(len(sess.artifacts), 1)
        self.assertEqual(sess.stream_events[-1]["type"], "artifact.published")
        self.assertEqual(sess.stream_events[-1]["data"]["artifact"]["id"], artifact["id"])
        public_artifact_event = event_snapshot(sess.stream_events[-1], include_projection=True)
        self.assertEqual(public_artifact_event["data"]["projection"]["scene"]["id"], f"artifact-{artifact['id']}")

        intervention = add_intervention(
            sess,
            origin_view="scene",
            origin_role_key=None,
            target_roles=("planner", "reviewer"),
            target_scope="planning",
            text="补上恢复策略",
        )
        self.assertEqual(intervention["status"], "queued")
        self.assertEqual(sess.stream_events[-1]["data"]["intervention"]["id"], intervention["id"])
        public_intervention_event = event_snapshot(sess.stream_events[-1], include_projection=True)
        self.assertEqual(public_intervention_event["data"]["projection"]["scene"]["id"], f"intervention-{intervention['id']}")

        planner_inputs = consume_interventions(sess, "planner", 1)
        self.assertEqual(planner_inputs, ["补上恢复策略"])
        self.assertEqual(intervention["status"], "acknowledged")
        self.assertEqual(sess.stream_events[-1]["data"]["intervention"]["status"], "acknowledged")

        reviewer_inputs = consume_interventions(sess, "reviewer", 1)
        self.assertEqual(reviewer_inputs, ["补上恢复策略"])
        self.assertEqual(intervention["status"], "consumed")
        self.assertIn("planner", intervention["consumed_by_roles"])
        self.assertIn("reviewer", intervention["consumed_by_roles"])
        self.assertEqual(sess.stream_events[-1]["data"]["intervention"]["status"], "consumed")


class StoreContractTests(unittest.TestCase):
    def test_store_resets_legacy_schema_and_round_trips_v4_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bridge.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, task TEXT)")
            conn.execute("CREATE TABLE session_history (id INTEGER)")
            conn.commit()
            conn.close()

            store = Store(str(db_path))
            try:
                store.register_tool("claude-code", "Claude Code", {"dangerous_mode": True})
                store.register_tool("codex", "Codex", {"dangerous_mode": False})
                c = store._conn.cursor()
                c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {row[0] for row in c.fetchall()}
                self.assertIn("sessions", tables)
                self.assertIn("workflow_roles", tables)
                self.assertIn("role_lanes", tables)
                self.assertIn("role_events", tables)
                self.assertIn("artifacts", tables)
                self.assertIn("interventions", tables)
                self.assertNotIn("session_history", tables)

                sess = SessionState("sess2", "task", "/tmp", default_workflow_config())
                add_event(sess, "lane.stdout_chunk", {"text": "hi"}, role_key="planner", source="claude-code")
                publish_artifact(
                    sess,
                    role_key="planner",
                    round_no=1,
                    phase="planning",
                    artifact_kind="plan",
                    content="# plan",
                )
                add_intervention(
                    sess,
                    origin_view="scene",
                    origin_role_key=None,
                    target_roles=("planner", "reviewer"),
                    target_scope="planning",
                    text="补上 artifact 生命周期",
                )
                touch_status(sess, status="paused", active_stage="planning", interrupt_reason="user_paused")

                store.save_session(sess)
                bundle = store.get_session_bundle("sess2")
                self.assertIsNotNone(bundle)
                self.assertEqual(bundle["session"]["status"], "paused")
                self.assertEqual(len(bundle["roles"]), 4)
                self.assertEqual(len(bundle["lanes"]), 4)
                self.assertEqual(len(bundle["events"]), 3)
                self.assertEqual(len(bundle["artifacts"]), 1)
                self.assertEqual(len(bundle["interventions"]), 1)
                self.assertNotIn("projection", bundle["events"][0]["data"])
                public_event = event_snapshot(bundle["events"][0], include_projection=True)
                self.assertEqual(public_event["data"]["projection"]["terminal"]["planner"], "hi")
            finally:
                store.close()

    def test_store_resets_partial_v4_schema_even_when_sessions_table_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bridge.db"
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """
                CREATE TABLE workflow_roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role_key TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE role_lanes (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role_key TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            store = Store(str(db_path))
            try:
                c = store._conn.cursor()
                c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {row[0] for row in c.fetchall()}
                self.assertIn("sessions", tables)
                self.assertIn("workflow_roles", tables)
                self.assertIn("role_lanes", tables)
                self.assertIn("role_events", tables)
                self.assertIn("artifacts", tables)
                self.assertIn("interventions", tables)

                c.execute("PRAGMA table_info(role_lanes)")
                lane_cols = {row[1] for row in c.fetchall()}
                self.assertIn("viewport_json", lane_cols)

                c.execute("PRAGMA table_info(sessions)")
                session_cols = {row[1] for row in c.fetchall()}
                self.assertIn("workflow_template", session_cols)
                self.assertIn("current_review_round", session_cols)
            finally:
                store.close()

    def test_event_persistence_is_single_bundle_for_session_lane_artifact_and_intervention(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bridge.db"
            store = Store(str(db_path))
            previous_hook = session_mod._persist_hook
            try:
                store.register_tool("claude-code", "Claude Code", {"dangerous_mode": True})
                store.register_tool("codex", "Codex", {"dangerous_mode": False})
                set_persist_hook(store)

                sess = SessionState("sess_bundle", "task", "/tmp", default_workflow_config())
                store.save_session_state(sess)

                touch_status(sess, status="paused", active_stage="planning", interrupt_reason="user_paused")
                add_event(
                    sess,
                    "session.status_changed",
                    session_event_payload(sess, status="paused", message="用户暂停了会话。"),
                    source="workflow",
                )

                sess.roles["planner"].lane_status = "busy"
                add_event(
                    sess,
                    "lane.status_changed",
                    {"lane": sess.roles["planner"].to_wire(), "status": "busy", "message": "planner 通道忙碌中"},
                    role_key="planner",
                    source="workflow",
                )

                publish_artifact(
                    sess,
                    role_key="planner",
                    round_no=1,
                    phase="planning",
                    artifact_kind="plan",
                    content="# bundled plan",
                )
                add_intervention(
                    sess,
                    origin_view="scene",
                    origin_role_key=None,
                    target_roles=("planner", "reviewer"),
                    target_scope="planning",
                    text="补上原子写路径说明",
                )

                bundle = store.get_session_bundle("sess_bundle")
                self.assertEqual(bundle["session"]["status"], "paused")
                lane_row = next(item for item in bundle["lanes"] if item["role_key"] == "planner")
                self.assertEqual(lane_row["lane_status"], "busy")
                self.assertEqual(len(bundle["events"]), 4)
                self.assertEqual(len(bundle["artifacts"]), 1)
                self.assertEqual(bundle["artifacts"][0]["content"], "# bundled plan")
                self.assertEqual(len(bundle["interventions"]), 1)
                self.assertEqual(bundle["interventions"][0]["text"], "补上原子写路径说明")
            finally:
                set_persist_hook(previous_hook)
                store.close()


class EngineContractTests(unittest.TestCase):
    def test_negotiation_publishes_plan_review_and_consensus_snapshot(self):
        sess = SessionState("sess3", "task", "/tmp", default_workflow_config())
        role_adapters = {"reviewer": FakeAdapter()}

        def call_role(role_key, prompt, cwd, sess_obj, **kwargs):
            if role_key == "planner":
                return "# Plan\n统一账本"
            if role_key == "reviewer":
                return "APPROVED\n方案可以执行"
            raise AssertionError(f"unexpected role: {role_key}")

        run_negotiation(
            sess,
            start_round=1,
            call_role=call_role,
            role_adapters=role_adapters,
            build_planner_first_prompt=lambda task, cwd: f"plan::{task}",
            build_planner_revise_prompt=lambda feedback, interventions, cwd: "revise",
            build_reviewer_first_prompt=lambda task, plan: f"review::{plan}",
            build_reviewer_review_prompt=lambda plan, interventions: f"review::{plan}",
        )

        kinds = [artifact["artifact_kind"] for artifact in sess.artifacts]
        self.assertEqual(kinds, ["plan", "review", "consensus_snapshot"])
        self.assertEqual(sess.status, "consensus")
        self.assertEqual(sess.active_stage, "awaiting_execution")
        self.assertEqual(sess.consensus_round, 1)
        self.assertIn("lane.status_changed", [event["type"] for event in sess.stream_events])
        status_events = [event for event in sess.stream_events if event["type"] == "session.status_changed"]
        self.assertTrue(status_events)
        self.assertEqual(status_events[-1]["data"]["session"]["status"], "consensus")
        self.assertEqual(status_events[-1]["data"]["summary"]["session_id"], "sess3")

    def test_execution_and_validation_publish_artifacts_until_done(self):
        sess = SessionState("sess4", "task", "/tmp", default_workflow_config())
        publish_artifact(
            sess,
            role_key="planner",
            round_no=1,
            phase="planning",
            artifact_kind="plan",
            content="# final plan",
        )
        sess.consensus_round = 1

        role_adapters = {"validator": FakeAdapter()}

        def call_role(role_key, prompt, cwd, sess_obj, **kwargs):
            if role_key == "executor":
                return "已完成执行"
            if role_key == "validator":
                return "任务收口成功：验证通过"
            raise AssertionError(f"unexpected role: {role_key}")

        run_execution(
            sess,
            call_role=call_role,
            role_adapters=role_adapters,
            _is_git_repo=lambda cwd: False,
            capture_baseline_ref=lambda cwd: None,
            capture_baseline_untracked=lambda cwd: set(),
            build_execution_prompt=lambda task, final_plan, approved=True: f"exec::{task}::{final_plan}",
            build_post_review_prompt=lambda sess_obj, task, approved_plan, execution_result: f"validate::{execution_result}",
        )

        kinds = [artifact["artifact_kind"] for artifact in sess.artifacts]
        self.assertIn("execution_summary", kinds)
        self.assertIn("validation_report", kinds)
        self.assertEqual(sess.status, "done")
        self.assertEqual(sess.active_stage, "done")


class InputContractTests(unittest.TestCase):
    def test_plain_text_creates_intervention_for_current_stage(self):
        bridge_mod = load_bridge_module()
        sess = SessionState("sess5", "task", "/tmp", default_workflow_config())

        result = bridge_mod.handle_input(
            sess,
            {
                "origin_view": "scene",
                "role_key": None,
                "text": "请补上恢复和 cursor 说明",
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "intervention")
        self.assertEqual(len(sess.interventions), 1)
        self.assertEqual(sess.interventions[0]["target_roles"], ["planner", "reviewer"])

    def test_consensus_rejects_plain_text(self):
        bridge_mod = load_bridge_module()
        sess = SessionState("sess6", "task", "/tmp", default_workflow_config())
        sess.status = "consensus"

        result = bridge_mod.handle_input(sess, {"origin_view": "scene", "text": "再加一条普通意见"})

        self.assertFalse(result["ok"])
        self.assertIn("共识状态", result["error"])

    def test_slash_command_is_logged_and_applied(self):
        bridge_mod = load_bridge_module()
        sess = SessionState("sess7", "task", "/tmp", default_workflow_config())

        result = bridge_mod.handle_input(
            sess,
            {
                "origin_view": "terminal",
                "role_key": "planner",
                "text": "/pause",
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(sess.status, "paused")
        self.assertEqual(sess.interventions[-1]["command"], "pause")
        self.assertEqual(sess.interventions[-1]["status"], "acknowledged")

    def test_session_view_mode_switch_is_session_level(self):
        bridge_mod = load_bridge_module()
        sess = SessionState("sess8", "task", "/tmp", default_workflow_config())

        error = bridge_mod.set_session_view_mode(sess, "terminal")

        self.assertIsNone(error)
        self.assertEqual(sess.view_mode, "terminal")
        self.assertEqual(sess.workflow_config["view_mode"], "terminal")
        self.assertEqual(sess.stream_events[-1]["type"], "session.view_mode_changed")

    def test_terminal_resize_updates_lane_viewport_once(self):
        bridge_mod = load_bridge_module()
        sess = SessionState("sess_resize", "task", "/tmp", default_workflow_config())

        result = bridge_mod.resize_terminal_viewport(
            sess,
            {
                "role_key": "planner",
                "width_px": 960,
                "height_px": 480,
                "cols": 120,
                "rows": 30,
            },
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(sess.roles["planner"].viewport["cols"], 120)
        self.assertEqual(sess.stream_events[-1]["type"], "lane.viewport_changed")
        self.assertNotIn("viewport", sess.stream_events[-1]["data"])
        self.assertEqual(sess.stream_events[-1]["data"]["lane"]["viewport"]["cols"], 120)

        repeat = bridge_mod.resize_terminal_viewport(
            sess,
            {
                "role_key": "planner",
                "width_px": 960,
                "height_px": 480,
                "cols": 120,
                "rows": 30,
            },
        )

        self.assertTrue(repeat["ok"])
        self.assertFalse(repeat["changed"])
        self.assertEqual(len([event for event in sess.stream_events if event["type"] == "lane.viewport_changed"]), 1)

    def test_history_payload_restores_process_from_ledger_without_replaying_stream(self):
        bridge_mod = load_bridge_module()
        sess = SessionState("sess9", "task", "/tmp", default_workflow_config())
        add_event(sess, "session.status_changed", {"status": "running", "message": "协商开始。"})
        add_event(sess, "lane.stdout_chunk", {"text": "hello"}, role_key="planner", source="claude-code")
        publish_artifact(
            sess,
            role_key="planner",
            round_no=1,
            phase="planning",
            artifact_kind="plan",
            content="# plan",
        )

        payload = bridge_mod.history_payload(sess)

        self.assertEqual(payload["stream_cursor"], len(payload["events"]))
        self.assertEqual(payload["events"][1]["data"]["text"], "hello")
        self.assertNotIn("projection", payload["events"][1]["data"])
        self.assertIn("hello", payload["projections"]["terminal"]["planner"])
        self.assertTrue(any(item["type"] == "artifact" for item in payload["projections"]["scene"]))
        artifact_items = [item for item in payload["projections"]["scene"] if item["type"] == "artifact"]
        self.assertEqual(len(artifact_items), 1)


class AdapterExtractResultTests(unittest.TestCase):
    """adapter 层 artifact 提取语义测试。"""

    def test_codex_adapter_uses_result_text(self):
        from bridge.adapters.codex_adapter import CodexAdapter
        adapter = CodexAdapter()
        result = adapter.extract_result(["stream stuff"], "final answer")
        self.assertEqual(result, "final answer")


class ClaudeAdapterPlanFileTests(unittest.TestCase):
    """Claude adapter: session UUID → JSONL slug → plan file 确定性映射。"""

    def test_extract_slug_from_jsonl(self):
        """从 session JSONL 提取 slug。"""
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "test-session.jsonl"
            import json as _json
            jsonl.write_text(
                _json.dumps({"type": "user", "sessionId": "abc-123", "slug": "happy-dancing-cat"}) + "\n"
                + _json.dumps({"type": "assistant", "sessionId": "abc-123", "slug": "happy-dancing-cat"}) + "\n",
                encoding="utf-8",
            )
            slug = ClaudeCodeAdapter._extract_slug(jsonl)
            self.assertEqual(slug, "happy-dancing-cat")

    def test_extract_slug_returns_none_for_empty(self):
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "empty.jsonl"
            jsonl.write_text("{}\n", encoding="utf-8")
            self.assertIsNone(ClaudeCodeAdapter._extract_slug(jsonl))

    def test_read_plan_file_full_chain(self):
        """session UUID → projects/{hash}/{uuid}.jsonl → slug → plans/{slug}.md"""
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        import json as _json

        with tempfile.TemporaryDirectory() as fake_home:
            # 构造 .claude/projects/fake-project/{uuid}.jsonl
            session_uuid = "deadbeef-1234-5678-9abc-def012345678"
            project_dir = Path(fake_home) / ".claude" / "projects" / "fake-project"
            project_dir.mkdir(parents=True)
            jsonl = project_dir / f"{session_uuid}.jsonl"
            jsonl.write_text(
                _json.dumps({"slug": "jolly-jumping-fox", "sessionId": session_uuid}) + "\n",
                encoding="utf-8",
            )

            # 构造 .claude/plans/jolly-jumping-fox.md
            plans_dir = Path(fake_home) / ".claude" / "plans"
            plans_dir.mkdir(parents=True)
            plan_file = plans_dir / "jolly-jumping-fox.md"
            plan_file.write_text("# 完整计划\n\n## 根因分析\n数据流断裂", encoding="utf-8")

            # monkey-patch Path.home 指向 fake_home
            original_home = Path.home
            Path.home = staticmethod(lambda: Path(fake_home))
            try:
                content = ClaudeCodeAdapter._read_plan_file(session_uuid)
                self.assertIsNotNone(content)
                self.assertIn("# 完整计划", content)
                self.assertIn("## 根因分析", content)
            finally:
                Path.home = original_home

    def test_read_plan_file_returns_none_when_no_jsonl(self):
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        with tempfile.TemporaryDirectory() as fake_home:
            original_home = Path.home
            Path.home = staticmethod(lambda: Path(fake_home))
            try:
                self.assertIsNone(ClaudeCodeAdapter._read_plan_file("nonexistent-uuid"))
            finally:
                Path.home = original_home

    def test_run_plan_mode_returns_plan_file_content(self):
        """plan mode: run() 返回 plan 文件内容，不返回 stdout 摘要。"""
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        import json as _json

        with tempfile.TemporaryDirectory() as fake_home:
            session_uuid = "plan-test-uuid-1234"
            project_dir = Path(fake_home) / ".claude" / "projects" / "test-proj"
            project_dir.mkdir(parents=True)
            (project_dir / f"{session_uuid}.jsonl").write_text(
                _json.dumps({"slug": "test-plan-slug", "sessionId": session_uuid}) + "\n",
                encoding="utf-8",
            )
            plans_dir = Path(fake_home) / ".claude" / "plans"
            plans_dir.mkdir(parents=True)
            (plans_dir / "test-plan-slug.md").write_text("# 真正的计划文档", encoding="utf-8")

            class StubClaude(ClaudeCodeAdapter):
                """跳过真实 CLI，只模拟 super().run() 返回 stdout 摘要。"""
                _base_run_called = False

                def run(self, prompt, cwd, sess, log_tag=None, agent_label=None, **kwargs):
                    bypass_permissions = kwargs.get("bypass_permissions", False)
                    is_plan_mode = not bypass_permissions
                    claude_session_id = kwargs.get("session_id", "")
                    # 模拟 super().run() 返回口述摘要
                    stdout_result = "我分析了代码并制定了方案。"
                    StubClaude._base_run_called = True

                    if is_plan_mode and claude_session_id:
                        plan_content = ClaudeCodeAdapter._read_plan_file(claude_session_id)
                        if plan_content:
                            return plan_content
                    return stdout_result

            wf = default_workflow_config()
            sess = SessionState("plan-test", "task", fake_home, wf)

            original_home = Path.home
            Path.home = staticmethod(lambda: Path(fake_home))
            try:
                adapter = StubClaude()
                result = adapter.run("task", fake_home, sess, session_id=session_uuid, bypass_permissions=False)
                self.assertTrue(StubClaude._base_run_called)
                self.assertEqual(result, "# 真正的计划文档")
            finally:
                Path.home = original_home

    def test_run_execution_mode_ignores_plan_file(self):
        """execution mode (bypass_permissions=True) 不读 plan 文件，返回 stdout。"""
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        import json as _json

        with tempfile.TemporaryDirectory() as fake_home:
            session_uuid = "exec-test-uuid-5678"
            project_dir = Path(fake_home) / ".claude" / "projects" / "test-proj"
            project_dir.mkdir(parents=True)
            (project_dir / f"{session_uuid}.jsonl").write_text(
                _json.dumps({"slug": "exec-slug", "sessionId": session_uuid}) + "\n",
                encoding="utf-8",
            )
            plans_dir = Path(fake_home) / ".claude" / "plans"
            plans_dir.mkdir(parents=True)
            (plans_dir / "exec-slug.md").write_text("# 不应该被读到的 plan", encoding="utf-8")

            class StubClaude(ClaudeCodeAdapter):
                def run(self, prompt, cwd, sess, log_tag=None, agent_label=None, **kwargs):
                    bypass_permissions = kwargs.get("bypass_permissions", False)
                    is_plan_mode = not bypass_permissions
                    claude_session_id = kwargs.get("session_id", "")
                    stdout_result = "执行完成，修改了3个文件。"

                    if is_plan_mode and claude_session_id:
                        plan_content = ClaudeCodeAdapter._read_plan_file(claude_session_id)
                        if plan_content:
                            return plan_content
                    return stdout_result

            wf = default_workflow_config()
            sess = SessionState("exec-test", "task", fake_home, wf)

            original_home = Path.home
            Path.home = staticmethod(lambda: Path(fake_home))
            try:
                adapter = StubClaude()
                result = adapter.run("task", fake_home, sess, session_id=session_uuid, bypass_permissions=True)
                self.assertEqual(result, "执行完成，修改了3个文件。")
            finally:
                Path.home = original_home


if __name__ == "__main__":
    unittest.main()
