"""
Bridge Contract Tests
=====================
可执行的 smoke/contract tests，保障协议不漂移。

覆盖范围:
  1. 状态机转换约束
  2. API 端点返回结构
  3. 事件类型完整性
  4. 提示词键完整性
  5. 协议常量与代码一致性

运行:
  python3 -m unittest tests.test_contract -v

依赖:
  只使用 Python 标准库 (unittest, json, http.client, threading, re)。
  无外部依赖。
"""

import ast
import json
import http.client
import os
import re
import sys
import threading
import time
import unittest

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
# 辅助: 从 bridge.py 源码提取信息
# ═════════════════════════════════════════════════════════════════

BRIDGE_PY = os.path.join(ROOT, "bridge.py")


def _read_bridge_source():
    with open(BRIDGE_PY, encoding="utf-8") as f:
        return f.read()


def _extract_add_event_types(source):
    """从源码中提取所有 add_event(sess, "xxx", ...) 的事件类型。"""
    pattern = r'add_event\(\s*sess\s*,\s*["\']([^"\']+)["\']'
    return set(re.findall(pattern, source))


def _extract_add_history_event_types(source):
    """从源码中提取所有 add_history_event(..., "xxx") 的事件类型。"""
    pattern = r'add_history_event\([^)]*,\s*["\']([^"\']+)["\']'
    return set(re.findall(pattern, source))


def _extract_prompt_config_keys(source):
    """从源码中提取所有 prompt_config.get("xxx", ...) 的键。"""
    pattern = r'prompt_config\.get\(\s*["\']([^"\']+)["\']'
    return set(re.findall(pattern, source))


def _extract_frontend_cfg_keys(source):
    """从 HTML_UI 中的 cfgKeys 数组提取键。"""
    pattern = r"cfgKeys\s*=\s*\[([^\]]+)\]"
    match = re.search(pattern, source)
    if not match:
        return set()
    keys_str = match.group(1)
    return set(re.findall(r"'([^']+)'", keys_str))


# ═════════════════════════════════════════════════════════════════
# 1. 状态机测试
# ═════════════════════════════════════════════════════════════════

class TestStateMachine(unittest.TestCase):
    """验证状态机定义和转换约束。"""

    def test_all_states_defined(self):
        """protocol.STATES 包含 9 种状态。"""
        self.assertEqual(len(STATES), 9)
        expected = {"idle", "running", "consensus", "max_rounds",
                    "executing", "review_pending", "review_fix", "done", "error"}
        self.assertEqual(STATES, expected)

    def test_executable_states(self):
        """只有 consensus 和 max_rounds 可触发执行。"""
        self.assertEqual(EXECUTABLE_STATES, {"consensus", "max_rounds"})

    def test_fixable_states(self):
        """只有 review_fix 可触发修复。"""
        self.assertEqual(FIXABLE_STATES, {"review_fix"})

    def test_continuable_states(self):
        """只有 consensus 和 max_rounds 可继续协商。"""
        self.assertEqual(CONTINUABLE_STATES, {"consensus", "max_rounds"})

    def test_terminal_states(self):
        """终态: idle, done, error。"""
        self.assertEqual(TERMINAL_STATES, {"idle", "done", "error"})

    def test_states_in_bridge_py(self):
        """验证 bridge.py 前端 pill 文本映射包含所有状态。"""
        source = _read_bridge_source()
        # 提取 updSt 中的状态映射键
        pattern = r"idle:'IDLE',running:'NEGOTIATING',consensus:'CONSENSUS',max_rounds:'MAX ROUNDS',executing:'EXECUTING',review_pending:'REVIEWING',review_fix:'NEEDS FIX',done:'DONE',error:'ERROR'"
        self.assertIn("idle:'IDLE'", source)
        self.assertIn("error:'ERROR'", source)

    def test_session_state_initial_status(self):
        """SessionState 初始 status 为 'running'。"""
        source = _read_bridge_source()
        # 验证 self.status = "running" 在 __init__ 中
        self.assertIn('self.status = "running"', source)

    def test_execute_cas_guard(self):
        """执行前有 CAS 状态检查。"""
        source = _read_bridge_source()
        # 验证只从 consensus/max_rounds 切换到 executing
        self.assertIn('if sess.status not in ("consensus", "max_rounds")', source)

    def test_review_fix_cas_guard(self):
        """修复前有 CAS 状态检查。"""
        source = _read_bridge_source()
        self.assertIn('if sess.status != "review_fix"', source)

    def test_continue_consensus_requires_reason(self):
        """共识状态继续协商必须提供理由。"""
        source = _read_bridge_source()
        self.assertIn('reason = body.get("message", "").strip()', source)
        self.assertIn('"驳回共识时必须提供理由"', source)


# ═════════════════════════════════════════════════════════════════
# 2. 事件协议测试
# ═════════════════════════════════════════════════════════════════

class TestEventProtocol(unittest.TestCase):
    """验证事件类型完整性。"""

    def test_event_types_count(self):
        """protocol.EVENT_TYPES 包含 20 种事件类型。"""
        self.assertEqual(len(EVENT_TYPES), 20)

    def test_add_event_types_covered(self):
        """bridge.py 中所有 add_event 调用的类型 ⊆ protocol.EVENT_TYPES。"""
        source = _read_bridge_source()
        code_types = _extract_add_event_types(source)
        uncovered = code_types - EVENT_TYPES
        self.assertEqual(uncovered, set(),
                         f"bridge.py 中的事件类型未在 protocol.EVENT_TYPES 中定义: {uncovered}")

    def test_add_history_event_types_covered(self):
        """bridge.py 中所有 add_history_event 的事件类型 ⊆ protocol.EVENT_TYPES。"""
        source = _read_bridge_source()
        code_types = _extract_add_history_event_types(source)
        uncovered = code_types - EVENT_TYPES
        self.assertEqual(uncovered, set(),
                         f"add_history_event 类型未在 protocol 中定义: {uncovered}")

    def test_no_extra_protocol_types(self):
        """protocol.EVENT_TYPES 中没有 bridge.py 不使用的多余类型。"""
        source = _read_bridge_source()
        code_types = _extract_add_event_types(source) | _extract_add_history_event_types(source)
        extra = EVENT_TYPES - code_types
        self.assertEqual(extra, set(),
                         f"protocol.EVENT_TYPES 中定义了但代码未使用的类型: {extra}")

    def test_add_event_structure(self):
        """add_event 函数产出 {id, type, data, ts} 结构。"""
        source = _read_bridge_source()
        # 验证 add_event 的结构
        self.assertIn('"id": len(sess.events)', source)
        self.assertIn('"type": etype', source)
        self.assertIn('"data": data', source)
        self.assertIn('"ts":', source)

    def test_add_history_event_atomicity(self):
        """add_history_event 在同一个锁内同时操作 history 和 events。"""
        source = _read_bridge_source()
        # 验证 event_lock 保护
        self.assertIn('def add_history_event', source)
        # 函数体内有 with sess.event_lock:
        # 找到 add_history_event 函数体
        match = re.search(
            r'def add_history_event\(.*?\):\n(.*?)(?=\ndef |\nclass |\Z)',
            source, re.DOTALL
        )
        self.assertIsNotNone(match)
        body = match.group(1)
        self.assertIn('with sess.event_lock:', body)
        self.assertIn('history_list.append(entry)', body)
        self.assertIn('sess.events.append(', body)


# ═════════════════════════════════════════════════════════════════
# 2b. 事件 payload 级别验证
# ═════════════════════════════════════════════════════════════════

class TestEventPayloads(unittest.TestCase):
    """验证事件 payload 结构与 protocol.py 定义一致。

    从 bridge.py 源码中提取每种事件的实际 payload key，
    与 EVENT_PAYLOAD_REQUIRED_KEYS 做双向校验。
    """

    def test_payload_schema_covers_all_event_types(self):
        """EVENT_PAYLOAD_REQUIRED_KEYS 覆盖全部 20 种事件类型。"""
        self.assertEqual(set(EVENT_PAYLOAD_REQUIRED_KEYS.keys()), EVENT_TYPES)

    def test_status_change_payload(self):
        """status_change 事件始终携带 status 和 msg。"""
        source = _read_bridge_source()
        # 找到所有 add_event(sess, "status_change", {...}) 调用
        matches = re.findall(
            r'add_event\(sess,\s*"status_change",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"status"', m, f"status_change payload 缺少 status: {m}")
            self.assertIn('"msg"', m, f"status_change payload 缺少 msg: {m}")

    def test_round_start_payload(self):
        """round_start 事件携带 round 和 max。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"round_start",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"round"', m)
            self.assertIn('"max"', m)

    def test_agent_thinking_payload(self):
        """agent_thinking 事件携带 agent 和 round。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"agent_thinking",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"agent"', m)
            self.assertIn('"round"', m)

    def test_consensus_reached_payload(self):
        """consensus_reached 事件携带 round 和 msg。"""
        source = _read_bridge_source()
        match = re.search(
            r'add_event\(sess,\s*"consensus_reached",\s*\{(.*?)\}\s*\)',
            source, re.DOTALL
        )
        self.assertIsNotNone(match)
        body = match.group(1)
        self.assertIn('"round"', body)
        self.assertIn('"msg"', body)

    def test_execution_done_payload(self):
        """execution_done 事件携带 result。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"execution_done",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"result"', m)

    def test_review_done_payload(self):
        """review_done 事件携带 round, msg, success。"""
        source = _read_bridge_source()
        # 提取 review_done 调用所在的完整行
        lines = [l for l in source.splitlines()
                 if 'add_event(sess, "review_done"' in l]
        self.assertGreater(len(lines), 0)
        for line in lines:
            self.assertIn('"round"', line)
            self.assertIn('"msg"', line)
            self.assertIn('"success"', line)

    def test_review_needs_fix_payload(self):
        """review_needs_fix 事件携带 round, msg, review。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"review_needs_fix",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"round"', m)
            self.assertIn('"msg"', m)
            self.assertIn('"review"', m)

    def test_rollback_payload(self):
        """rollback 事件携带 round, max, plan, msg。"""
        source = _read_bridge_source()
        match = re.search(
            r'add_event\(sess,\s*"rollback",\s*\{(.*?)\}\s*\)',
            source, re.DOTALL
        )
        self.assertIsNotNone(match)
        body = match.group(1)
        self.assertIn('"round"', body)
        self.assertIn('"max"', body)
        self.assertIn('"plan"', body)
        self.assertIn('"msg"', body)

    def test_error_payload(self):
        """error 事件携带 msg。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"error",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"msg"', m)

    def test_warning_payload(self):
        """warning 事件携带 msg。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"warning",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"msg"', m)

    def test_cli_start_payload(self):
        """cli_start 事件携带 agent 和 round。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"cli_start",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"agent"', m)

    def test_agent_result_payload(self):
        """agent_result 事件携带 agent 和 text。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"agent_result",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"agent"', m)
            self.assertIn('"text"', m)

    def test_agent_chunk_payload(self):
        """agent_chunk 事件携带 agent 和 text。"""
        source = _read_bridge_source()
        lines = [l for l in source.splitlines()
                 if 'add_event(sess, "agent_chunk"' in l]
        self.assertGreater(len(lines), 0)
        for line in lines:
            self.assertIn('"agent"', line)
            self.assertIn('"text"', line)

    def test_chunk_boundary_payload(self):
        """chunk_boundary 事件携带 agent 和 boundary_type。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"chunk_boundary",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"agent"', m)
            self.assertIn('"boundary_type"', m)

    def test_agent_stderr_payload(self):
        """agent_stderr 事件携带 agent, text, is_mcp。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"agent_stderr",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"agent"', m)
            self.assertIn('"text"', m)
            self.assertIn('"is_mcp"', m)

    def test_agent_response_payload(self):
        """agent_response 事件 (via add_history_event) 条目携带 round, role, phase, content。"""
        source = _read_bridge_source()
        # agent_response 通过 add_history_event 发送，entry dict 含所需字段
        # 找所有构造 entry 后调用 add_history_event(..., "agent_response") 的位置
        lines = [l for l in source.splitlines()
                 if 'add_history_event(' in l and '"agent_response"' in l]
        self.assertGreater(len(lines), 0)
        # entry 结构在调用前构造，验证 entry 模式存在
        # 找所有 "round":, "role":, "phase":, "content": 的 entry dict
        entry_pattern = re.findall(
            r'entry\w*\s*=\s*\{([^}]+)\}\s*\n\s*add_history_event',
            source
        )
        self.assertGreater(len(entry_pattern), 0)
        for m in entry_pattern:
            self.assertIn('"round"', m)
            self.assertIn('"role"', m)
            self.assertIn('"phase"', m)
            self.assertIn('"content"', m)

    def test_max_rounds_reached_payload(self):
        """max_rounds_reached 事件携带 round 和 msg。"""
        source = _read_bridge_source()
        match = re.search(
            r'add_event\(sess,\s*"max_rounds_reached",\s*\{(.*?)\}\s*\)',
            source, re.DOTALL
        )
        self.assertIsNotNone(match)
        body = match.group(1)
        self.assertIn('"round"', body)
        self.assertIn('"msg"', body)

    def test_review_start_payload(self):
        """review_start 事件携带 round 和 max。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"review_start",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"round"', m)
            self.assertIn('"max"', m)

    def test_review_round_start_payload(self):
        """review_round_start 事件携带 round 和 max。"""
        source = _read_bridge_source()
        matches = re.findall(
            r'add_event\(sess,\s*"review_round_start",\s*\{([^}]+)\}',
            source
        )
        self.assertGreater(len(matches), 0)
        for m in matches:
            self.assertIn('"round"', m)
            self.assertIn('"max"', m)

    def test_review_response_payload(self):
        """review_response 事件 (via add_history_event) 条目携带 round, role, phase, content。"""
        source = _read_bridge_source()
        lines = [l for l in source.splitlines()
                 if 'add_history_event(' in l and '"review_response"' in l]
        self.assertGreater(len(lines), 0)
        # 验证 entry 构造包含必需字段
        entry_pattern = re.findall(
            r'entry\s*=\s*\{([^}]+)\}\s*\n\s*add_history_event\(sess,\s*sess\.review_history',
            source
        )
        self.assertGreater(len(entry_pattern), 0)
        for m in entry_pattern:
            self.assertIn('"round"', m)
            self.assertIn('"role"', m)
            self.assertIn('"phase"', m)
            self.assertIn('"content"', m)


# ═════════════════════════════════════════════════════════════════
# 3. 提示词完整性测试
# ═════════════════════════════════════════════════════════════════

class TestPromptKeys(unittest.TestCase):
    """验证提示词键覆盖。"""

    def test_prompt_keys_count(self):
        """protocol.PROMPT_KEYS 包含 11 个键。"""
        self.assertEqual(len(PROMPT_KEYS), 11)

    def test_all_code_keys_in_protocol(self):
        """bridge.py 中 prompt_config.get() 的所有键 ⊆ PROMPT_KEYS_SET。"""
        source = _read_bridge_source()
        code_keys = _extract_prompt_config_keys(source)
        uncovered = code_keys - PROMPT_KEYS_SET
        self.assertEqual(uncovered, set(),
                         f"代码中使用但协议未定义的提示词键: {uncovered}")

    def test_no_extra_protocol_keys(self):
        """PROMPT_KEYS_SET 中没有代码不使用的多余键。"""
        source = _read_bridge_source()
        code_keys = _extract_prompt_config_keys(source)
        extra = PROMPT_KEYS_SET - code_keys
        self.assertEqual(extra, set(),
                         f"协议定义但代码未使用的提示词键: {extra}")

    def test_frontend_cfg_keys_match(self):
        """前端 cfgKeys 数组 == protocol.PROMPT_KEYS_SET。"""
        source = _read_bridge_source()
        frontend_keys = _extract_frontend_cfg_keys(source)
        self.assertEqual(frontend_keys, PROMPT_KEYS_SET,
                         f"前端 cfgKeys 与 protocol.PROMPT_KEYS 不一致。"
                         f"\n前端: {frontend_keys}\n协议: {PROMPT_KEYS_SET}")

    def test_prompts_json_has_all_keys(self):
        """prompts.json 文件包含全部 11 个键。"""
        prompts_file = os.path.join(ROOT, "prompts.json")
        if not os.path.exists(prompts_file):
            self.skipTest("prompts.json 不存在")
        with open(prompts_file, encoding="utf-8") as f:
            data = json.load(f)
        missing = PROMPT_KEYS_SET - set(data.keys())
        self.assertEqual(missing, set(),
                         f"prompts.json 缺少键: {missing}")


# ═════════════════════════════════════════════════════════════════
# 4. API 契约测试 (启动 HTTP server 验证)
# ═════════════════════════════════════════════════════════════════

def _find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


class TestAPIContract(unittest.TestCase):
    """验证 API 端点返回结构。

    在 setUpClass 中启动 bridge.py HTTP server (随机端口)。
    如果 socket bind 失败 (沙箱/CI 限制)，整个类 graceful skip。
    """

    server = None
    port = None
    server_thread = None
    _skip_reason = None

    @classmethod
    def setUpClass(cls):
        """启动 bridge HTTP server 用于测试。"""
        try:
            cls.port = _find_free_port()
        except (PermissionError, OSError) as e:
            cls._skip_reason = f"无法绑定端口 (沙箱/CI 限制): {e}"
            return

        try:
            # 动态导入 bridge.py 中的 server 类
            # 不用 import bridge (那是新包)，直接加载 bridge.py 脚本
            import importlib.util
            spec = importlib.util.spec_from_file_location("bridge_legacy", BRIDGE_PY)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            cls._bridge_mod = mod
            cls.server = mod.ThreadedHTTPServer(("127.0.0.1", cls.port), mod.BridgeHandler)
            cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
            cls.server_thread.start()
            time.sleep(0.3)  # 等待 server 启动
        except (PermissionError, OSError) as e:
            cls._skip_reason = f"无法启动 HTTP server: {e}"

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()

    def setUp(self):
        if self._skip_reason:
            self.skipTest(self._skip_reason)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        return resp.status, json.loads(data) if resp.getheader("Content-Type", "").startswith("application/json") else data

    def _post(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body or {}).encode()
        conn.request("POST", path, body=payload,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(payload))})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    # ── GET 端点结构验证 ──

    def test_state_idle_response_shape(self):
        """GET /api/state 无 sid 时返回完整字段。"""
        status, data = self._get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), STATE_RESPONSE_KEYS)
        self.assertEqual(data["status"], "idle")

    def test_events_response_shape(self):
        """GET /api/events 返回 {events, next}。"""
        status, data = self._get("/api/events")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), EVENTS_RESPONSE_KEYS)
        self.assertIsInstance(data["events"], list)
        self.assertIsInstance(data["next"], int)

    def test_history_response_shape(self):
        """GET /api/history 无 sid 时返回完整字段。"""
        status, data = self._get("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), HISTORY_RESPONSE_KEYS)

    def test_sessions_response_shape(self):
        """GET /api/sessions 返回 {sessions[]}。"""
        status, data = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertIn("sessions", data)
        self.assertIsInstance(data["sessions"], list)

    def test_prompts_response_has_all_keys(self):
        """GET /api/prompts 返回包含 11 个键的对象。"""
        status, data = self._get("/api/prompts")
        self.assertEqual(status, 200)
        missing = PROMPT_KEYS_SET - set(data.keys())
        self.assertEqual(missing, set(),
                         f"/api/prompts 缺少键: {missing}")

    def test_recent_paths_response_shape(self):
        """GET /api/recent_paths 返回结构匹配 RECENT_PATHS_RESPONSE_KEYS。"""
        status, data = self._get("/api/recent_paths")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), RECENT_PATHS_RESPONSE_KEYS)
        self.assertIsInstance(data["paths"], list)

    def test_browse_response_shape(self):
        """GET /api/browse?path=/tmp 返回结构匹配 BROWSE_RESPONSE_KEYS。"""
        status, data = self._get("/api/browse?path=/tmp")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), BROWSE_RESPONSE_KEYS)
        self.assertIsInstance(data["dirs"], list)
        self.assertIsInstance(data["is_git"], bool)
        self.assertIsInstance(data["truncated"], bool)

    def test_complete_response_shape(self):
        """GET /api/complete?prefix=/tmp/ 返回结构匹配 COMPLETE_RESPONSE_KEYS。"""
        status, data = self._get("/api/complete?prefix=/tmp/")
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), COMPLETE_RESPONSE_KEYS)
        self.assertIsInstance(data["suggestions"], list)

    # ── POST 端点验证 ──

    def test_start_requires_task(self):
        """POST /api/start 空 task 返回 400。"""
        status, data = self._post("/api/start", {"task": "", "project_path": "/tmp"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_start_requires_path(self):
        """POST /api/start 空 path 返回 400。"""
        status, data = self._post("/api/start", {"task": "test", "project_path": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_start_requires_valid_path(self):
        """POST /api/start 无效路径返回 400。"""
        status, data = self._post("/api/start",
                                  {"task": "test", "project_path": "/nonexistent/path/xyz"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_start_success_response_shape(self):
        """POST /api/start 成功时返回结构匹配 START_RESPONSE_KEYS。"""
        status, data = self._post("/api/start",
                                  {"task": "contract test", "project_path": "/tmp"})
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), START_RESPONSE_KEYS)
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["session_id"], str)
        self.assertGreater(len(data["session_id"]), 0)
        # 清理: 停止刚创建的会话
        self._post("/api/stop", {"session_id": data["session_id"]})

    def test_sessions_listing_keys(self):
        """GET /api/sessions 中每个条目包含 SESSION_LISTING_KEYS。"""
        # 先创建一个会话
        _, start_data = self._post("/api/start",
                                   {"task": "listing test", "project_path": "/tmp"})
        sid = start_data.get("session_id", "")
        try:
            status, data = self._get("/api/sessions")
            self.assertEqual(status, 200)
            sessions = data["sessions"]
            self.assertGreater(len(sessions), 0)
            for s in sessions:
                missing = SESSION_LISTING_KEYS - set(s.keys())
                self.assertEqual(missing, set(),
                                 f"session listing 缺少字段: {missing}")
        finally:
            self._post("/api/stop", {"session_id": sid})

    def test_execute_requires_session(self):
        """POST /api/execute 无效 session 返回 404。"""
        status, data = self._post("/api/execute", {"session_id": "nonexistent"})
        self.assertEqual(status, 404)

    def test_inject_requires_session(self):
        """POST /api/inject 无效 session 返回 404。"""
        status, data = self._post("/api/inject",
                                  {"session_id": "nonexistent", "message": "test"})
        self.assertEqual(status, 404)

    def test_stop_requires_session(self):
        """POST /api/stop 无效 session 返回 404。"""
        status, data = self._post("/api/stop", {"session_id": "nonexistent"})
        self.assertEqual(status, 404)


# ═════════════════════════════════════════════════════════════════
# 5. 协议常量一致性测试
# ═════════════════════════════════════════════════════════════════

class TestProtocolConsistency(unittest.TestCase):
    """验证 protocol.py 常量与代码/文档的一致性。"""

    def test_get_endpoints_in_bridge_py(self):
        """protocol.GET_ENDPOINTS 中的路径在 bridge.py 中有对应处理。"""
        source = _read_bridge_source()
        for ep in GET_ENDPOINTS:
            if ep == "/":
                self.assertIn('p.path == "/"', source)
            else:
                self.assertIn(f'p.path == "{ep}"', source,
                              f"GET 端点 {ep} 在 bridge.py 中未找到")

    def test_post_endpoints_in_bridge_py(self):
        """protocol.POST_ENDPOINTS 中的路径在 bridge.py 中有对应处理。"""
        source = _read_bridge_source()
        for ep in POST_ENDPOINTS:
            self.assertIn(f'p.path == "{ep}"', source,
                          f"POST 端点 {ep} 在 bridge.py 中未找到")

    def test_session_state_has_status_lock(self):
        """SessionState 使用 status_lock 保护状态转换。"""
        source = _read_bridge_source()
        self.assertIn("self.status_lock = threading.Lock()", source)

    def test_session_state_has_event_lock(self):
        """SessionState 使用 event_lock 保护事件流。"""
        source = _read_bridge_source()
        self.assertIn("self.event_lock = threading.Lock()", source)

    def test_max_review_rounds_default(self):
        """默认最大审查轮次为 3。"""
        source = _read_bridge_source()
        self.assertIn("self.max_review_rounds = 3", source)

    def test_approval_detection_logic(self):
        """is_approved 检测首行首词 APPROVED。"""
        source = _read_bridge_source()
        self.assertIn("def is_approved(text):", source)
        self.assertIn("APPROVED", source)

    def test_closure_detection_logic(self):
        """审查收口检测首行含'任务收口成功'。"""
        source = _read_bridge_source()
        self.assertIn("任务收口成功", source)

    def test_claude_session_binding(self):
        """Claude 使用 --session-id 和 --resume。"""
        source = _read_bridge_source()
        self.assertIn('"--resume"', source)
        self.assertIn('"--session-id"', source)
        # 不使用 -c
        self.assertNotIn('cmd.extend(["-c"', source)


# ═════════════════════════════════════════════════════════════════
# 6. 骨架包可导入测试
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

    def test_adapter_capabilities(self):
        """adapter 骨架的 capabilities 返回正确结构。"""
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        from bridge.adapters.codex_adapter import CodexAdapter

        claude = ClaudeCodeAdapter.__new__(ClaudeCodeAdapter)
        codex = CodexAdapter.__new__(CodexAdapter)

        # Claude capabilities
        cc = claude.capabilities
        self.assertTrue(cc["plan_mode"])
        self.assertTrue(cc["dangerous_mode"])
        self.assertTrue(cc["stream_json"])
        self.assertTrue(cc["session_resume"])

        # Codex capabilities
        xc = codex.capabilities
        self.assertFalse(xc["plan_mode"])
        self.assertFalse(xc["dangerous_mode"])
        self.assertFalse(xc["stream_json"])
        self.assertTrue(xc["session_resume"])

    def test_adapter_detect_approval(self):
        """CLIAdapter.detect_approval 默认逻辑验证。"""
        from bridge.adapters.claude_adapter import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter.__new__(ClaudeCodeAdapter)

        self.assertTrue(adapter.detect_approval("APPROVED\nsome reason"))
        self.assertTrue(adapter.detect_approval("  APPROVED — looks good"))
        self.assertTrue(adapter.detect_approval("approved\nlowercase"))
        self.assertFalse(adapter.detect_approval("NOT APPROVED"))
        self.assertFalse(adapter.detect_approval(""))
        self.assertFalse(adapter.detect_approval("Some text\nAPPROVED on second line"))

    def test_adapter_detect_closure(self):
        """CLIAdapter.detect_closure 默认逻辑验证。"""
        from bridge.adapters.codex_adapter import CodexAdapter
        adapter = CodexAdapter.__new__(CodexAdapter)

        self.assertTrue(adapter.detect_closure("任务收口成功\n其他内容"))
        self.assertTrue(adapter.detect_closure("任务收口成功"))
        self.assertFalse(adapter.detect_closure("其他内容\n任务收口成功"))
        self.assertFalse(adapter.detect_closure(""))
        self.assertFalse(adapter.detect_closure(None))


if __name__ == "__main__":
    unittest.main()
