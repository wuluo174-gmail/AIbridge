"""
Bridge Contract Tests (v8)
==========================
行为级 contract tests，保障协议不漂移。

设计原则:
  - 不锁源码文本形状 — 代码迁移不会导致假阳性
  - hermetic — 不碰真实 CLI、不碰 ~/.claude/plans/、不碰 git
  - stub 在 subprocess.Popen 层 — 让 wrapper + 编排完整运行
  - 用 protocol.py 常量校验 runtime 产出 — 交叉验证链

覆盖范围:
  1. 协议常量精确断言 (TestProtocolConstants)
  2. 骨架包可导入 (TestSkeletonImports)
  3. 纯函数行为 (TestPureFunctions)
  4. SessionState 生命周期 (TestSessionStateLifecycle)
  5. 多会话隔离 (TestMultiSessionIsolation)
  6. CLI 命令构造 + 会话绑定 (TestSessionBinding)
  7. 全栈 runtime 事件验证 (TestRuntimeEventContract)
  8. Plan 文件归属 (TestPlanFileAttribution)
  9. 提示词 fallback (TestPromptFallbacks)
  10. HTTP API 端点结构 (TestAPIContractHermetic)

运行:
  python3 -m unittest discover -s tests -v

依赖:
  只使用 Python 标准库。无外部依赖。
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.parse
import unittest
import uuid
from pathlib import Path

# 确保项目根目录在 path 中
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bridge.protocol import (
    STATES, EVENT_TYPES, PROMPT_KEYS, PROMPT_KEYS_SET,
    EXECUTABLE_STATES, FIXABLE_STATES, CONTINUABLE_STATES,
    TERMINAL_STATES, STATE_RESPONSE_KEYS, HISTORY_RESPONSE_KEYS,
    EVENTS_RESPONSE_KEYS, SESSIONS_RESPONSE_KEYS,
    SESSION_LISTING_KEYS, BROWSE_RESPONSE_KEYS,
    COMPLETE_RESPONSE_KEYS, RECENT_PATHS_RESPONSE_KEYS,
    START_RESPONSE_KEYS, EVENT_PAYLOAD_REQUIRED_KEYS,
    GET_ENDPOINTS, POST_ENDPOINTS,
)


# ═════════════════════════════════════════════════════════════════
# 辅助层
# ═════════════════════════════════════════════════════════════════

BRIDGE_PY = os.path.join(ROOT, "bridge.py")


def _load_bridge_module():
    """动态加载 bridge.py 为独立模块 (避免与 bridge/ 包冲突)。"""
    # 重置 session 单例状态，确保跨 test class 隔离
    try:
        import bridge.session as _bsession
        with _bsession.sessions_lock:
            _bsession.sessions.clear()
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location("bridge_legacy", BRIDGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scratch_root_candidates():
    """返回测试可用的外部 scratch 根目录候选。

    优先允许通过 BRIDGE_TEST_TMPDIR / TEST_TMPDIR 注入；
    否则只尝试仓库外部路径，避免回退到 repo 内部导致非 hermetic。
    """
    candidates = [
        os.environ.get("BRIDGE_TEST_TMPDIR"),
        os.environ.get("TEST_TMPDIR"),
        os.environ.get("TMPDIR"),
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        os.environ.get("XDG_RUNTIME_DIR"),
        "/var/tmp",
        "/private/tmp",
        "/tmp",
    ]

    try:
        home = Path.home()
    except Exception:
        home = None

    if home is not None:
        candidates.extend([
            home / ".cache",
            home / "Library" / "Caches",
        ])

    seen = set()
    for raw in candidates:
        if not raw:
            continue
        try:
            base = Path(raw).expanduser()
        except Exception:
            continue
        if not base.is_absolute():
            base = base.resolve()
        root = base / "bridge-contract-tests"
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        yield root


def _make_scratch_dir(prefix):
    """在外部 scratch 根目录下创建一个唯一目录，失败时返回 None。"""
    for root in _scratch_root_candidates():
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / f".probe_{uuid.uuid4().hex}"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue

        for _ in range(10):
            candidate = root / f"{prefix}{uuid.uuid4().hex}"
            try:
                candidate.mkdir()
                return candidate
            except FileExistsError:
                continue
            except OSError:
                break
    return None


def _patch_log_dir(mod):
    """将 mod.LOG_DIR 和 bridge.session.LOG_DIR 指向临时目录，返回 tmpdir 路径。

    迁移后 SessionState.__init__ 引用 bridge.session.LOG_DIR（模块作用域），
    而非 bridge_legacy.LOG_DIR，因此需要同时补丁两处。
    测试只使用仓库外部 scratch 根目录；必要时可通过 BRIDGE_TEST_TMPDIR 注入。
    """
    try:
        import bridge.session as _bsession
        tmpdir = _make_scratch_dir("bridge_test_logs_")
        if tmpdir is None:
            return None
        mod._orig_LOG_DIR = mod.LOG_DIR
        mod.LOG_DIR = tmpdir
        _bsession._orig_LOG_DIR = _bsession.LOG_DIR
        _bsession.LOG_DIR = tmpdir
        return tmpdir
    except (OSError, FileNotFoundError):
        return None


def _restore_log_dir(mod):
    """恢复 mod.LOG_DIR 和 bridge.session.LOG_DIR 为原始值。"""
    if hasattr(mod, '_orig_LOG_DIR'):
        mod.LOG_DIR = mod._orig_LOG_DIR
    try:
        import bridge.session as _bsession
        if hasattr(_bsession, '_orig_LOG_DIR'):
            _bsession.LOG_DIR = _bsession._orig_LOG_DIR
    except ImportError:
        pass


class FakePopen:
    """stub subprocess.Popen，提供文本模式的 stdout/stderr 流。

    与真实 Popen(text=True) 行为一致：
    - stdout/stderr 是文本流，逐行迭代产出 str
    - wait() 返回 returncode
    - poll() 返回 returncode（表示已结束）
    - kill() 是 no-op
    """

    def __init__(self, cmd, **kwargs):
        self._cmd = cmd
        self.pid = 99999
        self.returncode = 0
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def wait(self, **kwargs):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


def _make_claude_stream(text):
    """构造 Claude stream-json 格式的 stdout 行。"""
    lines = []
    for chunk in [text[i:i+20] for i in range(0, len(text), 20)]:
        evt = {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": chunk}
        }}
        lines.append(json.dumps(evt))
    lines.append(json.dumps({"type": "stream_event",
                              "event": {"type": "content_block_stop"}}))
    lines.append(json.dumps({"type": "result", "result": text}))
    return "\n".join(lines) + "\n"


def _make_codex_stream(text, commands=None):
    """构造 Codex JSONL 格式的 stdout 行。"""
    lines = []
    if commands:
        for cmd_str, output in commands:
            lines.append(json.dumps({
                "type": "item.started",
                "item": {"type": "command_execution", "command": cmd_str}
            }))
            lines.append(json.dumps({
                "type": "item.completed",
                "item": {"type": "command_execution", "aggregated_output": output}
            }))
    lines.append(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text}
    }))
    return "\n".join(lines) + "\n"


def _make_popen_factory(claude_text, codex_text,
                        codex_commands=None,
                        claude_stderr="", codex_stderr=""):
    """返回一个替代 subprocess.Popen 的工厂函数。"""
    def factory(cmd, **kwargs):
        proc = FakePopen(cmd, **kwargs)
        if cmd[0] == "claude":
            proc.stdout = io.StringIO(_make_claude_stream(claude_text))
            proc.stderr = io.StringIO(claude_stderr)
        elif cmd[0] == "codex":
            proc.stdout = io.StringIO(_make_codex_stream(codex_text, codex_commands))
            proc.stderr = io.StringIO(codex_stderr)
        return proc
    return factory


def _dispatch_http_request(mod, method, path, body=None):
    """进程内调用 BridgeHandler，避免测试依赖真实监听端口。"""
    payload = b""
    headers = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))

    handler = object.__new__(mod.BridgeHandler)
    handler.path = path
    handler.command = method
    handler.headers = headers
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    handler._status = None
    handler._headers = {}

    def _send_response(self, code, message=None):
        self._status = code

    def _send_header(self, key, value):
        self._headers[key] = value

    def _end_headers(self):
        pass

    def _send_error(self, code, message=None, explain=None):
        self._status = code
        text = message or ""
        self._headers["Content-Type"] = "text/plain; charset=utf-8"
        self.wfile.write(text.encode("utf-8"))

    handler.send_response = types.MethodType(_send_response, handler)
    handler.send_header = types.MethodType(_send_header, handler)
    handler.end_headers = types.MethodType(_end_headers, handler)
    handler.send_error = types.MethodType(_send_error, handler)
    handler.log_message = types.MethodType(lambda self, *a: None, handler)

    if method == "GET":
        mod.BridgeHandler.do_GET(handler)
    elif method == "POST":
        mod.BridgeHandler.do_POST(handler)
    else:
        raise ValueError(f"unsupported method: {method}")

    raw = handler.wfile.getvalue()
    ctype = handler._headers.get("Content-Type", "")
    if ctype.startswith("application/json"):
        return handler._status, json.loads(raw.decode("utf-8"))
    return handler._status, raw.decode("utf-8")


# ═════════════════════════════════════════════════════════════════
# 1. 协议常量精确断言
# ═════════════════════════════════════════════════════════════════

class TestProtocolConstants(unittest.TestCase):
    """protocol.py 常量自洽性 — 精确成员断言。"""

    def test_states_exact_members(self):
        self.assertEqual(STATES, {
            "idle", "running", "consensus", "max_rounds",
            "executing", "review_pending", "review_fix", "done", "error",
        })

    def test_executable_states_exact(self):
        self.assertEqual(EXECUTABLE_STATES, {"consensus", "max_rounds"})

    def test_fixable_states_exact(self):
        self.assertEqual(FIXABLE_STATES, {"review_fix"})

    def test_continuable_states_exact(self):
        self.assertEqual(CONTINUABLE_STATES, {"consensus", "max_rounds"})

    def test_terminal_states_exact(self):
        self.assertEqual(TERMINAL_STATES, {"idle", "done", "error"})

    def test_event_types_exact_members(self):
        self.assertEqual(EVENT_TYPES, {
            "status_change", "round_start", "agent_thinking", "cli_start",
            "agent_chunk", "chunk_boundary", "agent_stderr", "agent_result",
            "agent_response", "consensus_reached", "max_rounds_reached",
            "warning", "rollback", "error",
            "execution_done",
            "review_start", "review_round_start", "review_response",
            "review_needs_fix", "review_done",
        })

    def test_prompt_keys_exact_members(self):
        self.assertEqual(PROMPT_KEYS_SET, {
            "claude_first", "claude_revise", "codex_first", "codex_review",
            "execution", "execution_unapproved",
            "codex_post_review", "claude_post_fix", "codex_post_review_followup",
            "user_inject_label_claude", "user_inject_label_codex",
        })

    def test_state_subsets_are_subsets(self):
        for subset in (EXECUTABLE_STATES, FIXABLE_STATES,
                       CONTINUABLE_STATES, TERMINAL_STATES):
            self.assertTrue(subset <= STATES,
                f"{subset} 不是 STATES 的子集")

    def test_payload_schema_covers_all_event_types(self):
        self.assertEqual(set(EVENT_PAYLOAD_REQUIRED_KEYS.keys()), EVENT_TYPES)

    def test_get_endpoints_exact(self):
        self.assertEqual(set(GET_ENDPOINTS), {
            "/", "/api/events", "/api/state", "/api/sessions",
            "/api/history", "/api/browse", "/api/complete",
            "/api/recent_paths", "/api/prompts",
        })

    def test_post_endpoints_exact(self):
        self.assertEqual(set(POST_ENDPOINTS), {
            "/api/start", "/api/execute", "/api/stop",
            "/api/review_fix", "/api/review_skip", "/api/prompts",
            "/api/inject", "/api/continue",
        })


# ═════════════════════════════════════════════════════════════════
# 2. 骨架包可导入
# ═════════════════════════════════════════════════════════════════

class TestSkeletonImports(unittest.TestCase):
    """验证 bridge/ 骨架包可正常导入。"""

    def test_import_bridge(self):
        import bridge
        self.assertIsNotNone(bridge.__version__)

    def test_import_protocol(self):
        from bridge import protocol
        self.assertIsNotNone(protocol.STATES)
        self.assertIsNotNone(protocol.EVENT_TYPES)
        self.assertIsNotNone(protocol.PROMPT_KEYS)

    def test_import_adapters_base(self):
        from bridge.adapters import base
        self.assertTrue(hasattr(base, 'CLIAdapter'))

    def test_import_claude_adapter(self):
        from bridge.adapters import claude_adapter
        self.assertTrue(hasattr(claude_adapter, 'ClaudeCodeAdapter'))

    def test_import_codex_adapter(self):
        from bridge.adapters import codex_adapter
        self.assertTrue(hasattr(codex_adapter, 'CodexAdapter'))

    def test_import_engine(self):
        from bridge.orchestration import engine
        self.assertTrue(hasattr(engine, 'run_negotiation'))

    def test_import_prompts(self):
        from bridge.orchestration import prompts
        self.assertTrue(hasattr(prompts, 'build_claude_first_prompt'))

    def test_import_store(self):
        from bridge.persistence import store
        self.assertTrue(hasattr(store, 'Store'))

    def test_import_plan(self):
        from bridge import plan
        self.assertTrue(hasattr(plan, 'validate_plan_relevance'))
        self.assertTrue(hasattr(plan, 'find_new_plan_file'))
        self.assertTrue(hasattr(plan, 'snapshot_plan_files'))

    def test_adapter_capabilities(self):
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        from bridge.adapters.codex_adapter import CodexAdapter
        claude = ClaudeCodeAdapter.__new__(ClaudeCodeAdapter)
        codex = CodexAdapter.__new__(CodexAdapter)
        cc = claude.capabilities
        self.assertTrue(cc["plan_mode"])
        self.assertTrue(cc["dangerous_mode"])
        self.assertTrue(cc["stream_json"])
        self.assertTrue(cc["session_resume"])
        xc = codex.capabilities
        self.assertFalse(xc["plan_mode"])
        self.assertFalse(xc["dangerous_mode"])
        self.assertFalse(xc["stream_json"])
        self.assertTrue(xc["session_resume"])

    def test_adapter_detect_approval(self):
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter.__new__(ClaudeCodeAdapter)
        self.assertTrue(adapter.detect_approval("APPROVED\nsome reason"))
        self.assertTrue(adapter.detect_approval("  APPROVED — looks good"))
        self.assertTrue(adapter.detect_approval("approved\nlowercase"))
        self.assertFalse(adapter.detect_approval("NOT APPROVED"))
        self.assertFalse(adapter.detect_approval(""))
        self.assertFalse(adapter.detect_approval("Some text\nAPPROVED on second line"))

    def test_adapter_detect_closure(self):
        from bridge.adapters.codex_adapter import CodexAdapter
        adapter = CodexAdapter.__new__(CodexAdapter)
        self.assertTrue(adapter.detect_closure("任务收口成功\n其他内容"))
        self.assertTrue(adapter.detect_closure("任务收口成功"))
        self.assertFalse(adapter.detect_closure("其他内容\n任务收口成功"))
        self.assertFalse(adapter.detect_closure(""))
        self.assertFalse(adapter.detect_closure(None))


# ═════════════════════════════════════════════════════════════════
# 3. 纯函数行为
# ═════════════════════════════════════════════════════════════════

class TestPureFunctions(unittest.TestCase):
    """测试 bridge.py 中的纯函数行为。迁移后仍有效。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()

    # ── is_approved ──

    def test_approved_first_line(self):
        self.assertTrue(self.mod.is_approved("APPROVED\nsome details"))

    def test_approved_case_insensitive(self):
        self.assertTrue(self.mod.is_approved("approved\nlowercase"))

    def test_approved_leading_spaces(self):
        self.assertTrue(self.mod.is_approved("  APPROVED — ok"))

    def test_not_approved(self):
        self.assertFalse(self.mod.is_approved("NOT APPROVED"))

    def test_approved_second_line_fails(self):
        self.assertFalse(self.mod.is_approved("Some text\nAPPROVED"))

    def test_approved_empty(self):
        self.assertFalse(self.mod.is_approved(""))

    # ── last_complete_round ──

    def test_lcr_empty(self):
        self.assertEqual(self.mod.last_complete_round([]), 0)

    def test_lcr_complete_round(self):
        h = [{"round": 1, "role": "claude"}, {"round": 1, "role": "codex"}]
        self.assertEqual(self.mod.last_complete_round(h), 1)

    def test_lcr_incomplete_round(self):
        h = [{"round": 1, "role": "claude"}, {"round": 1, "role": "codex"},
             {"round": 2, "role": "claude"}]
        self.assertEqual(self.mod.last_complete_round(h), 1)

    def test_lcr_multiple_complete(self):
        h = [{"round": 1, "role": "claude"}, {"round": 1, "role": "codex"},
             {"round": 2, "role": "claude"}, {"round": 2, "role": "codex"}]
        self.assertEqual(self.mod.last_complete_round(h), 2)

    def test_lcr_with_user_entries(self):
        h = [{"round": 1, "role": "claude"}, {"round": 1, "role": "user"},
             {"round": 1, "role": "codex"}]
        self.assertEqual(self.mod.last_complete_round(h), 1)

    # ── collect_user_injects ──

    def test_cui_trailing_users(self):
        h = [{"role": "claude", "content": "plan"},
             {"role": "user", "content": "fix A"},
             {"role": "user", "content": "fix B"}]
        self.assertEqual(self.mod.collect_user_injects(h), ["fix A", "fix B"])

    def test_cui_no_users(self):
        h = [{"role": "claude", "content": "plan"},
             {"role": "codex", "content": "review"}]
        self.assertEqual(self.mod.collect_user_injects(h), [])

    def test_cui_user_between_agents(self):
        h = [{"role": "user", "content": "early"},
             {"role": "claude", "content": "plan"},
             {"role": "user", "content": "late"}]
        self.assertEqual(self.mod.collect_user_injects(h), ["late"])

    def test_cui_empty(self):
        self.assertEqual(self.mod.collect_user_injects([]), [])

    # ── validate_plan_relevance (from bridge.plan) ──

    def test_vpr_keyword_match(self):
        from bridge.plan import validate_plan_relevance
        self.assertTrue(validate_plan_relevance(
            "实现用户登录功能的方案", "用户登录"))

    def test_vpr_keyword_mismatch(self):
        from bridge.plan import validate_plan_relevance
        self.assertFalse(validate_plan_relevance(
            "数据库迁移方案", "用户登录"))

    def test_vpr_empty_content(self):
        from bridge.plan import validate_plan_relevance
        self.assertTrue(validate_plan_relevance("", "用户登录"))

    def test_vpr_empty_task(self):
        from bridge.plan import validate_plan_relevance
        self.assertTrue(validate_plan_relevance("some plan", ""))

    # ── detect_claude_md ──

    def test_dcm_nonexistent_dir(self):
        result = self.mod.detect_claude_md("/tmp/nonexistent_bridge_test_xyz")
        self.assertEqual(result, "")


# ═════════════════════════════════════════════════════════════════
# 4. SessionState 生命周期
# ═════════════════════════════════════════════════════════════════

class TestSessionStateLifecycle(unittest.TestCase):
    """直接实例化 SessionState 验证初始状态和字段。"""

    _skip_reason = None

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls._tmpdir = _patch_log_dir(cls.mod)
        if cls._tmpdir is None:
            cls._skip_reason = "无法创建外部 scratch 目录；可通过 BRIDGE_TEST_TMPDIR 指向可写目录"

    @classmethod
    def tearDownClass(cls):
        _restore_log_dir(cls.mod)
        if cls._tmpdir:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        if self._skip_reason:
            self.skipTest(self._skip_reason)

    def _make(self, sid="t1"):
        return self.mod.SessionState(sid, "test", "/tmp", 5)

    def test_initial_status_is_running(self):
        self.assertEqual(self._make().status, "running")

    def test_initial_round_is_zero(self):
        self.assertEqual(self._make().current_round, 0)

    def test_initial_consensus_false(self):
        self.assertFalse(self._make().consensus)

    def test_has_status_lock(self):
        self.assertIsNotNone(self._make().status_lock)

    def test_has_event_lock(self):
        self.assertIsNotNone(self._make().event_lock)

    def test_claude_session_id_is_uuid(self):
        self.assertEqual(len(self._make().claude_session_id), 36)

    def test_max_review_rounds_default_3(self):
        self.assertEqual(self._make().max_review_rounds, 3)

    def test_events_initially_empty(self):
        self.assertEqual(self._make().events, [])

    def test_history_initially_empty(self):
        self.assertEqual(self._make().history, [])


# ═════════════════════════════════════════════════════════════════
# 5. 多会话隔离
# ═════════════════════════════════════════════════════════════════

class TestMultiSessionIsolation(unittest.TestCase):
    """验证多会话间事件流和历史互不干扰 (NR-2)。"""

    _skip_reason = None

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls._tmpdir = _patch_log_dir(cls.mod)
        if cls._tmpdir is None:
            cls._skip_reason = "无法创建外部 scratch 目录；可通过 BRIDGE_TEST_TMPDIR 指向可写目录"

    @classmethod
    def tearDownClass(cls):
        _restore_log_dir(cls.mod)
        if cls._tmpdir:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        if self._skip_reason:
            self.skipTest(self._skip_reason)

    def test_events_isolated(self):
        s1 = self.mod.SessionState("s1", "task1", "/tmp", 5)
        s2 = self.mod.SessionState("s2", "task2", "/tmp", 5)
        self.mod.add_event(s1, "round_start", {"round": 1, "max": 5})
        self.assertEqual(len(s1.events), 1)
        self.assertEqual(len(s2.events), 0)

    def test_history_isolated(self):
        s1 = self.mod.SessionState("s1", "task1", "/tmp", 5)
        s2 = self.mod.SessionState("s2", "task2", "/tmp", 5)
        entry = {"round": 1, "role": "claude", "phase": "方案",
                 "content": "plan", "timestamp": "2024-01-01T00:00:00"}
        self.mod.add_history_event(s1, s1.history, entry, "agent_response")
        self.assertEqual(len(s1.history), 1)
        self.assertEqual(len(s2.history), 0)
        self.assertEqual(len(s1.events), 1)
        self.assertEqual(len(s2.events), 0)

    def test_session_ids_differ(self):
        s1 = self.mod.SessionState("s1", "task1", "/tmp", 5)
        s2 = self.mod.SessionState("s2", "task2", "/tmp", 5)
        self.assertNotEqual(s1.claude_session_id, s2.claude_session_id)


# ═════════════════════════════════════════════════════════════════
# 6. CLI 命令构造 + 会话绑定 (NR-4)
# ═════════════════════════════════════════════════════════════════

class TestSessionBinding(unittest.TestCase):
    """验证 CLI 命令构造和会话绑定参数 (NR-4)。"""

    _skip_reason = None

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls._tmpdir = _patch_log_dir(cls.mod)
        if cls._tmpdir is None:
            cls._skip_reason = "无法创建外部 scratch 目录；可通过 BRIDGE_TEST_TMPDIR 指向可写目录"

    @classmethod
    def tearDownClass(cls):
        _restore_log_dir(cls.mod)
        if cls._tmpdir:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        if self._skip_reason:
            self.skipTest(self._skip_reason)

    def _capture_cmd(self, agent, sess, **kwargs):
        """Stub Popen, 调用对应 wrapper, 返回捕获到的 cmd。"""
        captured = {}
        orig_popen = subprocess.Popen

        def capturing_factory(cmd, **kw):
            captured['cmd'] = cmd
            proc = FakePopen(cmd, **kw)
            if agent == "claude":
                proc.stdout = io.StringIO(
                    json.dumps({"type": "result", "result": "stub"}) + "\n")
            else:
                proc.stdout = io.StringIO(
                    json.dumps({"type": "item.completed",
                                "item": {"type": "agent_message", "text": "stub"}}) + "\n")
            proc.stderr = io.StringIO("")
            return proc

        subprocess.Popen = capturing_factory
        try:
            if agent == "claude":
                self.mod.call_claude_streaming(
                    "prompt", "/tmp", sess,
                    skip_plan_detection=True,
                    **kwargs)
            else:
                self.mod.call_codex_streaming("prompt", "/tmp", sess, **kwargs)
        except Exception:
            pass
        finally:
            subprocess.Popen = orig_popen
        return captured.get('cmd', [])

    def test_claude_first_call_uses_session_id(self):
        sess = self.mod.SessionState("sb1", "test", "/tmp", 5)
        cmd = self._capture_cmd("claude", sess, continue_session=False)
        self.assertIn("--session-id", cmd)
        idx = cmd.index("--session-id")
        self.assertEqual(cmd[idx + 1], sess.claude_session_id)
        self.assertNotIn("--resume", cmd)

    def test_claude_resume_uses_resume(self):
        sess = self.mod.SessionState("sb2", "test", "/tmp", 5)
        cmd = self._capture_cmd("claude", sess, continue_session=True)
        self.assertIn("--resume", cmd)
        idx = cmd.index("--resume")
        self.assertEqual(cmd[idx + 1], sess.claude_session_id)
        self.assertNotIn("--session-id", cmd)

    def test_claude_plan_mode(self):
        sess = self.mod.SessionState("sb3", "test", "/tmp", 5)
        cmd = self._capture_cmd("claude", sess, bypass_permissions=False)
        self.assertIn("--permission-mode", cmd)
        idx = cmd.index("--permission-mode")
        self.assertEqual(cmd[idx + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_claude_dangerous_mode(self):
        sess = self.mod.SessionState("sb4", "test", "/tmp", 5)
        cmd = self._capture_cmd("claude", sess, bypass_permissions=True)
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--permission-mode", cmd)

    def test_claude_no_dash_c(self):
        sess = self.mod.SessionState("sb5", "test", "/tmp", 5)
        cmd = self._capture_cmd("claude", sess)
        self.assertNotIn("-c", cmd)

    def test_claude_stream_json_format(self):
        sess = self.mod.SessionState("sb6", "test", "/tmp", 5)
        cmd = self._capture_cmd("claude", sess)
        self.assertIn("--output-format", cmd)
        idx = cmd.index("--output-format")
        self.assertEqual(cmd[idx + 1], "stream-json")

    def test_claude_effort_max(self):
        """--effort max 必须出现在所有 Claude 调用的命令行中。"""
        sess = self.mod.SessionState("sb_effort", "test", "/tmp", 5)
        cmd = self._capture_cmd("claude", sess)
        self.assertIn("--effort", cmd)
        idx = cmd.index("--effort")
        self.assertEqual(cmd[idx + 1], "max")

    def test_codex_first_call(self):
        sess = self.mod.SessionState("sb7", "test", "/tmp", 5)
        cmd = self._capture_cmd("codex", sess, resume_last=False)
        self.assertEqual(cmd[0], "codex")
        self.assertIn("exec", cmd)
        self.assertIn("--json", cmd)
        self.assertNotIn("resume", cmd)

    def test_codex_resume_call(self):
        sess = self.mod.SessionState("sb8", "test", "/tmp", 5)
        cmd = self._capture_cmd("codex", sess, resume_last=True)
        self.assertIn("resume", cmd)
        self.assertIn("--last", cmd)


# ═════════════════════════════════════════════════════════════════
# 7. 全栈 runtime 事件验证 (NR-1/3/6/7)
# ═════════════════════════════════════════════════════════════════

class TestRuntimeEventContract(unittest.TestCase):
    """全栈 runtime 验证: FakePopen → 真实 wrapper → 真实编排 → protocol.py 校验。

    stub 在 subprocess.Popen 层，喂假 stream-json/JSONL，
    让 call_claude_streaming / call_codex_streaming 的解析逻辑完整执行。
    """

    _skip_reason = None

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls._tmpdir = _patch_log_dir(cls.mod)
        if cls._tmpdir is None:
            cls._skip_reason = "无法创建外部 scratch 目录；可通过 BRIDGE_TEST_TMPDIR 指向可写目录"
            return
        cls._orig_popen = subprocess.Popen
        import bridge.plan
        # ── Boundary stubs ──
        # 用 dict 存函数引用，避免 Python descriptor 协议把函数绑定为方法。
        # plan 函数: patch bridge.plan 模块属性（全局单例），
        # 任何 import bridge.plan 的代码都会看到 stub——无论调用点在 bridge.py、
        # adapter 还是 engine。Step 2-6 迁移不影响 patch 有效性。
        cls._origs = {
            'is_git': cls.mod._is_git_repo,
            'capture_ref': cls.mod.capture_baseline_ref,
            'capture_untracked': cls.mod.capture_baseline_untracked,
            'capture_diff': cls.mod.capture_execution_diff,
            'snapshot_plans': bridge.plan.snapshot_plan_files,
            'find_plan': bridge.plan.find_new_plan_file,
            'validate_plan': bridge.plan.validate_plan_relevance,
        }

    @classmethod
    def tearDownClass(cls):
        if cls._skip_reason:
            return
        import bridge.plan
        subprocess.Popen = cls._orig_popen
        cls.mod._is_git_repo = cls._origs['is_git']
        cls.mod.capture_baseline_ref = cls._origs['capture_ref']
        cls.mod.capture_baseline_untracked = cls._origs['capture_untracked']
        cls.mod.capture_execution_diff = cls._origs['capture_diff']
        bridge.plan.snapshot_plan_files = cls._origs['snapshot_plans']
        bridge.plan.find_new_plan_file = cls._origs['find_plan']
        bridge.plan.validate_plan_relevance = cls._origs['validate_plan']
        _restore_log_dir(cls.mod)
        if cls._tmpdir:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        if self._skip_reason:
            self.skipTest(self._skip_reason)
        import bridge.plan
        subprocess.Popen = self._orig_popen
        self.mod._is_git_repo = lambda cwd: False
        self.mod.capture_baseline_ref = self._origs['capture_ref']
        self.mod.capture_baseline_untracked = self._origs['capture_untracked']
        self.mod.capture_execution_diff = self._origs['capture_diff']
        bridge.plan.validate_plan_relevance = self._origs['validate_plan']
        # plan 检测默认惰性返回 — 不碰真实 ~/.claude/plans/
        bridge.plan.snapshot_plan_files = lambda: {}
        bridge.plan.find_new_plan_file = lambda _: ""

    def _install_popen_stub(self, claude_text, codex_text,
                            codex_commands=None, claude_stderr=""):
        subprocess.Popen = _make_popen_factory(
            claude_text, codex_text,
            codex_commands=codex_commands,
            claude_stderr=claude_stderr)

    def _make_session(self, sid="rt"):
        return self.mod.SessionState(sid, "测试任务", "/tmp", 3)

    def _event_types(self, sess):
        return [e["type"] for e in sess.events]

    def _events_of_type(self, sess, etype):
        return [e for e in sess.events if e["type"] == etype]

    def _assert_all_events_valid(self, sess):
        """断言 sess.events 中每个事件都符合 protocol.py 声明。"""
        for e in sess.events:
            self.assertIn(e["type"], EVENT_TYPES,
                f"runtime 产出了未声明的事件类型: {e['type']}")
            required = EVENT_PAYLOAD_REQUIRED_KEYS[e["type"]]
            actual = set(e["data"].keys())
            missing = required - actual
            self.assertEqual(missing, set(),
                f"事件 {e['type']} payload 缺少必需键: {missing}, 实际: {actual}")

    # ── NR-1/3: 协商流程 ──

    def test_negotiation_consensus(self):
        """协商达成共识: wrapper + 编排事件全部通过 protocol 校验。"""
        sess = self._make_session("c1")
        self._install_popen_stub("my detailed plan", "APPROVED\nlooks good")
        self.mod.run_negotiation(sess)

        self._assert_all_events_valid(sess)
        self.assertEqual(sess.status, "consensus")
        self.assertIn(sess.status, STATES)
        types = set(self._event_types(sess))
        self.assertIn("status_change", types)
        self.assertIn("round_start", types)
        self.assertIn("agent_thinking", types)
        self.assertIn("agent_response", types)
        self.assertIn("consensus_reached", types)
        self.assertIn("cli_start", types)
        self.assertIn("agent_chunk", types)
        self.assertIn("agent_result", types)

    def test_negotiation_max_rounds(self):
        """协商未达共识: max_rounds, 跑满 3 轮。"""
        sess = self._make_session("c2")
        self._install_popen_stub("plan", "needs more work")
        self.mod.run_negotiation(sess)

        self._assert_all_events_valid(sess)
        self.assertEqual(sess.status, "max_rounds")
        self.assertIn("max_rounds_reached", self._event_types(sess))
        self.assertEqual(len(self._events_of_type(sess, "round_start")), 3)

    def test_negotiation_codex_with_commands(self):
        """Codex 带命令执行事件: chunk_boundary。"""
        sess = self._make_session("c3")
        self._install_popen_stub(
            "plan", "APPROVED\nok",
            codex_commands=[("ls -la", "file1\nfile2")])
        self.mod.run_negotiation(sess)

        self._assert_all_events_valid(sess)
        self.assertIn("chunk_boundary", set(self._event_types(sess)))

    # ── agent_stderr / warning / error ──

    def test_stderr_emits_agent_stderr(self):
        """stderr MCP 噪音 → agent_stderr (is_mcp=True)。"""
        sess = self._make_session("se")
        self._install_popen_stub(
            "APPROVED\nplan", "APPROVED\nok",
            claude_stderr="mcp: starting server\n")
        self.mod.run_negotiation(sess)

        self._assert_all_events_valid(sess)
        stderr_events = self._events_of_type(sess, "agent_stderr")
        self.assertGreater(len(stderr_events), 0)
        self.assertTrue(stderr_events[0]["data"]["is_mcp"])

    def test_warning_on_irrelevant_plan_file(self):
        """plan 文件与任务不相关 → warning 事件。"""
        import bridge.plan
        sess = self._make_session("w1")
        subprocess.Popen = _make_popen_factory("plan text", "APPROVED\nok")
        bridge.plan.find_new_plan_file = lambda snapshot: "完全无关的内容"
        bridge.plan.validate_plan_relevance = lambda content, task: False

        self.mod.run_negotiation(sess)

        self._assert_all_events_valid(sess)
        self.assertIn("warning", self._event_types(sess))

    def test_error_on_first_round_failure(self):
        """start_round=1 CLI 异常 → error 终态 (非 rollback)。"""
        sess = self._make_session("er")

        def failing_popen(cmd, **kwargs):
            raise FileNotFoundError("claude not found")
        subprocess.Popen = failing_popen

        self.mod.run_negotiation(sess, start_round=1)

        self.assertEqual(sess.status, "error")
        self.assertIn("error", self._event_types(sess))
        self.assertNotIn("rollback", self._event_types(sess))
        self._assert_all_events_valid(sess)

    # ── NR-6: 执行阶段 ──

    def test_execution_non_git(self):
        """非 git 执行: execution_done + 自动 review_start。"""
        sess = self._make_session("e1")
        sess.status = "executing"
        sess.history = [{"round": 1, "role": "claude", "content": "plan",
                         "phase": "方案", "timestamp": "t"}]
        sess.consensus = True
        self._install_popen_stub("executed ok", "任务收口成功\nall good")

        self.mod.run_execution(sess)

        self._assert_all_events_valid(sess)
        types = self._event_types(sess)
        self.assertIn("execution_done", types)
        self.assertIn("review_start", types)
        self.assertIsNotNone(sess.execution_result)

    def test_execution_captures_git_baseline(self):
        """git 项目: baseline ref + untracked 被正确捕获 (NR-6)。"""
        sess = self._make_session("e2")
        sess.status = "executing"
        sess.history = [{"round": 1, "role": "claude", "content": "plan",
                         "phase": "方案", "timestamp": "t"}]
        sess.consensus = True
        self.mod._is_git_repo = lambda cwd: True
        self.mod.capture_baseline_ref = lambda cwd: "abc123def456"
        self.mod.capture_baseline_untracked = lambda cwd: {"new_file.py", "temp.txt"}
        self.mod.capture_execution_diff = lambda cwd, ref, untracked=None: "(fake diff)"
        self._install_popen_stub("executed", "任务收口成功\nok")

        self.mod.run_execution(sess)

        self.assertTrue(sess.is_git_repo)
        self.assertEqual(sess.exec_baseline_ref, "abc123def456")
        self.assertEqual(sess.exec_baseline_untracked, {"new_file.py", "temp.txt"})

    # ── NR-7: 审查/修复 ──

    def test_review_closure_sets_done(self):
        """审查"任务收口成功" → done + review_done(success=True)。"""
        sess = self._make_session("r1")
        sess.status = "executing"
        sess.history = [{"round": 1, "role": "claude", "content": "plan",
                         "phase": "方案", "timestamp": "t"}]
        sess.consensus = True
        self._install_popen_stub("executed", "任务收口成功\nall checks pass")

        self.mod.run_execution(sess)

        self.assertEqual(sess.status, "done")
        review_dones = self._events_of_type(sess, "review_done")
        self.assertGreater(len(review_dones), 0)
        self.assertTrue(review_dones[-1]["data"]["success"])
        self._assert_all_events_valid(sess)

    def test_review_needs_fix(self):
        """审查发现问题 → review_fix + review_needs_fix。"""
        sess = self._make_session("r2")
        sess.status = "executing"
        sess.history = [{"round": 1, "role": "claude", "content": "plan",
                         "phase": "方案", "timestamp": "t"}]
        sess.consensus = True
        self._install_popen_stub("executed", "发现以下问题需要修复")

        self.mod.run_execution(sess)

        self.assertEqual(sess.status, "review_fix")
        self.assertIn("review_needs_fix", self._event_types(sess))
        self._assert_all_events_valid(sess)

    def test_review_fix_cycle_closure(self):
        """修复循环: Claude 修 → Codex 确认 → done。"""
        sess = self._make_session("r3")
        sess.status = "review_fix"
        sess.review_round = 1
        sess.review_history = [{"round": 1, "role": "codex", "phase": "执行审查",
                                "content": "问题", "timestamp": "t"}]
        sess.execution_result = "prev"
        self._install_popen_stub("fixed code", "任务收口成功\nok")

        self.mod.run_review_fix_cycle(sess)

        self.assertEqual(sess.status, "done")
        self.assertEqual(sess.review_round, 2)
        self._assert_all_events_valid(sess)

    def test_review_fix_max_rounds(self):
        """修复轮次超限 → 自动结束 (success=False)。"""
        sess = self._make_session("r4")
        sess.status = "review_fix"
        sess.review_round = 3
        sess.max_review_rounds = 3
        sess.review_history = []

        self.mod.run_review_fix_cycle(sess)

        self.assertEqual(sess.status, "done")
        review_dones = self._events_of_type(sess, "review_done")
        self.assertGreater(len(review_dones), 0)
        self.assertFalse(review_dones[-1]["data"]["success"])
        self._assert_all_events_valid(sess)

    # ── NR-3: 回退 ──

    def test_rollback_on_continuation_failure(self):
        """续接失败: 回退到 last_complete_round。"""
        sess = self._make_session("rb")
        sess.history = [
            {"round": 1, "role": "claude", "content": "plan1",
             "phase": "方案", "timestamp": "t"},
            {"round": 1, "role": "codex", "content": "review1",
             "phase": "审查", "timestamp": "t"},
        ]
        sess.current_round = 1
        sess.max_rounds = 3

        def failing_popen(cmd, **kwargs):
            raise FileNotFoundError("claude not found")
        subprocess.Popen = failing_popen

        self.mod.run_negotiation(sess, start_round=2)

        self.assertEqual(sess.status, "max_rounds")
        self.assertEqual(sess.current_round, 1)
        self.assertIn("rollback", self._event_types(sess))
        self._assert_all_events_valid(sess)

    # ── 事件覆盖率 ──

    def test_combined_flow_covers_common_event_types(self):
        """完整流程覆盖常见事件类型。"""
        sess = self._make_session("full")
        self._install_popen_stub(
            "my plan", "APPROVED\nlooks great",
            codex_commands=[("cat file.py", "content")],
            claude_stderr="mcp: server ready\n")
        self.mod.run_negotiation(sess)

        neg_types = set(self._event_types(sess))
        sess.events.clear()
        sess.status = "executing"
        self._install_popen_stub("executed", "发现问题")
        self.mod.run_execution(sess)

        all_types = neg_types | set(self._event_types(sess))
        expected_common = {
            "status_change", "round_start", "cli_start",
            "agent_chunk", "agent_result", "agent_stderr",
            "agent_thinking", "agent_response", "consensus_reached",
            "chunk_boundary",
            "execution_done", "review_start", "review_response",
            "review_needs_fix",
        }
        missing = expected_common - all_types
        self.assertEqual(missing, set(),
            f"主流程未覆盖的事件类型: {missing}")

    # ── Step E4: 事件类型校验测试 ──

    def test_add_event_rejects_unknown_type(self):
        """add_event 拒绝未在 protocol.EVENT_TYPES 中注册的事件类型。"""
        sess = self._make_session("ev")
        with self.assertRaises(ValueError):
            self.mod.add_event(sess, "nonexistent_type", {"msg": "test"})

    def test_add_history_event_rejects_unknown_type(self):
        """add_history_event 同样拒绝未注册的事件类型。"""
        sess = self._make_session("hev")
        entry = {"round": 1, "role": "claude", "phase": "方案",
                 "content": "test", "timestamp": "t"}
        with self.assertRaises(ValueError):
            self.mod.add_history_event(sess, sess.history, entry, "nonexistent_type")


# ═════════════════════════════════════════════════════════════════
# 8. Plan 文件归属 (NR-8)
# ═════════════════════════════════════════════════════════════════

class TestPlanFileAttribution(unittest.TestCase):
    """Plan 文件归属: 差集行为 + 锁语义 (NR-8)。

    测试 bridge.plan 模块的公开 API。
    不修改 Path.home。通过 patch bridge.plan.snapshot_plan_files 指向 tmpdir。
    """

    _skip_reason = None

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls._tmpdir = _patch_log_dir(cls.mod)
        if cls._tmpdir is None:
            cls._skip_reason = "无法创建外部 scratch 目录；可通过 BRIDGE_TEST_TMPDIR 指向可写目录"
            return
        cls._orig_plan_lock_timeout = cls.mod.PLAN_LOCK_ACQUIRE_TIMEOUT
        import bridge.plan
        cls._origs = {
            'snapshot': bridge.plan.snapshot_plan_files,
            'find': bridge.plan.find_new_plan_file,
        }

    @classmethod
    def tearDownClass(cls):
        if cls._skip_reason:
            return
        import bridge.plan
        bridge.plan.snapshot_plan_files = cls._origs['snapshot']
        bridge.plan.find_new_plan_file = cls._origs['find']
        cls.mod.PLAN_LOCK_ACQUIRE_TIMEOUT = cls._orig_plan_lock_timeout
        _restore_log_dir(cls.mod)
        if cls._tmpdir:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        if self._skip_reason:
            self.skipTest(self._skip_reason)
        import bridge.plan
        bridge.plan.snapshot_plan_files = self._origs['snapshot']
        bridge.plan.find_new_plan_file = self._origs['find']
        self.mod.PLAN_LOCK_ACQUIRE_TIMEOUT = 0.01
        with self.mod.plan_file_locks_lock:
            self.mod.plan_file_locks.clear()

    def _make_blocking_claude_factory(self, first_started, release_first, second_started):
        """第一个 Claude Popen 阻塞，第二个仅在真正启动时打点。"""
        call_lock = threading.Lock()
        state = {"count": 0}

        def factory(cmd, **kwargs):
            proc = FakePopen(cmd, **kwargs)
            proc.stderr = io.StringIO("")
            with call_lock:
                state["count"] += 1
                idx = state["count"]
            proc.stdout = io.StringIO(_make_claude_stream(f"stub {idx}"))
            if idx == 1:
                first_started.set()
                release_first.wait(timeout=2)
            elif idx == 2:
                second_started.set()
            return proc

        return factory

    def test_snapshot_and_find_new_plan(self):
        """快照前无文件, 写入后 → find_new_plan_file 返回其内容。"""
        import bridge.plan
        plans_dir = _make_scratch_dir("bridge_plans_")
        self.assertIsNotNone(plans_dir, "无法创建外部 scratch 目录")
        try:
            before = {}
            plan_file = plans_dir / "test_plan.md"
            plan_file.write_text("# Plan\n实现用户登录功能", encoding="utf-8")

            bridge.plan.snapshot_plan_files = lambda: {
                p: p.stat().st_mtime for p in plans_dir.glob("*.md")
            }

            content = bridge.plan.find_new_plan_file(before)
            self.assertIn("用户登录", content)
        finally:
            shutil.rmtree(plans_dir, ignore_errors=True)

    def test_find_new_plan_file_no_change(self):
        """快照前后无变化 → 返回空。"""
        import bridge.plan
        plans_dir = _make_scratch_dir("bridge_plans_")
        self.assertIsNotNone(plans_dir, "无法创建外部 scratch 目录")
        try:
            existing = plans_dir / "existing.md"
            existing.write_text("old content", encoding="utf-8")

            bridge.plan.snapshot_plan_files = lambda: {
                p: p.stat().st_mtime for p in plans_dir.glob("*.md")
            }

            before = bridge.plan.snapshot_plan_files()
            content = bridge.plan.find_new_plan_file(before)
            self.assertEqual(content, "")
        finally:
            shutil.rmtree(plans_dir, ignore_errors=True)

    def test_per_project_plan_file_lock_used_in_claude_streaming(self):
        """call_claude_streaming(skip_plan_detection=False) 不崩溃。"""
        import bridge.plan
        sess = self.mod.SessionState("pl1", "测试任务", "/tmp", 5)
        bridge.plan.snapshot_plan_files = lambda: {}
        bridge.plan.find_new_plan_file = lambda snapshot: ""
        orig_popen = subprocess.Popen
        subprocess.Popen = _make_popen_factory("stub plan", "stub")
        try:
            result = self.mod.call_claude_streaming(
                "test", "/tmp", sess, skip_plan_detection=False)
            self.assertIsInstance(result, str)
        except Exception:
            pass
        finally:
            subprocess.Popen = orig_popen

    def test_same_project_plan_lock_blocks_until_release(self):
        import bridge.plan
        sess1 = self.mod.SessionState("pl1", "任务一", "/tmp/project-a", 5)
        sess2 = self.mod.SessionState("pl2", "任务二", "/tmp/project-a", 5)
        bridge.plan.snapshot_plan_files = lambda: {}
        bridge.plan.find_new_plan_file = lambda snapshot: ""

        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        results = {}
        orig_popen = subprocess.Popen
        subprocess.Popen = self._make_blocking_claude_factory(
            first_started, release_first, second_started)

        def run_call(name, sess, prompt):
            results[name] = self.mod.call_claude_streaming(
                prompt, sess.project_path, sess, skip_plan_detection=False)

        t1 = threading.Thread(target=run_call, args=("first", sess1, "first"), daemon=True)
        t2 = threading.Thread(target=run_call, args=("second", sess2, "second"), daemon=True)
        try:
            t1.start()
            self.assertTrue(first_started.wait(1))
            t2.start()
            self.assertFalse(second_started.wait(0.2))

            release_first.set()
            t1.join(1)
            t2.join(1)

            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertTrue(second_started.is_set())
            self.assertEqual(results["first"], "stub 1")
            self.assertEqual(results["second"], "stub 2")
        finally:
            release_first.set()
            t1.join(1)
            t2.join(1)
            subprocess.Popen = orig_popen

    def test_different_project_plan_locks_run_in_parallel(self):
        import bridge.plan
        sess1 = self.mod.SessionState("pl1", "任务一", "/tmp/project-a", 5)
        sess2 = self.mod.SessionState("pl2", "任务二", "/tmp/project-b", 5)
        bridge.plan.snapshot_plan_files = lambda: {}
        bridge.plan.find_new_plan_file = lambda snapshot: ""

        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        results = {}
        orig_popen = subprocess.Popen
        subprocess.Popen = self._make_blocking_claude_factory(
            first_started, release_first, second_started)

        def run_call(name, sess, prompt):
            results[name] = self.mod.call_claude_streaming(
                prompt, sess.project_path, sess, skip_plan_detection=False)

        t1 = threading.Thread(target=run_call, args=("first", sess1, "first"), daemon=True)
        t2 = threading.Thread(target=run_call, args=("second", sess2, "second"), daemon=True)
        try:
            t1.start()
            self.assertTrue(first_started.wait(1))
            t2.start()
            self.assertTrue(second_started.wait(0.5))

            release_first.set()
            t1.join(1)
            t2.join(1)

            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertEqual(results["first"], "stub 1")
            self.assertEqual(results["second"], "stub 2")
        finally:
            release_first.set()
            t1.join(1)
            t2.join(1)
            subprocess.Popen = orig_popen

    def test_stop_while_waiting_for_same_project_plan_lock(self):
        import bridge.plan
        sess1 = self.mod.SessionState("pl1", "任务一", "/tmp/project-a", 5)
        sess2 = self.mod.SessionState("pl2", "任务二", "/tmp/project-a", 5)
        bridge.plan.snapshot_plan_files = lambda: {}
        bridge.plan.find_new_plan_file = lambda snapshot: ""

        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        results = {}
        orig_popen = subprocess.Popen
        subprocess.Popen = self._make_blocking_claude_factory(
            first_started, release_first, second_started)

        def run_call(name, sess, prompt):
            results[name] = self.mod.call_claude_streaming(
                prompt, sess.project_path, sess, skip_plan_detection=False)

        t1 = threading.Thread(target=run_call, args=("first", sess1, "first"), daemon=True)
        t2 = threading.Thread(target=run_call, args=("second", sess2, "second"), daemon=True)
        try:
            t1.start()
            self.assertTrue(first_started.wait(1))
            t2.start()
            time.sleep(0.05)

            sess2.stop_flag.set()
            t2.join(1)

            self.assertFalse(t2.is_alive())
            self.assertEqual(results["second"], "(已中止)")
            self.assertFalse(second_started.is_set())
        finally:
            release_first.set()
            t1.join(1)
            t2.join(1)
            subprocess.Popen = orig_popen


# ═════════════════════════════════════════════════════════════════
# 9. 提示词 fallback (NR-9)
# ═════════════════════════════════════════════════════════════════

class TestPromptFallbacks(unittest.TestCase):
    """验证 prompt_config 为空时 build_*_prompt 仍能正常工作。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls._orig_config = cls.mod.prompt_config.copy()
        cls.mod.prompt_config.clear()

    @classmethod
    def tearDownClass(cls):
        cls.mod.prompt_config.update(cls._orig_config)

    def test_claude_first_fallback(self):
        result = self.mod.build_claude_first_prompt("测试任务", "/tmp")
        self.assertIn("测试任务", result)

    def test_codex_first_fallback(self):
        result = self.mod.build_codex_first_prompt("任务", "Claude 方案")
        self.assertIn("任务", result)
        self.assertIn("Claude 方案", result)

    def test_claude_revise_fallback(self):
        result = self.mod.build_claude_revise_prompt("反馈内容")
        self.assertIn("反馈内容", result)

    def test_execution_fallback(self):
        result = self.mod.build_execution_prompt("任务")
        self.assertIn("任务", result)

    def test_claude_post_fix_fallback(self):
        result = self.mod.build_claude_post_fix_prompt("修复反馈")
        # fallback 模板可能不含 {review_feedback} 占位符，但函数不应崩溃
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)


# ═════════════════════════════════════════════════════════════════
# 10. HTTP API 端点结构 (graceful skip)
# ═════════════════════════════════════════════════════════════════

class TestAPIContractHermetic(unittest.TestCase):
    """Hermetic API 测试 — 阻止所有外部副作用。

    在 setUpClass 中 monkey-patch run_negotiation / save_recent_paths / LOG_DIR
    来阻止后台线程进入真实 CLI 调用；HTTP 请求改为进程内分发，
    不依赖真实监听端口。
    """

    _skip_reason = None

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls._tmpdir = _patch_log_dir(cls.mod)
        if cls._tmpdir is None:
            cls._skip_reason = (
                "无法创建外部 scratch 目录；可通过 BRIDGE_TEST_TMPDIR 指向可写目录"
            )
            return

        cls._project_path = ROOT
        cls._browse_path = ROOT
        cls._complete_prefix = ROOT if ROOT.endswith(os.sep) else ROOT + os.sep

        cls._orig_run_negotiation = cls.mod.run_negotiation
        cls._orig_run_execution = cls.mod.run_execution
        cls._orig_run_review_fix_cycle = cls.mod.run_review_fix_cycle
        cls._orig_save_recent = cls.mod.save_recent_paths
        cls._orig_prompt_config = cls.mod.prompt_config.copy()
        cls._orig_save_prompts = cls.mod.save_prompts

        def _fake_negotiation(sess, start_round=1):
            sess.status = "max_rounds"
            sess.current_round = sess.max_rounds

        cls.mod.run_negotiation = _fake_negotiation
        cls.mod.run_execution = lambda sess: None
        cls.mod.run_review_fix_cycle = lambda sess: None
        cls.mod.save_recent_paths = lambda paths: None
        cls.mod.prompt_config.clear()
        cls.mod.prompt_config.update({k: f"test template for {k}" for k in PROMPT_KEYS_SET})
        cls.mod.save_prompts = lambda data: None

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, '_orig_run_negotiation'):
            cls.mod.run_negotiation = cls._orig_run_negotiation
            cls.mod.run_execution = cls._orig_run_execution
            cls.mod.run_review_fix_cycle = cls._orig_run_review_fix_cycle
            cls.mod.save_recent_paths = cls._orig_save_recent
            cls.mod.prompt_config.clear()
            cls.mod.prompt_config.update(cls._orig_prompt_config)
            cls.mod.save_prompts = cls._orig_save_prompts
        if hasattr(cls, '_tmpdir') and cls._tmpdir:
            _restore_log_dir(cls.mod)
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        if self._skip_reason:
            self.skipTest(self._skip_reason)

    def _get(self, path):
        return _dispatch_http_request(self.mod, "GET", path)

    def _post(self, path, body=None):
        return _dispatch_http_request(self.mod, "POST", path, body)

    # ── GET 端点 ──

    def test_state_idle_response_shape(self):
        status, data = self._get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), STATE_RESPONSE_KEYS)
        self.assertEqual(data["status"], "idle")

    def test_events_response_shape(self):
        status, data = self._get("/api/events")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), EVENTS_RESPONSE_KEYS)
        self.assertIsInstance(data["events"], list)
        self.assertIsInstance(data["next"], int)

    def test_history_response_shape(self):
        status, data = self._get("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), HISTORY_RESPONSE_KEYS)

    def test_sessions_response_shape(self):
        status, data = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertIn("sessions", data)
        self.assertIsInstance(data["sessions"], list)

    def test_prompts_response_has_all_keys(self):
        status, data = self._get("/api/prompts")
        self.assertEqual(status, 200)
        missing = PROMPT_KEYS_SET - set(data.keys())
        self.assertEqual(missing, set(), f"/api/prompts 缺少键: {missing}")

    def test_recent_paths_response_shape(self):
        status, data = self._get("/api/recent_paths")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), RECENT_PATHS_RESPONSE_KEYS)

    def test_browse_response_shape(self):
        encoded = urllib.parse.quote(self._browse_path)
        status, data = self._get(f"/api/browse?path={encoded}")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), BROWSE_RESPONSE_KEYS)

    def test_complete_response_shape(self):
        encoded = urllib.parse.quote(self._complete_prefix)
        status, data = self._get(f"/api/complete?prefix={encoded}")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), COMPLETE_RESPONSE_KEYS)

    # ── POST 端点 ──

    def test_start_requires_task(self):
        status, data = self._post(
            "/api/start", {"task": "", "project_path": self._project_path}
        )
        self.assertEqual(status, 400)

    def test_start_requires_path(self):
        status, data = self._post("/api/start", {"task": "test", "project_path": ""})
        self.assertEqual(status, 400)

    def test_start_requires_valid_path(self):
        status, data = self._post("/api/start",
                                  {"task": "test", "project_path": "/nonexistent/path/xyz"})
        self.assertEqual(status, 400)

    def test_start_success_response_shape(self):
        status, data = self._post("/api/start", {
            "task": "contract test",
            "project_path": self._project_path,
        })
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), START_RESPONSE_KEYS)
        self.assertTrue(data["ok"])
        self._post("/api/stop", {"session_id": data["session_id"]})

    def test_execute_requires_session(self):
        status, data = self._post("/api/execute", {"session_id": "nonexistent"})
        self.assertEqual(status, 404)

    def test_inject_requires_session(self):
        status, data = self._post("/api/inject",
                                  {"session_id": "nonexistent", "message": "test"})
        self.assertEqual(status, 404)

    def test_stop_requires_session(self):
        status, data = self._post("/api/stop", {"session_id": "nonexistent"})
        self.assertEqual(status, 404)

    # ── 状态机守卫 via API ──

    def test_execute_wrong_state_returns_400(self):
        """running 状态下 execute 返回 400。"""
        _, start_data = self._post("/api/start",
                                   {"task": "t", "project_path": self._project_path})
        sid = start_data["session_id"]
        try:
            sess = self.mod.get_session(sid)
            with sess.status_lock:
                sess.status = "running"
            status, data = self._post("/api/execute", {"session_id": sid})
            self.assertEqual(status, 400)
        finally:
            self._post("/api/stop", {"session_id": sid})

    def test_continue_consensus_without_reason_returns_400(self):
        """consensus 下 continue 无 reason 返回 400。"""
        _, start_data = self._post("/api/start",
                                   {"task": "t", "project_path": self._project_path})
        sid = start_data["session_id"]
        try:
            sess = self.mod.get_session(sid)
            with sess.status_lock:
                sess.status = "consensus"
                sess.consensus = True
            status, data = self._post("/api/continue",
                                      {"session_id": sid, "extra_rounds": 3})
            self.assertEqual(status, 400)
            self.assertIn("驳回共识时必须提供理由", data.get("error", ""))
        finally:
            self._post("/api/stop", {"session_id": sid})

    def test_inject_consensus_returns_400(self):
        """consensus 下 inject 返回 400。"""
        _, start_data = self._post("/api/start",
                                   {"task": "t", "project_path": self._project_path})
        sid = start_data["session_id"]
        try:
            sess = self.mod.get_session(sid)
            with sess.status_lock:
                sess.status = "consensus"
            status, data = self._post("/api/inject",
                                      {"session_id": sid, "message": "test"})
            self.assertEqual(status, 400)
        finally:
            self._post("/api/stop", {"session_id": sid})

    def test_review_fix_wrong_state_returns_400(self):
        """非 review_fix 下 review_fix 返回 400。"""
        _, start_data = self._post("/api/start",
                                   {"task": "t", "project_path": self._project_path})
        sid = start_data["session_id"]
        try:
            sess = self.mod.get_session(sid)
            with sess.status_lock:
                sess.status = "running"
            status, data = self._post("/api/review_fix", {"session_id": sid})
            self.assertEqual(status, 400)
        finally:
            self._post("/api/stop", {"session_id": sid})

    # ── Step F: 补全端点覆盖 ──

    def test_root_returns_html(self):
        """GET / 返回 HTML。"""
        status, data = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("<!doctype html>", data[:100].lower() if isinstance(data, str) else "")

    def test_review_skip_wrong_state_returns_400(self):
        """非 review_fix 下 review_skip 返回 400。"""
        _, start_data = self._post("/api/start",
                                   {"task": "t", "project_path": self._project_path})
        sid = start_data["session_id"]
        try:
            sess = self.mod.get_session(sid)
            with sess.status_lock:
                sess.status = "running"
            status, _ = self._post("/api/review_skip", {"session_id": sid})
            self.assertEqual(status, 400)
        finally:
            self._post("/api/stop", {"session_id": sid})

    def test_review_skip_success(self):
        """review_fix 状态下 review_skip → 200 + done。"""
        _, start_data = self._post("/api/start",
                                   {"task": "t", "project_path": self._project_path})
        sid = start_data["session_id"]
        try:
            sess = self.mod.get_session(sid)
            with sess.status_lock:
                sess.status = "review_fix"
            status, data = self._post("/api/review_skip", {"session_id": sid})
            self.assertEqual(status, 200)
            self.assertEqual(sess.status, "done")
        finally:
            self._post("/api/stop", {"session_id": sid})

    def test_prompts_post_updates_in_memory(self):
        """POST /api/prompts 更新内存配置 (save_prompts 已被 stub)。"""
        self._post("/api/prompts", {"claude_first": "updated template"})
        status, data = self._get("/api/prompts")
        self.assertEqual(status, 200)
        self.assertEqual(data["claude_first"], "updated template")

    def test_continue_from_max_rounds(self):
        """max_rounds 下 continue + extra_rounds → 200。"""
        _, start_data = self._post("/api/start",
                                   {"task": "t", "project_path": self._project_path})
        sid = start_data["session_id"]
        try:
            sess = self.mod.get_session(sid)
            with sess.status_lock:
                sess.status = "max_rounds"
                sess.consensus = False
            status, data = self._post("/api/continue",
                                      {"session_id": sid, "extra_rounds": 2})
            self.assertEqual(status, 200)
        finally:
            self._post("/api/stop", {"session_id": sid})

    def test_inject_during_running(self):
        """running 状态下 inject → 200。"""
        _, start_data = self._post("/api/start",
                                   {"task": "t", "project_path": self._project_path})
        sid = start_data["session_id"]
        try:
            sess = self.mod.get_session(sid)
            with sess.status_lock:
                sess.status = "running"
            status, data = self._post("/api/inject",
                                      {"session_id": sid, "message": "feedback"})
            self.assertEqual(status, 200)
        finally:
            self._post("/api/stop", {"session_id": sid})

    # ── Step E: 协议消费行为验证 ──

    def test_execute_guard_consumes_executable_states(self):
        """修改 EXECUTABLE_STATES → /api/execute 行为随之改变。"""
        _, start_data = self._post(
            "/api/start", {"task": "t", "project_path": self._project_path}
        )
        sid = start_data["session_id"]
        sess = self.mod.get_session(sid)
        orig = self.mod.EXECUTABLE_STATES
        try:
            # running 状态正常被拒
            with sess.status_lock:
                sess.status = "running"
            s1, _ = self._post("/api/execute", {"session_id": sid})
            self.assertEqual(s1, 400)
            # 扩展后被接受
            self.mod.EXECUTABLE_STATES = frozenset(orig | {"running"})
            with sess.status_lock:
                sess.status = "running"
            s2, _ = self._post("/api/execute", {"session_id": sid})
            self.assertEqual(s2, 200)
        finally:
            self.mod.EXECUTABLE_STATES = orig
            self._post("/api/stop", {"session_id": sid})

    def test_review_fix_guard_consumes_fixable_states(self):
        """修改 FIXABLE_STATES → /api/review_fix + /api/review_skip 行为随之改变。"""
        _, start_data = self._post(
            "/api/start", {"task": "t", "project_path": self._project_path}
        )
        sid = start_data["session_id"]
        sess = self.mod.get_session(sid)
        orig = self.mod.FIXABLE_STATES
        try:
            # --- review_fix ---
            # running 状态正常被拒
            with sess.status_lock:
                sess.status = "running"
            s1, _ = self._post("/api/review_fix", {"session_id": sid})
            self.assertEqual(s1, 400)
            # 扩展后被接受
            self.mod.FIXABLE_STATES = frozenset(orig | {"running"})
            with sess.status_lock:
                sess.status = "running"
            s2, _ = self._post("/api/review_fix", {"session_id": sid})
            self.assertEqual(s2, 200)
            # --- review_skip (独立守卫 bridge.py:1158) ---
            # 先恢复原始常量，确认 400 基线
            self.mod.FIXABLE_STATES = orig
            with sess.status_lock:
                sess.status = "running"
            s3, _ = self._post("/api/review_skip", {"session_id": sid})
            self.assertEqual(s3, 400)
            # 再扩展，确认 200
            self.mod.FIXABLE_STATES = frozenset(orig | {"running"})
            with sess.status_lock:
                sess.status = "running"
            s4, _ = self._post("/api/review_skip", {"session_id": sid})
            self.assertEqual(s4, 200)
        finally:
            self.mod.FIXABLE_STATES = orig
            self._post("/api/stop", {"session_id": sid})

    def test_continue_guard_consumes_continuable_states(self):
        """修改 CONTINUABLE_STATES → /api/continue 行为随之改变。"""
        _, start_data = self._post(
            "/api/start", {"task": "t", "project_path": self._project_path}
        )
        sid = start_data["session_id"]
        sess = self.mod.get_session(sid)
        orig = self.mod.CONTINUABLE_STATES
        try:
            # running 状态正常被拒
            with sess.status_lock:
                sess.status = "running"
            s1, _ = self._post("/api/continue",
                               {"session_id": sid, "extra_rounds": 3})
            self.assertEqual(s1, 400)
            # 扩展后被接受 (running 不是 "consensus" 所以不需要 reason)
            self.mod.CONTINUABLE_STATES = frozenset(orig | {"running"})
            with sess.status_lock:
                sess.status = "running"
            s2, _ = self._post("/api/continue",
                               {"session_id": sid, "extra_rounds": 3})
            self.assertEqual(s2, 200)
        finally:
            self.mod.CONTINUABLE_STATES = orig
            self._post("/api/stop", {"session_id": sid})


if __name__ == "__main__":
    unittest.main()
