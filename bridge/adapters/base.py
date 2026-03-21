"""
Bridge CLIAdapter 基类
=====================
所有 CLI 工具适配器的抽象基类。

设计原则:
  1. 每个工具的差异在 adapter 内部隔离 (build_command, parse_stream_line)
  2. 编排引擎只通过基类接口调用 (run)
  3. 能力矩阵 (capabilities) 声明每个工具支持什么
  4. run() 是 Template Method — 管理进程生命周期，调用抽象钩子
  5. 认证能力全部标记为"待验证" — 当前代码无认证检测逻辑

Step 3 从 bridge.py 迁入:
  - _stderr_reader: L110-128 (共享静态方法)
  - 进程生命周期: Popen/日志/事件发射 (run 具体方法)
"""

import os
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from datetime import datetime

from bridge.session import add_event


class CLIAdapter(ABC):
    """CLI 工具适配器基类。"""

    def __init__(self):
        # 启动时探测一次，后续 discover() 只读这份快照。
        self._probed_path = None
        self._probed_version = None
        self._probe_error = None
        self._probed_at = None

    # ── 身份 ──

    @property
    @abstractmethod
    def id(self) -> str:
        """工具唯一标识，如 "claude-code", "codex"。"""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """工具显示名称，如 "Claude Code", "Codex"。"""
        ...

    @property
    @abstractmethod
    def cli_name(self) -> str:
        """可执行文件名，如 "claude", "codex"。"""
        ...

    # ── 可覆盖属性 ──

    @property
    def agent_name(self) -> str:
        """事件流中使用的代理名称。默认与 id 相同，子类可覆盖。"""
        return self.id

    @property
    def context_files(self) -> list[str]:
        """项目上下文文件列表。子类声明各自支持的文件名。"""
        return []

    @property
    def log_raw_stdout(self) -> bool:
        """是否在解析前记录 raw stdout 行到日志。
        Codex=True（记录所有 raw lines），Claude=False（仅记录 text chunks）。
        """
        return True

    # ── 能力矩阵 ──

    @property
    def capabilities(self) -> dict:
        """能力声明 — 前端只读消费，不做适配逻辑。

        所有认证相关字段标记为待验证。
        """
        return {
            "can_detect_install": True,
            "can_detect_auth": False,       # 待验证
            "can_trigger_auth": False,      # 待验证
            "auth_method": "unknown",       # 待验证
            "plan_mode": False,
            "dangerous_mode": False,
            "stream_json": False,
            "session_resume": False,
        }

    # ── 生命周期 ──

    def check_installed(self) -> bool:
        """检查工具是否已安装。"""
        return shutil.which(self.cli_name) is not None

    @staticmethod
    def _first_nonempty_line(*outputs) -> str | None:
        for output in outputs:
            if not output:
                continue
            for line in output.splitlines():
                stripped = line.strip()
                if stripped:
                    return stripped
        return None

    def _probe_version_details(self, executable_path=None) -> tuple[str | None, str | None]:
        """探测版本并返回 (version, error)。错误文本由 probe() 消费。"""
        cmd = [executable_path or self.cli_name, "--version"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError:
            return None, f"未找到 '{self.cli_name}' 命令"
        except subprocess.TimeoutExpired:
            return None, f"'{self.cli_name} --version' 执行超时"

        if proc.returncode != 0:
            detail = self._first_nonempty_line(proc.stderr, proc.stdout)
            if detail:
                return None, f"'{self.cli_name} --version' 失败: {detail}"
            return None, f"'{self.cli_name} --version' 失败 (code {proc.returncode})"

        version = self._first_nonempty_line(proc.stdout, proc.stderr)
        if version:
            return version, None
        return None, f"'{self.cli_name} --version' 未返回版本信息"

    def check_version(self, executable_path=None) -> str | None:
        """尝试获取工具版本。失败返回 None，不抛异常。"""
        version, _ = self._probe_version_details(executable_path)
        return version

    def probe(self):
        """启动时探测工具路径和版本，结果缓存到实例上。"""
        self._probed_path = None
        self._probed_version = None
        self._probe_error = None
        self._probed_at = datetime.now().isoformat()

        try:
            executable_path = shutil.which(self.cli_name)
            self._probed_path = executable_path
            if executable_path is None:
                self._probe_error = f"未找到 '{self.cli_name}' 命令"
                return

            self._probed_version, self._probe_error = self._probe_version_details(executable_path)
        except Exception as e:
            self._probe_error = f"探测失败: {e}"

    def probe_snapshot(self) -> dict:
        """返回当前 adapter 持有的启动探测快照。"""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "agent_name": self.agent_name,
            "detected_installed": self._probed_path is not None,
            "executable_path": self._probed_path,
            "version": self._probed_version,
            "probe_error": self._probe_error,
            "last_checked_at": self._probed_at,
            "capabilities": self.capabilities,
        }

    # ── 抽象钩子（子类必须实现） ──

    @abstractmethod
    def build_command(self, prompt: str, cwd: str, **kwargs) -> list[str]:
        """构建 CLI 命令行参数列表。

        kwargs 可包含:
          continue_session: bool — 是否续接会话
          bypass_permissions: bool — 是否跳过权限
          session_id: str — 会话 ID
          resume_last: bool — 是否恢复上次会话 (Codex)
        """
        ...

    @abstractmethod
    def parse_stream_line(self, line: str) -> dict | None:
        """解析一行流输出，返回归一化事件或 None。

        返回格式:
          {"type": "text_chunk", "text": "..."}
          {"type": "block_stop"}
          {"type": "message", "text": "..."}
          {"type": "command_start", "command": "..."}
          {"type": "command_output", "output": "..."}
          {"type": "result", "text": "..."}
          {"type": "debug_sample", "key": "...", "raw": "..."}
          None — 忽略该行
        """
        ...

    # ── 可选钩子（子类可覆盖） ──

    def get_env_overrides(self) -> dict | None:
        """返回子进程额外环境变量，默认 None。"""
        return None

    def extract_result(self, stream_display: list[str], result_text: str) -> str:
        """从流数据计算最终输出。默认优先 result_text，否则拼接 stream_display。"""
        return result_text or "".join(stream_display).strip()

    def format_process_error(self, returncode: int, log_file) -> str:
        """非零退出错误消息。子类可覆盖以保留原始措辞。"""
        return f"{self.display_name} CLI 错误 (code {returncode})"

    def format_not_found_error(self) -> str:
        """可执行文件未找到错误消息。子类可覆盖以保留原始措辞。"""
        return f"未找到 '{self.cli_name}' 命令"

    # ── 协议检测 ──

    def detect_approval(self, text: str) -> bool:
        """检测审查结果是否为 APPROVED。默认委托 protocol.is_approved()。"""
        from bridge.protocol import is_approved
        return is_approved(text)

    def detect_closure(self, text: str) -> bool:
        """检测执行后审查结果是否为"任务收口成功"。

        默认实现: 首行含"任务收口成功"。
        """
        if not text:
            return False
        return "任务收口成功" in text.split("\n")[0]

    # ── 共享工具 ──

    @staticmethod
    def stderr_reader(proc, agent, log_file, log_lock, sess):
        """后台线程：逐行读取 stderr，推送到过程日志，防止 pipe buffer 填满导致死锁。"""
        MCP_NOISE = ("mcp:", "mcp_", "starting mcp", "mcp server", "mcp startup",
                     "mcp client", "handshaking", "initialize response")
        try:
            for line in proc.stderr:
                stripped = line.rstrip('\n')
                if not stripped:
                    continue
                with log_lock:
                    log_file.write(f"[stderr] {line}")
                    log_file.flush()
                is_mcp = any(p in stripped.lower() for p in MCP_NOISE)
                if is_mcp:
                    add_event(sess, "agent_stderr", {"agent": agent, "text": stripped, "is_mcp": True})
                else:
                    add_event(sess, "agent_chunk", {"agent": agent, "text": stripped + "\n"})
        except ValueError:
            pass  # pipe closed

    # ── 进程生命周期 (Template Method) ──

    def run(self, prompt, cwd, sess, log_tag=None, agent_label=None, **kwargs):
        """完整 CLI 调用生命周期。子类可重写添加前/后处理（如 plan 检测）。

        agent_label: 覆盖事件中的 agent 名称。高层 role caller 传 "planner"/"reviewer"
                     控制前端面板路由。低层直接调用不传，使用 self.agent_name。
        """
        agent = agent_label or self.agent_name
        log_tag = log_tag or agent
        cmd = self.build_command(prompt, cwd, **kwargs)
        log_file = sess.log_dir / f"{log_tag}.log"
        add_event(sess, "cli_start", {"agent": agent, "round": sess.current_round})

        try:
            env = dict(os.environ)
            overrides = self.get_env_overrides()
            if overrides:
                env.update(overrides)

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=cwd, bufsize=1, env=env,
                start_new_session=True,
            )
            with sess.proc_lock:
                sess.active_proc = proc
                try:
                    sess.active_pgid = os.getpgid(proc.pid)
                except (ProcessLookupError, PermissionError):
                    sess.active_pgid = proc.pid

            stream_display = []
            result_text = ""

            with open(log_file, "a", encoding="utf-8") as lf:
                header = f"\n{'═'*60}\n[Round {sess.current_round}] {agent.capitalize()} — {datetime.now().strftime('%H:%M:%S')}\n{'═'*60}\n"
                lf.write(header)
                lf.flush()

                log_lock = threading.Lock()
                stderr_t = threading.Thread(
                    target=self.stderr_reader,
                    args=(proc, agent, lf, log_lock, sess), daemon=True)
                stderr_t.start()

                for raw_line in proc.stdout:
                    stripped = raw_line.strip()
                    if not stripped:
                        continue

                    # Codex: 解析前记录 raw line（保留原始行为）
                    if self.log_raw_stdout:
                        with log_lock:
                            lf.write(raw_line)
                            lf.flush()

                    norm = self.parse_stream_line(stripped)
                    if norm is None:
                        continue

                    ntype = norm["type"]

                    if ntype == "text_chunk":
                        chunk = norm["text"]
                        stream_display.append(chunk)
                        if not self.log_raw_stdout:  # Claude: 只写 text chunks
                            with log_lock:
                                lf.write(chunk)
                                lf.flush()
                        add_event(sess, "agent_chunk", {"agent": agent, "text": chunk})

                    elif ntype == "block_stop":
                        if stream_display and not stream_display[-1].endswith("\n"):
                            stream_display.append("\n")
                            if not self.log_raw_stdout:
                                with log_lock:
                                    lf.write("\n")
                                    lf.flush()
                            add_event(sess, "agent_chunk", {"agent": agent, "text": "\n"})

                    elif ntype == "message":
                        text = norm["text"]
                        result_text = text
                        add_event(sess, "agent_chunk", {"agent": agent, "text": text + "\n"})

                    elif ntype == "command_start":
                        add_event(sess, "agent_chunk", {
                            "agent": agent, "text": f"$ {norm['command']}\n",
                            "chunk_type": "command"})

                    elif ntype == "command_output":
                        add_event(sess, "agent_chunk", {
                            "agent": agent, "text": norm["output"],
                            "chunk_type": "command_output"})
                        add_event(sess, "chunk_boundary", {
                            "agent": agent, "boundary_type": "command_output"})

                    elif ntype == "result":
                        result_text = norm["text"]

                    elif ntype == "debug_sample":
                        # STREAM_DEBUG 按事件类型采样，保留原始行为
                        _sc = getattr(sess, '_sample_counts', None)
                        if _sc is None:
                            _sc = {}
                            sess._sample_counts = _sc
                        ek = norm["key"]
                        cnt = _sc.get(ek, 0)
                        if cnt < 5:
                            _sc[ek] = cnt + 1
                            with log_lock:
                                lf.write(f"[SAMPLE] {norm['raw']}\n")
                                lf.flush()

            proc.wait()
            stderr_t.join(timeout=5)
            with sess.proc_lock:
                sess.active_proc = None
                pgid = sess.active_pgid
                if pgid is not None:
                    try:
                        os.killpg(pgid, 0)
                    except (ProcessLookupError, PermissionError):
                        sess.active_pgid = None

            if sess.stop_flag.is_set():
                output = self.extract_result(stream_display, result_text)
                return output or "(已中止)"

            output = self.extract_result(stream_display, result_text)

            if not output and proc.returncode != 0:
                raise RuntimeError(self.format_process_error(proc.returncode, log_file))

            if output:
                add_event(sess, "agent_result", {"agent": agent, "text": output})

            return output

        except FileNotFoundError:
            raise RuntimeError(self.format_not_found_error())
