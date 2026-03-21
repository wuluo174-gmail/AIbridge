"""
Claude Code CLI 适配器
=====================
封装 call_claude_streaming 的完整逻辑。

Step 3 从 bridge.py 迁入:
  - build_command: L139-150 (命令构造)
  - parse_stream_line: L194-238 (stream-json 解析 + STREAM_DEBUG 采样)
  - plan 检测: L155-161, L164-168, L248-259 (锁/快照/差集校验)
  - run() override: 包裹 plan 检测完整流程

Claude Code 特有功能:
  - stream-json 流格式 (text_delta 事件)
  - --session-id / --resume 会话绑定
  - --permission-mode plan (协商阶段)
  - --dangerously-skip-permissions (执行阶段)
  - Plan 文件快照差集检测
  - stderr MCP 噪音过滤
"""

import json
import os

import bridge.plan
from bridge.session import add_event

from .base import CLIAdapter

_STREAM_DEBUG = os.environ.get("BRIDGE_DEBUG_STREAM") == "1"


class ClaudeCodeAdapter(CLIAdapter):
    """Claude Code CLI 适配器。"""

    def __init__(self, plan_lock_acquire_fn=None):
        super().__init__()
        self._plan_lock_acquire_fn = plan_lock_acquire_fn

    # ── 身份 ──

    @property
    def id(self) -> str:
        return "claude-code"

    @property
    def display_name(self) -> str:
        return "Claude Code"

    @property
    def cli_name(self) -> str:
        return "claude"

    @property
    def agent_name(self) -> str:
        return "claude"

    @property
    def context_files(self):
        return ["CLAUDE.md"]

    @property
    def log_raw_stdout(self) -> bool:
        return False

    # ── 能力矩阵 ──

    @property
    def capabilities(self) -> dict:
        return {
            **super().capabilities,
            "can_detect_install": True,
            "can_detect_auth": False,       # 待验证
            "can_trigger_auth": False,       # 待验证
            "auth_method": "unknown",        # 待验证
            "plan_mode": True,              # --permission-mode plan
            "dangerous_mode": True,         # --dangerously-skip-permissions
            "stream_json": True,            # --output-format stream-json
            "session_resume": True,         # --session-id / --resume
        }

    # ── 命令构造 ──

    def build_command(self, prompt, cwd, **kwargs):
        session_id = kwargs.get("session_id", "")
        continue_session = kwargs.get("continue_session", False)
        bypass_permissions = kwargs.get("bypass_permissions", False)

        cmd = ["claude"]
        if continue_session:
            cmd.extend(["--resume", session_id])
        else:
            cmd.extend(["--session-id", session_id])
        cmd.extend(["-p", "--verbose", "--output-format", "stream-json",
                    "--include-partial-messages", "--effort", "max"])
        if bypass_permissions:
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd.extend(["--permission-mode", "plan"])
        cmd.append(prompt)
        return cmd

    # ── 流解析 ──

    def parse_stream_line(self, line):
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return None

        etype = evt.get("type", "")

        if etype == "stream_event":
            inner = evt.get("event", {})
            inner_type = inner.get("type", "")
            delta = inner.get("delta", {})
            if delta.get("type") == "text_delta":
                chunk = delta.get("text", "")
                return {"type": "text_chunk", "text": chunk} if chunk else None
            elif inner_type == "content_block_stop":
                return {"type": "block_stop"}
            else:
                # 按事件类型采样，每类型 max 5，JSON 序列化 ≤500 字符
                if _STREAM_DEBUG:
                    return {"type": "debug_sample",
                            "key": inner_type or etype,
                            "raw": json.dumps(evt, ensure_ascii=False)[:500]}
                return None

        elif etype == "result":
            return {"type": "result", "text": evt.get("result", "")}

        return None

    # ── 可选钩子 ──

    def get_env_overrides(self):
        return {"CLAUDE_CODE_DISABLE_NONINTERACTIVE_WARNING": "1"}

    def format_process_error(self, returncode, log_file):
        return f"Claude CLI 错误 (code {returncode})，详见日志: {log_file}"

    def format_not_found_error(self):
        return "未找到 'claude' 命令。请安装: npm install -g @anthropic-ai/claude-code"

    # ── Plan 后处理 ──

    def post_process_output(self, output, plan_snapshot, sess):
        """Plan 文件差集校验 — 从 bridge.py L248-259 迁入。"""
        plan_content = bridge.plan.find_new_plan_file(plan_snapshot)
        if plan_content and bridge.plan.validate_plan_relevance(plan_content, sess.task):
            return plan_content
        if plan_content:
            add_event(sess, "warning", {
                "msg": "检测到 plan 文件内容与当前任务不相关，已忽略（可能来自外部 Claude 进程）"
            })
        return output

    # ── run() override — 包裹 plan 检测完整流程 ──

    def run(self, prompt, cwd, sess, log_tag=None, agent_label=None, **kwargs):
        skip_plan_detection = kwargs.pop("skip_plan_detection", False)

        lock = None
        if not skip_plan_detection:
            if self._plan_lock_acquire_fn is None:
                raise RuntimeError(
                    "ClaudeCodeAdapter: plan_lock_acquire_fn 未注入，"
                    "无法安全执行 plan 检测。请通过构造器注入或使用 skip_plan_detection=True")
            lock = self._plan_lock_acquire_fn(sess.project_path, sess.stop_flag)
            if lock is None:
                return "(已中止)"
        try:
            # 拿到锁后立即检查 stop_flag（保留原始 bridge.py L164 语义）
            if sess.stop_flag.is_set():
                return "(已中止)"

            # 运行时读 bridge.plan 模块属性（test monkeypatch 生效）
            plan_snapshot = bridge.plan.snapshot_plan_files() if not skip_plan_detection else None

            output = super().run(prompt, cwd, sess, log_tag=log_tag,
                                 agent_label=agent_label, **kwargs)

            # stop guard: 已中止则跳过 plan 差集检测（保留原始 bridge.py L244 语义）
            if sess.stop_flag.is_set():
                return output

            if plan_snapshot is not None:
                output = self.post_process_output(output, plan_snapshot, sess)

            return output
        finally:
            if lock is not None:
                lock.release()
