#!/usr/bin/env python3
"""
Claude ↔ Codex 协商桥梁 v3
============================
核心改动:
  1. 多会话并发 — 每个浏览器 Tab 独立协商，互不干扰
  2. Plan 文件可靠关联 — 快照差集方案，不依赖时间戳排序
  3. 结构化输出 — Claude stream-json / Codex --json JSONL
  4. stderr 并发读取 — 防死锁 + MCP 噪音隔离
  5. 双 Tab 面板 — 过程日志 / 最终结果分离

用法:
  python3 bridge.py                            # Web UI 模式 (默认)
  python3 bridge.py --port 9090                # 自定义端口
"""

import http.server
import socketserver
import json
import threading
import subprocess
import sys
import os
import argparse
import urllib.parse
import shutil
import uuid
from datetime import datetime
from pathlib import Path

# ═════════════════════════════════════════════════════════════════
# Prompt Configuration (全局共享)
# ═════════════════════════════════════════════════════════════════
PROMPTS_FILE = Path(__file__).parent / "prompts.json"

def load_prompts():
    if PROMPTS_FILE.exists():
        return json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    return {}

def save_prompts(data):
    PROMPTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

prompt_config = load_prompts()

# ═════════════════════════════════════════════════════════════════
# Session State — 每个协商会话独立
# ═════════════════════════════════════════════════════════════════
LOG_DIR = Path("/tmp/bridge-logs")

sessions = {}           # session_id → SessionState
sessions_lock = threading.Lock()


class SessionState:
    def __init__(self, session_id, task, project_path, max_rounds):
        self.session_id = session_id
        self.task = task
        self.project_path = project_path
        self.max_rounds = max_rounds
        self.status = "running"
        self.current_round = 0
        self.history = []
        self.consensus = False
        self.consensus_round = 0
        self.execution_result = None
        self.error = None
        # 事件流（每会话独立）
        self.events = []
        self.event_lock = threading.Lock()
        # 进程控制
        self.stop_flag = threading.Event()
        self.claude_has_session = False
        self.codex_has_session = False
        self.active_proc = None
        # 日志目录（每会话独立）
        self.log_dir = LOG_DIR / session_id
        self.log_dir.mkdir(parents=True, exist_ok=True)


def get_session(sid):
    """按 session_id 查找会话，不存在返回 None。"""
    with sessions_lock:
        return sessions.get(sid)


def add_event(sess, etype, data):
    with sess.event_lock:
        sess.events.append({
            "id": len(sess.events), "type": etype,
            "data": data, "ts": datetime.now().isoformat(),
        })


# ═════════════════════════════════════════════════════════════════
# Plan 文件可靠关联 — 快照差集
# ═════════════════════════════════════════════════════════════════
def _snapshot_plan_files():
    """快照 ~/.claude/plans/ 下所有 .md 文件，返回 {path: mtime}。"""
    plans_dir = Path.home() / ".claude" / "plans"
    if not plans_dir.exists():
        return {}
    return {p: p.stat().st_mtime for p in plans_dir.glob("*.md")}


def _find_new_plan_file(before_snapshot):
    """对比快照，找到新增或修改的 plan 文件内容。"""
    after = _snapshot_plan_files()
    new_files = []
    for path, mtime in after.items():
        if path not in before_snapshot or mtime > before_snapshot[path]:
            new_files.append(path)
    if not new_files:
        return ""
    newest = max(new_files, key=lambda p: p.stat().st_mtime)
    try:
        return newest.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ═════════════════════════════════════════════════════════════════
# CLI Wrappers — 流式输出，逐行写日志
# ═════════════════════════════════════════════════════════════════
def _stderr_reader(proc, agent, log_file, log_lock, sess):
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


def call_claude_streaming(prompt, cwd, sess, continue_session=False,
                          bypass_permissions=False, log_tag="claude"):
    """
    调用 Claude Code CLI，用 stream-json 逐 token 流式输出。
    协商阶段: --permission-mode plan / 执行阶段: --dangerously-skip-permissions
    """
    cmd = ["claude"]
    if continue_session:
        cmd.append("-c")
    cmd.extend(["-p", "--verbose", "--output-format", "stream-json", "--include-partial-messages"])
    if bypass_permissions:
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd.extend(["--permission-mode", "plan"])
    cmd.append(prompt)

    log_file = sess.log_dir / f"{log_tag}.log"
    add_event(sess, "cli_start", {"agent": "claude", "round": sess.current_round})

    # Plan 文件快照（Popen 前）
    plan_snapshot = _snapshot_plan_files()

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, bufsize=1,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONINTERACTIVE_WARNING": "1"},
        )
        sess.active_proc = proc

        stream_display = []
        result_text = ""

        with open(log_file, "a", encoding="utf-8") as lf:
            header = f"\n{'═'*60}\n[Round {sess.current_round}] Claude — {datetime.now().strftime('%H:%M:%S')}\n{'═'*60}\n"
            lf.write(header)
            lf.flush()

            log_lock = threading.Lock()
            stderr_t = threading.Thread(
                target=_stderr_reader, args=(proc, "claude", lf, log_lock, sess), daemon=True)
            stderr_t.start()

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = evt.get("type", "")

                if etype == "stream_event":
                    inner = evt.get("event", {})
                    inner_type = inner.get("type", "")
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        chunk = delta.get("text", "")
                        if chunk:
                            stream_display.append(chunk)
                            with log_lock:
                                lf.write(chunk)
                                lf.flush()
                            add_event(sess, "agent_chunk", {"agent": "claude", "text": chunk})
                    elif inner_type == "content_block_stop":
                        stream_display.append("\n")
                        with log_lock:
                            lf.write("\n")
                            lf.flush()
                        add_event(sess, "agent_chunk", {"agent": "claude", "text": "\n"})

                elif etype == "result":
                    result_text = evt.get("result", "")

        proc.wait()
        stderr_t.join(timeout=5)
        sess.active_proc = None

        if sess.stop_flag.is_set():
            return result_text or "".join(stream_display).strip() or "(已中止)"

        # 优先级: plan 文件(快照差集) > result 事件 > stream 文本
        plan_content = _find_new_plan_file(plan_snapshot)
        output = plan_content or result_text or "".join(stream_display).strip()

        if not output:
            if proc.returncode != 0:
                raise RuntimeError(f"Claude CLI 错误 (code {proc.returncode})")

        if output:
            add_event(sess, "agent_result", {"agent": "claude", "text": output})

        return output

    except FileNotFoundError:
        raise RuntimeError("未找到 'claude' 命令。请安装: npm install -g @anthropic-ai/claude-code")
    except subprocess.TimeoutExpired:
        proc.kill()
        sess.active_proc = None
        raise RuntimeError("Claude CLI 超时")


def call_codex_streaming(prompt, cwd, sess, resume_last=False, log_tag="codex"):
    """
    调用 Codex CLI (--json JSONL 模式)，实时解析事件流。
    """
    cmd = ["codex"]
    if resume_last:
        cmd.extend(["exec", "--json", "resume", "--last", prompt])
    else:
        cmd.extend(["exec", "--json", prompt])

    log_file = sess.log_dir / f"{log_tag}.log"
    add_event(sess, "cli_start", {"agent": "codex", "round": sess.current_round})

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, bufsize=1,
        )
        sess.active_proc = proc

        agent_messages = []
        with open(log_file, "a", encoding="utf-8") as lf:
            header = f"\n{'═'*60}\n[Round {sess.current_round}] Codex — {datetime.now().strftime('%H:%M:%S')}\n{'═'*60}\n"
            lf.write(header)
            lf.flush()

            log_lock = threading.Lock()
            stderr_t = threading.Thread(
                target=_stderr_reader, args=(proc, "codex", lf, log_lock, sess), daemon=True)
            stderr_t.start()

            for line in proc.stdout:
                raw = line.strip()
                if not raw:
                    continue
                with log_lock:
                    lf.write(line)
                    lf.flush()

                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    add_event(sess, "agent_chunk", {"agent": "codex", "text": line})
                    continue

                etype = evt.get("type", "")
                item = evt.get("item", {})
                item_type = item.get("type", "")

                if etype == "item.completed" and item_type == "agent_message":
                    text = item.get("text", "")
                    if text:
                        agent_messages.append(text)
                        add_event(sess, "agent_chunk", {"agent": "codex", "text": text + "\n"})

                elif etype == "item.started" and item_type == "command_execution":
                    cmd_str = item.get("command", "")
                    if cmd_str:
                        add_event(sess, "agent_chunk", {"agent": "codex", "text": f"$ {cmd_str}\n"})

                elif etype == "item.completed" and item_type == "command_execution":
                    cmd_output = item.get("aggregated_output", "")
                    if cmd_output:
                        display = cmd_output if len(cmd_output) <= 2000 else cmd_output[:2000] + "\n...(truncated)\n"
                        add_event(sess, "agent_chunk", {"agent": "codex", "text": display})

        proc.wait()
        stderr_t.join(timeout=5)
        sess.active_proc = None

        result_text = agent_messages[-1] if agent_messages else ""
        output = result_text

        if sess.stop_flag.is_set():
            return output or "(已中止)"

        if proc.returncode != 0 and not output:
            raise RuntimeError(f"Codex CLI 错误 (code {proc.returncode})")

        if output:
            add_event(sess, "agent_result", {"agent": "codex", "text": output})

        return output

    except FileNotFoundError:
        raise RuntimeError("未找到 'codex' 命令。请安装: npm install -g @openai/codex")
    except subprocess.TimeoutExpired:
        proc.kill()
        sess.active_proc = None
        raise RuntimeError("Codex CLI 超时")


# ═════════════════════════════════════════════════════════════════
# Prompt Templates
# ═════════════════════════════════════════════════════════════════
def detect_claude_md(cwd):
    p = Path(cwd) / "CLAUDE.md"
    if p.exists():
        content = p.read_text(encoding="utf-8")[:2000]
        return f"\n\n## 项目开发规范 (CLAUDE.md)\n{content}"
    return ""


def build_claude_first_prompt(task, cwd):
    claude_md = detect_claude_md(cwd)
    tpl = prompt_config.get("claude_first", "## 任务\n{task}")
    body = tpl.format(task=task)
    return f"{claude_md}\n\n{body}"


def collect_user_injects(history):
    injects = []
    for h in reversed(history):
        if h["role"] == "user":
            injects.append(h["content"])
        else:
            break
    injects.reverse()
    return injects


def build_claude_revise_prompt(codex_feedback, user_injects=None):
    inject_section = ""
    if user_injects:
        joined = "\n".join(f"- {m}" for m in user_injects)
        label = prompt_config.get("user_inject_label_claude", "用户补充的约束和意见（必须优先考虑）")
        inject_section = f"\n\n## {label}\n{joined}"
    tpl = prompt_config.get("claude_revise",
        "以上是你之前的方案。\n\n## 审查者反馈\n{codex_feedback}{inject_section}\n\n请修订方案。")
    return tpl.format(codex_feedback=codex_feedback, inject_section=inject_section)


def build_codex_first_prompt(task, claude_plan):
    tpl = prompt_config.get("codex_first",
        "对于以下方案有什么看法？\n\n## 原始任务\n{task}\n\n## Claude 的方案\n{claude_plan}")
    return tpl.format(task=task, claude_plan=claude_plan)


def build_codex_review_prompt(claude_revision, user_injects=None):
    inject_section = ""
    if user_injects:
        joined = "\n".join(f"- {m}" for m in user_injects)
        label = prompt_config.get("user_inject_label_codex", "用户补充的约束和意见（审查时必须考虑）")
        inject_section = f"\n\n## {label}\n{joined}"
    tpl = prompt_config.get("codex_review",
        "Claude 修订了方案。\n\n## Claude 的修订方案\n{claude_revision}{inject_section}")
    return tpl.format(claude_revision=claude_revision, inject_section=inject_section)


def build_execution_prompt(task):
    tpl = prompt_config.get("execution",
        "以上方案已获得 APPROVED。请执行所有代码修改。\n\n原始任务: {task}")
    return tpl.format(task=task)


# ═════════════════════════════════════════════════════════════════
# Orchestration Engine
# ═════════════════════════════════════════════════════════════════
def is_approved(text):
    return "APPROVED" in text.strip().split("\n")[0].upper()


def run_negotiation(sess):
    task = sess.task
    cwd = sess.project_path
    max_rounds = sess.max_rounds

    try:
        sess.status = "running"
        add_event(sess, "status_change", {"status": "running", "msg": "协商开始"})

        for rnd in range(1, max_rounds + 1):
            if sess.stop_flag.is_set():
                sess.status = "idle"
                add_event(sess, "status_change", {"status": "stopped", "msg": "用户中止"})
                return

            sess.current_round = rnd
            add_event(sess, "round_start", {"round": rnd, "max": max_rounds})

            # ── A) Claude 出方案 / 修订 ─────────────────────
            add_event(sess, "agent_thinking", {"agent": "claude", "round": rnd})

            if rnd == 1:
                prompt_c = build_claude_first_prompt(task, cwd)
            else:
                last_codex = ""
                for h in reversed(sess.history):
                    if h["role"] == "codex":
                        last_codex = h["content"]
                        break
                user_injects = collect_user_injects(sess.history)
                prompt_c = build_claude_revise_prompt(last_codex, user_injects)

            plan = call_claude_streaming(
                prompt_c, cwd, sess,
                continue_session=sess.claude_has_session,
            )
            sess.claude_has_session = True

            entry_c = {
                "round": rnd, "role": "claude", "phase": "方案",
                "content": plan, "timestamp": datetime.now().isoformat(),
            }
            sess.history.append(entry_c)
            add_event(sess, "agent_response", entry_c)

            if sess.stop_flag.is_set():
                return

            # ── B) Codex 审查 ───────────────────────────────
            add_event(sess, "agent_thinking", {"agent": "codex", "round": rnd})

            if rnd == 1:
                prompt_x = build_codex_first_prompt(task, plan)
            else:
                user_injects_x = collect_user_injects(sess.history)
                prompt_x = build_codex_review_prompt(plan, user_injects_x)

            review = call_codex_streaming(
                prompt_x, cwd, sess,
                resume_last=sess.codex_has_session,
            )
            sess.codex_has_session = True

            entry_x = {
                "round": rnd, "role": "codex", "phase": "审查",
                "content": review, "timestamp": datetime.now().isoformat(),
            }
            sess.history.append(entry_x)
            add_event(sess, "agent_response", entry_x)

            # ── C) 共识? ───────────────────────────────────
            if is_approved(review):
                sess.consensus = True
                sess.consensus_round = rnd
                sess.status = "consensus"
                add_event(sess, "consensus_reached", {
                    "round": rnd,
                    "msg": f"Codex 在第 {rnd} 轮认可了方案，等待你确认执行。",
                })
                return

        sess.status = "consensus"
        add_event(sess, "max_rounds_reached", {
            "round": max_rounds,
            "msg": f"已完成 {max_rounds} 轮协商，可选择执行当前方案。",
        })

    except Exception as e:
        sess.status = "error"
        sess.error = str(e)
        add_event(sess, "error", {"msg": str(e)})


def run_execution(sess):
    try:
        sess.status = "executing"
        add_event(sess, "status_change", {"status": "executing", "msg": "Claude 正在执行..."})

        prompt = build_execution_prompt(sess.task)

        result = call_claude_streaming(
            prompt, sess.project_path, sess,
            continue_session=sess.claude_has_session,
            bypass_permissions=True,
            log_tag="claude",
        )

        sess.execution_result = result
        sess.status = "done"
        add_event(sess, "execution_done", {"result": result})

    except Exception as e:
        sess.status = "error"
        sess.error = str(e)
        add_event(sess, "error", {"msg": str(e)})


# ═════════════════════════════════════════════════════════════════
# HTTP Server
# ═════════════════════════════════════════════════════════════════
class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class BridgeHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *a):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _get_session_from_qs(self, qs):
        sid = qs.get("sid", [None])[0]
        return get_session(sid) if sid else None

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        if p.path == "/":
            body = HTML_UI.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p.path == "/api/events":
            sess = self._get_session_from_qs(qs)
            if not sess:
                return self._json({"events": [], "next": 0})
            since = int(qs.get("since", ["0"])[0])
            with sess.event_lock:
                new = list(sess.events[since:])
            self._json({"events": new, "next": len(sess.events)})
        elif p.path == "/api/state":
            sess = self._get_session_from_qs(qs)
            if not sess:
                return self._json({"status": "idle", "round": 0, "max_rounds": 5,
                                    "consensus": False, "consensus_round": 0,
                                    "history_len": 0, "error": None})
            self._json({
                "status": sess.status,
                "round": sess.current_round,
                "max_rounds": sess.max_rounds,
                "consensus": sess.consensus,
                "consensus_round": sess.consensus_round,
                "history_len": len(sess.history),
                "error": sess.error,
            })
        elif p.path == "/api/sessions":
            with sessions_lock:
                listing = [{
                    "session_id": s.session_id,
                    "task": s.task[:80],
                    "project_path": s.project_path,
                    "status": s.status,
                    "round": s.current_round,
                    "max_rounds": s.max_rounds,
                } for s in sessions.values()]
            self._json({"sessions": listing})
        elif p.path == "/api/prompts":
            self._json(prompt_config)
        else:
            self.send_error(404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/start":
            body = self._body()
            task = body.get("task", "").strip()
            project = body.get("project_path", "").strip()
            rounds = int(body.get("max_rounds", 5))
            if not task:
                return self._json({"error": "请输入任务描述"}, 400)
            if not project or not os.path.isdir(project):
                return self._json({"error": f"项目路径无效: {project}"}, 400)
            sid = uuid.uuid4().hex[:8]
            sess = SessionState(sid, task, project, rounds)
            with sessions_lock:
                sessions[sid] = sess
            threading.Thread(target=run_negotiation, args=(sess,), daemon=True).start()
            self._json({"ok": True, "session_id": sid})
        elif p.path == "/api/execute":
            body = self._body()
            sess = get_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            if sess.status != "consensus":
                return self._json({"error": "当前不在共识状态"}, 400)
            threading.Thread(target=run_execution, args=(sess,), daemon=True).start()
            self._json({"ok": True})
        elif p.path == "/api/stop":
            body = self._body()
            sess = get_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            sess.stop_flag.set()
            if sess.active_proc and sess.active_proc.poll() is None:
                try:
                    sess.active_proc.kill()
                except Exception:
                    pass
            sess.status = "idle"
            add_event(sess, "status_change", {"status": "stopped", "msg": "用户中止"})
            self._json({"ok": True})
        elif p.path == "/api/prompts":
            global prompt_config
            body = self._body()
            prompt_config.update(body)
            save_prompts(prompt_config)
            self._json({"ok": True})
        elif p.path == "/api/inject":
            body = self._body()
            sess = get_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            msg = body.get("message", "").strip()
            if not msg:
                return self._json({"error": "消息不能为空"}, 400)
            entry = {
                "round": sess.current_round, "role": "user",
                "phase": "人工干预", "content": msg,
                "timestamp": datetime.now().isoformat(),
            }
            sess.history.append(entry)
            add_event(sess, "agent_response", entry)
            self._json({"ok": True})
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ═════════════════════════════════════════════════════════════════
# HTML UI — 双 Tab 面板 + 多会话支持
# ═════════════════════════════════════════════════════════════════
HTML_UI = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude ↔ Codex Bridge</title>
<style>
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--claude:#7c5cfc;--codex:#10a37f;--user:#d29922;--approve:#3fb950;--danger:#f85149}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'SF Mono','Fira Code','Menlo',monospace;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ── 控制栏 ── */
.controls{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;flex-shrink:0}
.controls .field{display:flex;flex-direction:column;gap:3px}
.controls label{font-size:11px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.5px;font-family:-apple-system,sans-serif}
.controls input,.controls textarea{background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:6px 10px;font-size:13px;font-family:inherit;outline:none}
.controls input:focus,.controls textarea:focus{border-color:var(--claude)}
.controls .f-path{flex:0 0 260px}
.controls .f-task{flex:1 1 300px}
.controls .f-task textarea{min-height:80px;max-height:200px;resize:vertical}
.controls .f-rounds{flex:0 0 60px}
.controls .f-rounds input{width:60px;text-align:center}

.btn{padding:6px 14px;border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:600;font-family:-apple-system,sans-serif;transition:all .15s}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-go{background:var(--claude);color:#fff}
.btn-stop{background:var(--danger);color:#fff}
.btn-exec{background:var(--approve);color:#000}

.status-bar{display:flex;align-items:center;gap:12px;margin-left:auto;font-family:-apple-system,sans-serif}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600;text-transform:uppercase}
.pill-idle{background:var(--border);color:var(--dim)}
.pill-running{background:rgba(124,92,252,.2);color:var(--claude)}
.pill-consensus{background:rgba(63,185,80,.2);color:var(--approve)}
.pill-executing{background:rgba(210,153,34,.2);color:var(--user)}
.pill-done{background:rgba(63,185,80,.3);color:var(--approve)}
.pill-error{background:rgba(248,81,73,.2);color:var(--danger)}
.round-info{font-size:12px;color:var(--dim)}

/* ── 双面板 ── */
.panels{flex:1;display:flex;overflow:hidden;border-bottom:1px solid var(--border)}
.panel{flex:1;display:flex;flex-direction:column;overflow:hidden}
.panel+.panel{border-left:1px solid var(--border)}
.panel-head{background:var(--surface);padding:8px 14px;font-size:13px;font-weight:700;border-bottom:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;gap:8px;font-family:-apple-system,sans-serif}
.panel-head .dot{width:10px;height:10px;border-radius:50%}
.dot-claude{background:var(--claude)} .dot-codex{background:var(--codex)}
.tab-group{margin-left:auto;display:flex;gap:2px}
.tab{background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--dim);padding:2px 10px;font-size:11px;cursor:pointer;font-family:-apple-system,sans-serif;font-weight:600}
.tab.active{background:var(--border);color:var(--text)}
.tab-body{flex:1;position:relative;overflow:hidden}
.tab-pane{position:absolute;inset:0;overflow-y:auto;padding:12px 14px;font-size:13px;line-height:1.6;white-space:pre-wrap;word-wrap:break-word;color:#c9d1d9;display:none}
.tab-pane.active{display:block}
.mcp-line{color:#484f58;font-size:11px}
.term{flex:1;overflow-y:auto;padding:12px 14px;font-size:13px;line-height:1.6;white-space:pre-wrap;word-wrap:break-word;color:#c9d1d9}
.term .sys{color:var(--dim);font-style:italic}
.term .err{color:var(--danger)}
.term .ok{color:var(--approve);font-weight:700}

/* ── 注入栏 ── */
.inject{background:var(--surface);border-top:1px solid var(--border);padding:8px 16px;display:flex;gap:8px;flex-shrink:0}
.inject input{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:6px 10px;font-size:13px;font-family:inherit;outline:none}
.inject input:focus{border-color:var(--user)}
.inject input::placeholder{color:var(--dim)}
.btn-inj{background:var(--user);color:#000}

/* ── 设置按钮 ── */
.btn-cfg{background:transparent;color:var(--dim);border:1px solid var(--border);font-size:13px;padding:6px 10px}
.btn-cfg:hover{color:var(--text);border-color:var(--text)}

/* ── 设置弹窗 ── */
.modal-mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;justify-content:center;align-items:center}
.modal-mask.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:8px;width:760px;max-width:92vw;max-height:88vh;display:flex;flex-direction:column}
.modal-hdr{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border);font-family:-apple-system,sans-serif;font-weight:700;font-size:15px}
.modal-hdr .close{cursor:pointer;color:var(--dim);font-size:20px;background:none;border:none}
.modal-hdr .close:hover{color:var(--text)}
.modal-body{flex:1;overflow-y:auto;padding:18px}
.modal-body .cfg-field{margin-bottom:16px}
.modal-body .cfg-field label{display:block;font-size:12px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px;font-family:-apple-system,sans-serif}
.modal-body .cfg-field .cfg-hint{font-size:11px;color:var(--dim);margin-bottom:4px;font-family:-apple-system,sans-serif}
.modal-body textarea{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:8px 10px;font-size:12px;font-family:inherit;outline:none;resize:vertical;min-height:90px;line-height:1.5}
.modal-body textarea:focus{border-color:var(--claude)}
.modal-foot{padding:12px 18px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px}
.btn-save{background:var(--claude);color:#fff}
.btn-cancel{background:var(--border);color:var(--text)}

::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>

<!-- 控制栏 -->
<div class="controls">
  <div class="field f-path"><label>项目路径</label><input type="text" id="inp_path"></div>
  <div class="field f-task"><label>任务描述</label><textarea id="inp_task" rows="3" placeholder="描述任务..."></textarea></div>
  <div class="field f-rounds"><label>轮次</label><input type="number" id="inp_rounds" value="5" min="1" max="20"></div>
  <button class="btn btn-go" id="btn_go" onclick="doStart()">▶ 开始</button>
  <button class="btn btn-stop" id="btn_stop" onclick="doStop()" disabled>⏹ 中止</button>
  <button class="btn btn-exec" id="btn_exec" onclick="doExec()" disabled>⚡ 执行</button>
  <button class="btn btn-cfg" onclick="openCfg()">⚙ 提示词</button>
  <div class="status-bar">
    <span class="pill pill-idle" id="pill">IDLE</span>
    <span class="round-info" id="rinfo"></span>
  </div>
</div>

<!-- 双终端面板 -->
<div class="panels">
  <div class="panel">
    <div class="panel-head">
      <span class="dot dot-claude"></span> Claude Code
      <div class="tab-group">
        <button class="tab active" data-agent="claude" data-tab="log" onclick="switchTab('claude','log')">过程</button>
        <button class="tab" data-agent="claude" data-tab="result" onclick="switchTab('claude','result')">结果</button>
      </div>
    </div>
    <div class="tab-body">
      <div class="term tab-pane active" id="log_claude"></div>
      <div class="term tab-pane" id="result_claude"></div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-head">
      <span class="dot dot-codex"></span> Codex
      <div class="tab-group">
        <button class="tab active" data-agent="codex" data-tab="log" onclick="switchTab('codex','log')">过程</button>
        <button class="tab" data-agent="codex" data-tab="result" onclick="switchTab('codex','result')">结果</button>
      </div>
    </div>
    <div class="tab-body">
      <div class="term tab-pane active" id="log_codex"></div>
      <div class="term tab-pane" id="result_codex"></div>
    </div>
  </div>
</div>

<!-- 注入栏 -->
<div class="inject">
  <input type="text" id="inp_inject" placeholder="协商中插入你的意见..." onkeydown="if(event.key==='Enter')doInject()">
  <button class="btn btn-inj" onclick="doInject()">发送</button>
</div>

<!-- 提示词配置弹窗 -->
<div class="modal-mask" id="cfgModal">
  <div class="modal">
    <div class="modal-hdr">
      <span>提示词配置</span>
      <button class="close" onclick="closeCfg()">&times;</button>
    </div>
    <div class="modal-body">
      <div class="cfg-field">
        <label>Claude 初始方案提示词</label>
        <div class="cfg-hint">变量: {task} — 第1轮，Claude 根据此提示生成方案</div>
        <textarea id="cfg_claude_first" rows="6"></textarea>
      </div>
      <div class="cfg-field">
        <label>Claude 修订提示词</label>
        <div class="cfg-hint">变量: {codex_feedback} {inject_section} — 第2+轮，Claude 根据反馈修订</div>
        <textarea id="cfg_claude_revise" rows="6"></textarea>
      </div>
      <div class="cfg-field">
        <label>Codex 首次审查提示词</label>
        <div class="cfg-hint">变量: {task} {claude_plan} — 第1轮，Codex 审查方案</div>
        <textarea id="cfg_codex_first" rows="6"></textarea>
      </div>
      <div class="cfg-field">
        <label>Codex 继续审查提示词</label>
        <div class="cfg-hint">变量: {claude_revision} {inject_section} — 第2+轮，Codex 继续审查</div>
        <textarea id="cfg_codex_review" rows="6"></textarea>
      </div>
      <div class="cfg-field">
        <label>执行提示词</label>
        <div class="cfg-hint">变量: {task} — 达成共识后 Claude 执行方案</div>
        <textarea id="cfg_execution" rows="4"></textarea>
      </div>
      <div class="cfg-field">
        <label>用户干预标签 (Claude)</label>
        <div class="cfg-hint">注入用户意见时在 Claude 提示中显示的标题</div>
        <textarea id="cfg_user_inject_label_claude" rows="1"></textarea>
      </div>
      <div class="cfg-field">
        <label>用户干预标签 (Codex)</label>
        <div class="cfg-hint">注入用户意见时在 Codex 提示中显示的标题</div>
        <textarea id="cfg_user_inject_label_codex" rows="1"></textarea>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-cancel" onclick="closeCfg()">取消</button>
      <button class="btn btn-save" onclick="saveCfg()">保存</button>
    </div>
  </div>
</div>

<script>
let sid=null,cursor=0,poll=null,st='idle';

async function api(m,u,b){
  const o={method:m,headers:{'Content-Type':'application/json'}};
  if(b)o.body=JSON.stringify(b);return(await fetch(u,o)).json();
}

// ── Actions ──
async function doStart(){
  const path=document.getElementById('inp_path').value.trim();
  const task=document.getElementById('inp_task').value.trim();
  const rounds=parseInt(document.getElementById('inp_rounds').value)||5;
  if(!path||!task){alert('请填写项目路径和任务描述');return;}
  const r=await api('POST','/api/start',{project_path:path,task,max_rounds:rounds});
  if(r.error){alert(r.error);return;}
  sid=r.session_id;
  // 写入 URL 以便刷新恢复
  const u=new URL(location);u.searchParams.set('sid',sid);
  if(u.searchParams.has('project'))u.searchParams.delete('project');
  history.replaceState(null,'',u);
  ['claude','codex'].forEach(a=>{
    document.getElementById('log_'+a).innerHTML='';
    document.getElementById('result_'+a).innerHTML='';
    switchTab(a,'log');
  });
  cursor=0; st='idle';
  if(poll)clearInterval(poll);
  poll=setInterval(pollEvt,300);
}
async function doStop(){
  if(!sid)return;
  await api('POST','/api/stop',{session_id:sid});
}
async function doExec(){
  if(!sid)return;
  if(!confirm('确认执行？Claude 将用 --dangerously-skip-permissions'))return;
  await api('POST','/api/execute',{session_id:sid});
}
async function doInject(){
  if(!sid)return;
  const i=document.getElementById('inp_inject');
  if(!i.value.trim())return;
  await api('POST','/api/inject',{session_id:sid,message:i.value.trim()});
  appendLog('claude','<span class="sys">[你] '+esc(i.value.trim())+'</span>\n');
  appendLog('codex','<span class="sys">[你] '+esc(i.value.trim())+'</span>\n');
  i.value='';
}

// ── Polling ──
async function pollEvt(){
  if(!sid)return;
  try{
    const r=await api('GET','/api/events?sid='+sid+'&since='+cursor);
    if(r.events)for(const e of r.events)handle(e);
    cursor=r.next;
    const s=await api('GET','/api/state?sid='+sid);
    updSt(s.status,s.round,s.max_rounds);
  }catch(e){}
}

function handle(e){
  switch(e.type){
    case 'round_start':
      const hdr='\n══════ 第 '+e.data.round+' / '+e.data.max+' 轮 ══════\n';
      appendLog('claude','<span class="sys">'+hdr+'</span>');
      appendLog('codex','<span class="sys">'+hdr+'</span>');
      break;
    case 'agent_thinking':
      const who=e.data.agent;
      switchTab(who,'log');
      appendLog(who,'<span class="sys">'+(who==='claude'?'[Claude 分析中...]':'[Codex 审查中...]')+'</span>\n');
      break;
    case 'agent_chunk':
      appendLog(e.data.agent, esc(e.data.text));
      break;
    case 'agent_stderr':
      if(e.data.is_mcp){
        appendLog(e.data.agent,'<span class="mcp-line">[MCP] '+esc(e.data.text)+'</span>\n');
      }
      break;
    case 'agent_result':
      appendResult(e.data.agent, esc(e.data.text));
      switchTab(e.data.agent,'result');
      break;
    case 'agent_response':
      const ag=e.data.role==='claude'?'claude':'codex';
      appendLog(ag,'\n<span class="sys">── '+e.data.phase+' 完成 (R'+e.data.round+') ──</span>\n');
      if(e.data.content){
        const rel=document.getElementById('result_'+ag);
        if(rel) rel.innerHTML='<span class="ok">── R'+e.data.round+' '+e.data.phase+' ──</span>\n'+esc(e.data.content);
      }
      switchTab(ag,'result');
      if(e.data.role==='claude'){
        appendLog('codex','\n<span class="sys">── Claude R'+e.data.round+' 方案已发送给 Codex ──</span>\n');
      }else{
        appendLog('claude','\n<span class="sys">── Codex R'+e.data.round+' 审查意见已发送给 Claude ──</span>\n');
      }
      break;
    case 'consensus_reached':
      const ok='\n✓ '+e.data.msg+'\n';
      appendLog('claude','<span class="ok">'+ok+'</span>');
      appendLog('codex','<span class="ok">'+ok+'</span>');
      break;
    case 'max_rounds_reached':
      appendLog('claude','<span class="sys">\n⚠ '+e.data.msg+'</span>\n');
      appendLog('codex','<span class="sys">\n⚠ '+e.data.msg+'</span>\n');
      break;
    case 'execution_done':
      appendLog('claude','\n<span class="ok">══════ 执行完成 ══════</span>\n');
      appendResult('claude', esc(e.data.result));
      switchTab('claude','result');
      break;
    case 'error':
      appendLog('claude','<span class="err">\n❌ '+esc(e.data.msg)+'</span>\n');
      appendLog('codex','<span class="err">\n❌ '+esc(e.data.msg)+'</span>\n');
      break;
    case 'status_change':
      if(e.data.status==='stopped'){
        appendLog('claude','<span class="sys">\n⏹ 已中止</span>\n');
        appendLog('codex','<span class="sys">\n⏹ 已中止</span>\n');
      }
      break;
  }
}

function switchTab(agent, tab){
  document.querySelectorAll('.tab[data-agent="'+agent+'"]').forEach(t=>t.classList.toggle('active',t.dataset.tab===tab));
  document.getElementById('log_'+agent).classList.toggle('active',tab==='log');
  document.getElementById('result_'+agent).classList.toggle('active',tab==='result');
}

function appendLog(agent, html){
  const el=document.getElementById('log_'+agent);
  if(!el)return;
  el.innerHTML+=html;
  el.scrollTop=el.scrollHeight;
}

function appendResult(agent, html){
  const el=document.getElementById('result_'+agent);
  if(!el)return;
  el.innerHTML+=html;
  el.scrollTop=el.scrollHeight;
}

function updSt(s,r,m){
  if(s===st)return; st=s;
  const p=document.getElementById('pill');
  p.className='pill pill-'+s;
  p.textContent={idle:'IDLE',running:'NEGOTIATING',consensus:'CONSENSUS',executing:'EXECUTING',done:'DONE',error:'ERROR'}[s]||s;
  document.getElementById('rinfo').textContent=s==='idle'?'':'R'+r+'/'+m;
  document.getElementById('btn_go').disabled=!['idle','done','error'].includes(s);
  document.getElementById('btn_stop').disabled=s!=='running'&&s!=='executing';
  document.getElementById('btn_exec').disabled=s!=='consensus';
  if(['idle','done','error'].includes(s)&&poll){clearInterval(poll);poll=null;}
}

function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// ── 提示词配置 ──
const cfgKeys=['claude_first','claude_revise','codex_first','codex_review','execution','user_inject_label_claude','user_inject_label_codex'];

async function openCfg(){
  const data=await api('GET','/api/prompts');
  cfgKeys.forEach(k=>{
    const el=document.getElementById('cfg_'+k);
    if(el)el.value=data[k]||'';
  });
  document.getElementById('cfgModal').classList.add('open');
}
function closeCfg(){document.getElementById('cfgModal').classList.remove('open');}

async function saveCfg(){
  const body={};
  cfgKeys.forEach(k=>{
    const el=document.getElementById('cfg_'+k);
    if(el)body[k]=el.value;
  });
  const r=await api('POST','/api/prompts',body);
  if(r.ok){closeCfg();}else{alert(r.error||'保存失败');}
}

// ── 初始化：从 URL 恢复会话 ──
(function(){
  const p=new URLSearchParams(location.search);
  if(p.get('project'))document.getElementById('inp_path').value=p.get('project');
  if(p.get('sid')){
    sid=p.get('sid');
    cursor=0; st='idle';
    poll=setInterval(pollEvt,300);
  }
})();
</script>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Claude ↔ Codex 协商桥梁")
    parser.add_argument("--port", type=int, default=8686)
    parser.add_argument("--project", type=str, help="预设项目路径")
    args = parser.parse_args()

    project_note = ""
    if args.project:
        project_note = f"?project={urllib.parse.quote(args.project)}"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadedHTTPServer(("0.0.0.0", args.port), BridgeHandler)

    print(f"""
╔═══════════════════════════════════════════════╗
║  Claude ↔ Codex Bridge  v3                    ║
║  http://localhost:{args.port}/{project_note:<28}║
║  多 Tab 并发协商 · Ctrl+C 退出               ║
╚═══════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
        server.shutdown()


if __name__ == "__main__":
    main()
