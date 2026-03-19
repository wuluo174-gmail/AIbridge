#!/usr/bin/env python3
"""
Claude ↔ Codex 协商桥梁 v2
============================
核心改动:
  1. 提示词风格 — 参考用户实际使用的提示词，引用 CLAUDE.md，第一性原理，不逢迎讨好
  2. 不重复灌历史 — 依赖 -c / resume --last 的会话连续性，只传对方最新的增量内容
  3. 实时流式输出 — Popen 逐行读取 + 写日志文件，Web UI 和 tmux 都能实时看
  4. 双终端视图  — tmux 左 Claude 右 Codex 下控制台，Web UI 作为备选

用法:
  python3 bridge.py                            # Web UI 模式 (默认)
  python3 bridge.py --tmux                     # tmux 双窗格模式
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
import time
from datetime import datetime
from pathlib import Path

# ═════════════════════════════════════════════════════════════════
# Prompt Configuration
# ═════════════════════════════════════════════════════════════════
PROMPTS_FILE = Path(__file__).parent / "prompts.json"

def load_prompts():
    """从 prompts.json 加载提示词配置。"""
    if PROMPTS_FILE.exists():
        return json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    return {}

def save_prompts(data):
    """保存提示词配置到 prompts.json。"""
    PROMPTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

prompt_config = load_prompts()

# ═════════════════════════════════════════════════════════════════
# Global State
# ═════════════════════════════════════════════════════════════════
LOG_DIR = Path("/tmp/bridge-logs")

state = {
    "status": "idle",
    "task": "",
    "project_path": "",
    "max_rounds": 5,
    "current_round": 0,
    "history": [],
    "consensus": False,
    "consensus_round": 0,
    "execution_result": None,
    "error": None,
}

events = []
event_lock = threading.Lock()
stop_flag = threading.Event()
claude_has_session = False
codex_has_session = False
active_proc = None  # 当前正在运行的子进程，中止时直接 kill


def add_event(etype, data):
    with event_lock:
        events.append({
            "id": len(events), "type": etype,
            "data": data, "ts": datetime.now().isoformat(),
        })


def reset_state():
    global events, claude_has_session, codex_has_session
    state.update({
        "status": "idle", "task": "", "current_round": 0,
        "history": [], "consensus": False, "consensus_round": 0,
        "execution_result": None, "error": None,
    })
    with event_lock:
        events.clear()
    stop_flag.clear()
    claude_has_session = False
    codex_has_session = False
    # 清空日志目录
    if LOG_DIR.exists():
        shutil.rmtree(LOG_DIR)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


# ═════════════════════════════════════════════════════════════════
# CLI Wrappers — 流式输出，逐行写日志
# ═════════════════════════════════════════════════════════════════
_last_plan_mtime = 0  # 上次读到的 plan 文件修改时间


def _read_latest_plan_file():
    """
    读取 ~/.claude/plans/ 下最新的 .md 文件。
    Claude Code 在 --permission-mode plan 时会生成计划文件到这里。
    每次文件名不同（如 optimized-marinating-badger.md），所以按修改时间取最新的。
    只返回本次协商期间新产生的文件（比 _last_plan_mtime 更新的）。
    """
    global _last_plan_mtime
    plans_dir = Path.home() / ".claude" / "plans"
    if not plans_dir.exists():
        return ""
    plan_files = sorted(plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not plan_files:
        return ""
    newest = plan_files[0]
    mtime = newest.stat().st_mtime
    if mtime <= _last_plan_mtime:
        return ""  # 不是新文件
    _last_plan_mtime = mtime
    try:
        return newest.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def call_claude_streaming(prompt, cwd, continue_session=False,
                          bypass_permissions=False, log_tag="claude"):
    """
    调用 Claude Code CLI，用 stream-json 逐 token 流式输出。
    从 NDJSON 流中提取 assistant:text delta，实时推送到 Web UI。

    协商阶段: --permission-mode plan (原生计划模式，架构层禁止写入)
    执行阶段: --dangerously-skip-permissions (绕过所有权限确认)
    """
    cmd = ["claude"]
    if continue_session:
        cmd.append("-c")
    cmd.extend(["-p", "--verbose", "--output-format", "stream-json", "--include-partial-messages"])
    if bypass_permissions:
        cmd.append("--dangerously-skip-permissions")
    else:
        # 协商阶段用原生 plan 模式，从架构层面禁止文件修改和命令执行
        cmd.extend(["--permission-mode", "plan"])
    cmd.append(prompt)

    global active_proc
    log_file = LOG_DIR / f"{log_tag}.log"
    add_event("cli_start", {"agent": "claude", "round": state["current_round"]})

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, bufsize=1,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONINTERACTIVE_WARNING": "1"},
        )
        active_proc = proc

        stream_display = []   # 所有 text_delta（用于实时显示，含思考过程）
        result_text = ""      # result 事件的最终干净文本（只含方案，传给对方）

        with open(log_file, "a", encoding="utf-8") as lf:
            header = f"\n{'═'*60}\n[Round {state['current_round']}] Claude — {datetime.now().strftime('%H:%M:%S')}\n{'═'*60}\n"
            lf.write(header)
            lf.flush()

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = evt.get("type", "")

                # 实时流：提取 text_delta 用于显示
                if etype == "stream_event":
                    inner = evt.get("event", {})
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        chunk = delta.get("text", "")
                        if chunk:
                            stream_display.append(chunk)
                            lf.write(chunk)
                            lf.flush()
                            add_event("agent_chunk", {"agent": "claude", "text": chunk})

                # 最终结果：干净的方案文本，这才是传给 Codex 的内容
                elif etype == "result":
                    result_text = evt.get("result", "")

        proc.wait()
        active_proc = None

        if stop_flag.is_set():
            return result_text or "".join(stream_display).strip() or "(已中止)"

        # 优先级: plan 文件(plan模式产物) > result 事件 > stream 文本
        plan_content = _read_latest_plan_file()
        output = plan_content or result_text or "".join(stream_display).strip()

        if not output:
            stderr_out = proc.stderr.read().strip() if proc.stderr else ""
            if proc.returncode != 0:
                raise RuntimeError(f"Claude CLI 错误 (code {proc.returncode}): {stderr_out[:500]}")

        return output

    except FileNotFoundError:
        raise RuntimeError("未找到 'claude' 命令。请安装: npm install -g @anthropic-ai/claude-code")
    except subprocess.TimeoutExpired:
        proc.kill()
        active_proc = None
        raise RuntimeError("Claude CLI 超时")


def call_codex_streaming(prompt, cwd, resume_last=False, log_tag="codex"):
    """
    调用 Codex CLI，实时逐行输出到日志文件。
    codex exec 非交互模式本身就不写文件（无 TTY 则 approval 自动降级为 never）。
    """
    cmd = ["codex"]
    if resume_last:
        cmd.extend(["exec", "resume", "--last", prompt])
    else:
        cmd.extend(["exec", prompt])

    global active_proc
    log_file = LOG_DIR / f"{log_tag}.log"
    add_event("cli_start", {"agent": "codex", "round": state["current_round"]})

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, bufsize=1,
        )
        active_proc = proc

        lines = []
        with open(log_file, "a", encoding="utf-8") as lf:
            header = f"\n{'═'*60}\n[Round {state['current_round']}] Codex — {datetime.now().strftime('%H:%M:%S')}\n{'═'*60}\n"
            lf.write(header)
            lf.flush()

            for line in proc.stdout:
                lf.write(line)
                lf.flush()
                lines.append(line)
                add_event("agent_chunk", {"agent": "codex", "text": line})

        proc.wait()
        active_proc = None
        output = "".join(lines).strip()

        if stop_flag.is_set():
            return output or "(已中止)"

        if proc.returncode != 0 and not output:
            raise RuntimeError(f"Codex CLI 错误 (code {proc.returncode})")
        return output

    except FileNotFoundError:
        raise RuntimeError("未找到 'codex' 命令。请安装: npm install -g @openai/codex")
    except subprocess.TimeoutExpired:
        proc.kill()
        active_proc = None
        raise RuntimeError("Codex CLI 超时")


# ═════════════════════════════════════════════════════════════════
# Prompt Templates — 参考用户实际提示词风格
# ═════════════════════════════════════════════════════════════════
def detect_claude_md(cwd):
    """检测项目中是否有 CLAUDE.md，有则读取摘要。"""
    p = Path(cwd) / "CLAUDE.md"
    if p.exists():
        content = p.read_text(encoding="utf-8")[:2000]  # 取前 2000 字符
        return f"\n\n## 项目开发规范 (CLAUDE.md)\n{content}"
    return ""


def build_claude_first_prompt(task, cwd):
    """第 1 轮：Claude 初始方案。带 CLAUDE.md 上下文。"""
    claude_md = detect_claude_md(cwd)
    tpl = prompt_config.get("claude_first", "## 任务\n{task}")
    body = tpl.format(task=task)
    return f"{claude_md}\n\n{body}"


def collect_user_injects(history):
    """收集上一轮 agent 回复之后、本轮开始之前，用户插入的所有意见。"""
    injects = []
    for h in reversed(history):
        if h["role"] == "user":
            injects.append(h["content"])
        else:
            break  # 遇到非 user 的就停，只取最近连续的 inject
    injects.reverse()
    return injects


def build_claude_revise_prompt(codex_feedback, user_injects=None):
    """第 2+ 轮：Claude 修订。只传 Codex 最新反馈 + 用户干预（如有）。"""
    inject_section = ""
    if user_injects:
        joined = "\n".join(f"- {m}" for m in user_injects)
        label = prompt_config.get("user_inject_label_claude", "用户补充的约束和意见（必须优先考虑）")
        inject_section = f"\n\n## {label}\n{joined}"

    tpl = prompt_config.get("claude_revise",
        "以上是你之前的方案。\n\n## 审查者反馈\n{codex_feedback}{inject_section}\n\n请修订方案。")
    return tpl.format(codex_feedback=codex_feedback, inject_section=inject_section)


def build_codex_first_prompt(task, claude_plan):
    """第 1 轮：Codex 首次审查。传任务 + Claude 方案。"""
    tpl = prompt_config.get("codex_first",
        "对于以下方案有什么看法？\n\n## 原始任务\n{task}\n\n## Claude 的方案\n{claude_plan}")
    return tpl.format(task=task, claude_plan=claude_plan)


def build_codex_review_prompt(claude_revision, user_injects=None):
    """第 2+ 轮：Codex 继续审查。只传 Claude 最新修订 + 用户干预（如有）。"""
    inject_section = ""
    if user_injects:
        joined = "\n".join(f"- {m}" for m in user_injects)
        label = prompt_config.get("user_inject_label_codex", "用户补充的约束和意见（审查时必须考虑）")
        inject_section = f"\n\n## {label}\n{joined}"

    tpl = prompt_config.get("codex_review",
        "Claude 修订了方案。\n\n## Claude 的修订方案\n{claude_revision}{inject_section}")
    return tpl.format(claude_revision=claude_revision, inject_section=inject_section)


def build_execution_prompt(task):
    """最终执行：续接 Claude 对话，方案已在上下文中。"""
    tpl = prompt_config.get("execution",
        "以上方案已获得 APPROVED。请执行所有代码修改。\n\n原始任务: {task}")
    return tpl.format(task=task)


# ═════════════════════════════════════════════════════════════════
# Orchestration Engine
# ═════════════════════════════════════════════════════════════════
def is_approved(text):
    return "APPROVED" in text.strip().split("\n")[0].upper()


def run_negotiation():
    global claude_has_session, codex_has_session

    task = state["task"]
    cwd = state["project_path"]
    max_rounds = state["max_rounds"]

    try:
        state["status"] = "running"
        add_event("status_change", {"status": "running", "msg": "协商开始"})

        for rnd in range(1, max_rounds + 1):
            if stop_flag.is_set():
                state["status"] = "idle"
                add_event("status_change", {"status": "stopped", "msg": "用户中止"})
                return

            state["current_round"] = rnd
            add_event("round_start", {"round": rnd, "max": max_rounds})

            # ── A) Claude 出方案 / 修订 ─────────────────────
            add_event("agent_thinking", {"agent": "claude", "round": rnd})

            if rnd == 1:
                prompt_c = build_claude_first_prompt(task, cwd)
            else:
                # 找到最近一条 Codex 的回复
                last_codex = ""
                for h in reversed(state["history"]):
                    if h["role"] == "codex":
                        last_codex = h["content"]
                        break
                # 收集用户在两轮之间插入的意见
                user_injects = collect_user_injects(state["history"])
                prompt_c = build_claude_revise_prompt(last_codex, user_injects)

            plan = call_claude_streaming(
                prompt_c, cwd,
                continue_session=claude_has_session,
            )
            claude_has_session = True

            entry_c = {
                "round": rnd, "role": "claude", "phase": "方案",
                "content": plan, "timestamp": datetime.now().isoformat(),
            }
            state["history"].append(entry_c)
            add_event("agent_response", entry_c)

            if stop_flag.is_set():
                return

            # ── B) Codex 审查 ───────────────────────────────
            add_event("agent_thinking", {"agent": "codex", "round": rnd})

            if rnd == 1:
                prompt_x = build_codex_first_prompt(task, plan)
            else:
                # 收集 Claude 回复之后、Codex 开始之前的用户注入
                user_injects_x = collect_user_injects(state["history"])
                prompt_x = build_codex_review_prompt(plan, user_injects_x)

            review = call_codex_streaming(
                prompt_x, cwd,
                resume_last=codex_has_session,
            )
            codex_has_session = True

            entry_x = {
                "round": rnd, "role": "codex", "phase": "审查",
                "content": review, "timestamp": datetime.now().isoformat(),
            }
            state["history"].append(entry_x)
            add_event("agent_response", entry_x)

            # ── C) 共识? ───────────────────────────────────
            if is_approved(review):
                state["consensus"] = True
                state["consensus_round"] = rnd
                state["status"] = "consensus"
                add_event("consensus_reached", {
                    "round": rnd,
                    "msg": f"Codex 在第 {rnd} 轮认可了方案，等待你确认执行。",
                })
                return

        state["status"] = "consensus"
        add_event("max_rounds_reached", {
            "round": max_rounds,
            "msg": f"已完成 {max_rounds} 轮协商，可选择执行当前方案。",
        })

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        add_event("error", {"msg": str(e)})


def run_execution():
    try:
        state["status"] = "executing"
        add_event("status_change", {"status": "executing", "msg": "Claude 正在执行..."})

        prompt = build_execution_prompt(state["task"])

        result = call_claude_streaming(
            prompt, state["project_path"],
            continue_session=claude_has_session,
            bypass_permissions=True,
            log_tag="claude",
        )

        state["execution_result"] = result
        state["status"] = "done"
        add_event("execution_done", {"result": result})

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        add_event("error", {"msg": str(e)})


# ═════════════════════════════════════════════════════════════════
# tmux 双窗格启动器
# ═════════════════════════════════════════════════════════════════
def launch_tmux(port, project):
    """启动 tmux 三窗格布局: 左Claude日志 | 右Codex日志 | 下Bridge控制台"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "claude.log").touch()
    (LOG_DIR / "codex.log").touch()

    session = "bridge"
    bridge_cmd = f"python3 {__file__} --port {port}"
    if project:
        bridge_cmd += f" --project {project}"

    # kill old session
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)

    cmds = [
        # 创建 session，底部运行 bridge server
        f"tmux new-session -d -s {session} -x 220 -y 55 '{bridge_cmd}'",
        # 上方分一个水平窗格
        f"tmux split-window -b -v -t {session} -p 70 ''",
        # 上方左右分
        f"tmux split-window -h -t {session}:0.0 ''",
        # 左上: Claude 日志
        f"tmux send-keys -t {session}:0.0 'echo -e \"\\033[94m═══ Claude Code 输出 ═══\\033[0m\" && tail -f {LOG_DIR}/claude.log' Enter",
        # 右上: Codex 日志
        f"tmux send-keys -t {session}:0.1 'echo -e \"\\033[92m═══ Codex 输出 ═══\\033[0m\" && tail -f {LOG_DIR}/codex.log' Enter",
        # 选中底部控制台
        f"tmux select-pane -t {session}:0.2",
        # attach
        f"tmux attach -t {session}",
    ]

    for c in cmds:
        os.system(c)
        if "attach" in c:
            break  # attach 会阻塞


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

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/":
            body = HTML_UI.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p.path == "/api/events":
            qs = urllib.parse.parse_qs(p.query)
            since = int(qs.get("since", ["0"])[0])
            with event_lock:
                new = list(events[since:])
            self._json({"events": new, "next": len(events)})
        elif p.path == "/api/state":
            self._json({
                "status": state["status"],
                "round": state["current_round"],
                "max_rounds": state["max_rounds"],
                "consensus": state["consensus"],
                "consensus_round": state["consensus_round"],
                "history_len": len(state["history"]),
                "error": state["error"],
            })
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
            reset_state()
            state["task"] = task
            state["project_path"] = project
            state["max_rounds"] = rounds
            threading.Thread(target=run_negotiation, daemon=True).start()
            self._json({"ok": True})
        elif p.path == "/api/execute":
            if state["status"] != "consensus":
                return self._json({"error": "当前不在共识状态"}, 400)
            threading.Thread(target=run_execution, daemon=True).start()
            self._json({"ok": True})
        elif p.path == "/api/stop":
            stop_flag.set()
            # 立即杀掉正在运行的 CLI 子进程
            if active_proc and active_proc.poll() is None:
                try:
                    active_proc.kill()
                except Exception:
                    pass
            state["status"] = "idle"
            add_event("status_change", {"status": "stopped", "msg": "用户中止"})
            self._json({"ok": True})
        elif p.path == "/api/prompts":
            global prompt_config
            body = self._body()
            prompt_config.update(body)
            save_prompts(prompt_config)
            self._json({"ok": True})
        elif p.path == "/api/inject":
            body = self._body()
            msg = body.get("message", "").strip()
            if not msg:
                return self._json({"error": "消息不能为空"}, 400)
            entry = {
                "round": state["current_round"], "role": "user",
                "phase": "人工干预", "content": msg,
                "timestamp": datetime.now().isoformat(),
            }
            state["history"].append(entry)
            add_event("agent_response", entry)
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
# ═════════════════════════════════════════════════════════════════
# HTML UI — 双终端面板 + 控制栏（全在浏览器内）
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
    <div class="panel-head"><span class="dot dot-claude"></span> Claude Code</div>
    <div class="term" id="term_claude"></div>
  </div>
  <div class="panel">
    <div class="panel-head"><span class="dot dot-codex"></span> Codex</div>
    <div class="term" id="term_codex"></div>
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
let cursor=0,poll=null,st='idle';

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
  document.getElementById('term_claude').innerHTML='';
  document.getElementById('term_codex').innerHTML='';
  cursor=0;
  if(poll)clearInterval(poll);
  poll=setInterval(pollEvt,300);
}
async function doStop(){await api('POST','/api/stop');}
async function doExec(){
  if(!confirm('确认执行？Claude 将用 --dangerously-skip-permissions'))return;
  await api('POST','/api/execute');
}
async function doInject(){
  const i=document.getElementById('inp_inject');
  if(!i.value.trim())return;
  await api('POST','/api/inject',{message:i.value.trim()});
  // Show in both panels
  appendTerm('claude','<span class="sys">[你] '+esc(i.value.trim())+'</span>\n');
  appendTerm('codex','<span class="sys">[你] '+esc(i.value.trim())+'</span>\n');
  i.value='';
}

// ── Polling ──
async function pollEvt(){
  try{
    const r=await api('GET','/api/events?since='+cursor);
    if(r.events)for(const e of r.events)handle(e);
    cursor=r.next;
    const s=await api('GET','/api/state');
    updSt(s.status,s.round,s.max_rounds);
  }catch(e){}
}

function handle(e){
  switch(e.type){
    case 'round_start':
      const hdr='\n══════ 第 '+e.data.round+' / '+e.data.max+' 轮 ══════\n';
      appendTerm('claude','<span class="sys">'+hdr+'</span>');
      appendTerm('codex','<span class="sys">'+hdr+'</span>');
      break;
    case 'agent_thinking':
      const who=e.data.agent;
      appendTerm(who,'<span class="sys">'+(who==='claude'?'[Claude 分析中...]':'[Codex 审查中...]')+'</span>\n');
      break;
    case 'agent_chunk':
      appendTerm(e.data.agent, esc(e.data.text));
      break;
    case 'agent_response':
      // 最终结果：在对应面板追加分隔线
      const ag=e.data.role==='claude'?'claude':'codex';
      appendTerm(ag,'\n<span class="sys">── '+e.data.phase+' 完成 (R'+e.data.round+') ──</span>\n');
      // 如果是 result 事件的干净文本，在对面面板显示摘要
      if(e.data.role==='claude'){
        appendTerm('codex','\n<span class="sys">── Claude R'+e.data.round+' 方案已发送给 Codex ──</span>\n');
      }else{
        appendTerm('claude','\n<span class="sys">── Codex R'+e.data.round+' 审查意见已发送给 Claude ──</span>\n');
      }
      break;
    case 'consensus_reached':
      const ok='\n✓ '+e.data.msg+'\n';
      appendTerm('claude','<span class="ok">'+ok+'</span>');
      appendTerm('codex','<span class="ok">'+ok+'</span>');
      break;
    case 'max_rounds_reached':
      appendTerm('claude','<span class="sys">\n⚠ '+e.data.msg+'</span>\n');
      appendTerm('codex','<span class="sys">\n⚠ '+e.data.msg+'</span>\n');
      break;
    case 'execution_done':
      appendTerm('claude','\n<span class="ok">══════ 执行完成 ══════</span>\n'+esc(e.data.result)+'\n');
      break;
    case 'error':
      appendTerm('claude','<span class="err">\n❌ '+esc(e.data.msg)+'</span>\n');
      appendTerm('codex','<span class="err">\n❌ '+esc(e.data.msg)+'</span>\n');
      break;
    case 'status_change':
      if(e.data.status==='stopped'){
        appendTerm('claude','<span class="sys">\n⏹ 已中止</span>\n');
        appendTerm('codex','<span class="sys">\n⏹ 已中止</span>\n');
      }
      break;
  }
}

function appendTerm(agent, html){
  const el=document.getElementById('term_'+agent);
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

(function(){const p=new URLSearchParams(location.search);if(p.get('project'))document.getElementById('inp_path').value=p.get('project');})();
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
║  Claude ↔ Codex Bridge                        ║
║  http://localhost:{args.port}/{project_note:<28}║
║  Ctrl+C 退出                                  ║
╚═══════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
        server.shutdown()


if __name__ == "__main__":
    main()
