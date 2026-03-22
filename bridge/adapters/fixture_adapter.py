"""
Fixture CLI 适配器
=================
用于浏览器级 E2E 和开发期自测的内建确定性 adapter。
"""

from __future__ import annotations

from datetime import datetime

from bridge.session import add_event

from .base import CLIAdapter


class FixtureAdapter(CLIAdapter):
    @property
    def id(self) -> str:
        return "fixture-cli"

    @property
    def display_name(self) -> str:
        return "Fixture CLI"

    @property
    def cli_name(self) -> str:
        return "fixture-cli"

    @property
    def agent_name(self) -> str:
        return "fixture"

    @property
    def capabilities(self) -> dict:
        return {
            **super().capabilities,
            "can_detect_install": True,
            "plan_mode": True,
            "dangerous_mode": True,
            "stream_json": False,
            "session_resume": False,
            "fixture": True,
        }

    def probe(self):
        self._probed_path = "builtin://fixture-cli"
        self._probed_version = "fixture-v1"
        self._probe_error = None
        self._probed_at = datetime.now().isoformat()

    def build_command(self, prompt: str, cwd: str, **kwargs) -> list[str]:
        role_key = kwargs.get("role_key", "fixture")
        return ["fixture-cli", role_key]

    def parse_stream_line(self, line: str) -> dict | None:
        return None

    def run(self, prompt, cwd, sess, log_tag=None, agent_label=None, **kwargs):
        role_key = kwargs.pop("role_key", None) or agent_label or self.agent_name
        add_event(
            sess,
            "lane.cli_started",
            {"round": sess.current_round or sess.current_review_round, "command": ["fixture-cli", role_key]},
            role_key=role_key,
            source=self.id,
        )
        output = self._result_for(role_key)
        add_event(
            sess,
            "lane.stdout_chunk",
            {"text": output + "\n"},
            role_key=role_key,
            source=self.id,
        )
        add_event(
            sess,
            "lane.result_emitted",
            {"text": output},
            role_key=role_key,
            source=self.id,
        )
        return output

    @staticmethod
    def _result_for(role_key: str) -> str:
        if role_key == "planner":
            return "# Plan\n- 使用统一账本\n- 保持 viewport 与 lane 同步"
        if role_key == "reviewer":
            return "APPROVED\n方案可执行"
        if role_key == "executor":
            return "执行完成"
        if role_key == "validator":
            return "任务收口成功：fixture 校验通过"
        return "fixture result"
