"""
Bridge HTTP Server
==================
BridgeHandler, ThreadedHTTPServer, HTML UI, 以及路径浏览辅助函数。

BridgeHandler 通过 _BridgeProxy 间接引用 bridge.py 命名空间中的
可 monkey-patch 函数/常量，保持测试兼容性。
"""

import http.server
import socketserver
import json
import signal
import threading
import time
import os
import uuid
import urllib.parse
from datetime import datetime
from pathlib import Path

from bridge.session import (
    SessionState, sessions, sessions_lock,
    get_session, add_event, add_history_event,
)


# ═════════════════════════════════════════════════════════════════
# Recent Paths
# ═════════════════════════════════════════════════════════════════
_ROOT = Path(__file__).resolve().parent.parent
RECENT_PATHS_FILE = _ROOT / "recent_paths.json"


def load_recent_paths():
    """加载最近使用的项目路径列表。"""
    if RECENT_PATHS_FILE.exists():
        try:
            return json.loads(RECENT_PATHS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_recent_paths(paths):
    """保存最近使用的项目路径（失败不阻断主流程）。"""
    try:
        RECENT_PATHS_FILE.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════
# _BridgeProxy — bridge.py globals() 代理
# ═════════════════════════════════════════════════════════════════
class _BridgeProxy:
    """bridge.py 命名空间代理，支持测试 monkey-patch 穿透。

    bridge.py 执行 ``_server._b._ns = globals()`` 将自身命名空间注入。
    BridgeHandler 方法通过 ``_b.X`` 访问，运行时从 bridge.py 的 __dict__ 查找，
    测试 ``self.mod.X = fake`` 修改同一 dict，fake 自然生效。
    """
    _ns = None

    def __getattr__(self, name):
        try:
            return self._ns[name]
        except (TypeError, KeyError):
            raise AttributeError(name)


_b = _BridgeProxy()


NO_DIST_BOOTSTRAP_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bridge Frontend Not Built</title>
<style>
:root{color-scheme:dark light;--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:24px}
.card{max-width:760px;background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:28px 30px;box-shadow:0 20px 60px rgba(0,0,0,.35)}
.badge{display:inline-block;margin-bottom:14px;padding:4px 10px;border-radius:999px;background:rgba(210,153,34,.16);color:var(--warn);font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
h1{margin:0 0 10px;font-size:28px;line-height:1.2}
p{margin:0 0 12px;line-height:1.6;color:var(--dim)}
code{background:rgba(255,255,255,.06);padding:2px 6px;border-radius:6px;color:var(--text)}
</style>
</head>
<body>
  <main class="card">
    <div class="badge">Frontend Required</div>
    <h1>前端尚未构建</h1>
    <p>当前不会再回退到旧的内置 UI 快照，因为那会形成第二套前端实现并持续产生协议漂移。</p>
    <p>请在项目根目录执行 <code>cd frontend && npm run build</code>，然后刷新页面。</p>
    <p>后端 API 已启动，但浏览器入口只服务最新构建产物。</p>
  </main>
</body>
</html>"""


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
        return _b.get_or_load_session(sid) if sid else None

    def _stop_process_group(self, sess):
        sess.stop_flag.set()
        with sess.proc_lock:
            pgid = sess.active_pgid
        if not pgid:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            with sess.proc_lock:
                sess.active_pgid = None
            return

        def _stop_followup(target_pgid, target_sess):
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    os.killpg(target_pgid, 0)
                except (ProcessLookupError, PermissionError):
                    with target_sess.proc_lock:
                        target_sess.active_pgid = None
                    return
                time.sleep(0.3)
            try:
                os.killpg(target_pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            with target_sess.proc_lock:
                target_sess.active_pgid = None

        threading.Thread(
            target=_stop_followup, args=(pgid, sess), daemon=True
        ).start()

    _DIST_DIR = Path(__file__).parent.parent / "frontend" / "dist"
    _MIME_MAP = {'.js': 'text/javascript', '.css': 'text/css',
                 '.svg': 'image/svg+xml', '.png': 'image/png',
                 '.woff2': 'font/woff2', '.woff': 'font/woff'}

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        if p.path == "/":
            dist_index = self._DIST_DIR / "index.html"
            if dist_index.is_file():
                body = dist_index.read_bytes()
            else:
                body = NO_DIST_BOOTSTRAP_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p.path.startswith("/assets/"):
            filepath = (self._DIST_DIR / p.path.lstrip("/")).resolve()
            dist_resolved = str(self._DIST_DIR.resolve()) + os.sep
            if filepath.is_file() and str(filepath).startswith(dist_resolved):
                body = filepath.read_bytes()
                mime = self._MIME_MAP.get(filepath.suffix, 'application/octet-stream')
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
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
                                    "history_len": 0, "error": None,
                                    "planner_tool_id": _b._role_config.planner_tool_id,
                                    "reviewer_tool_id": _b._role_config.reviewer_tool_id,
                                    "executor_panel": "planner",
                                    "review_round": 0, "max_review_rounds": 3,
                                    "phase": "negotiation",
                                    "updated_at": None, "finished_at": None,
                                    "interrupt_reason": None, "resume_available": False})
            self._json(_b._serialize_session_state(sess))
        elif p.path == "/api/sessions":
            limit = int(qs.get("limit", ["50"])[0])
            offset = int(qs.get("offset", ["0"])[0])
            self._json({"sessions": _b.list_sessions(limit, offset)})
        elif p.path == "/api/history":
            sess = self._get_session_from_qs(qs)
            if not sess:
                return self._json({"entries": [], "execution_result": None,
                                   "review_entries": [], "review_round": 0,
                                   "review_status": None, "event_cursor": 0})
            payload = _b._session_history_payload(sess)
            self._json({
                "entries": payload["entries"],
                "execution_result": payload["execution_result"],
                "review_entries": payload["review_entries"],
                "review_round": payload["review_round"],
                "review_status": payload["review_status"],
                "event_cursor": payload["event_cursor"],
            })
        elif p.path == "/api/browse":
            raw = qs.get("path", [""])[0].strip()
            target = Path(raw).expanduser().resolve() if raw else Path.home()
            if not target.is_dir():
                return self._json({"error": f"非目录: {target}"}, 400)
            parent = str(target.parent) if target != target.parent else None
            raw_dirs = []
            truncated = False
            try:
                with os.scandir(str(target)) as it:
                    for entry in it:
                        if not entry.is_dir(follow_symlinks=True):
                            continue
                        if entry.name.startswith('.'):
                            continue
                        raw_dirs.append((entry.name, entry.path))
            except PermissionError:
                return self._json({"error": f"权限不足: {target}"}, 403)
            raw_dirs.sort(key=lambda x: x[0])
            if len(raw_dirs) > 200:
                raw_dirs = raw_dirs[:200]
                truncated = True
            dirs = [{"name": name, "path": path,
                     "is_git": os.path.isdir(os.path.join(path, ".git"))}
                    for name, path in raw_dirs]
            self._json({"current": str(target), "parent": parent, "dirs": dirs,
                        "is_git": (target / ".git").is_dir(), "truncated": truncated})
        elif p.path == "/api/complete":
            prefix = qs.get("prefix", [""])[0].strip()
            if not prefix:
                return self._json({"suggestions": []})
            target = Path(prefix).expanduser()
            if target.is_dir() and prefix.endswith(os.sep):
                parent_dir, match = str(target), ""
            else:
                parent_dir, match = str(target.parent), target.name.lower()
            suggestions = []
            if os.path.isdir(parent_dir):
                raw_dirs = []
                try:
                    with os.scandir(parent_dir) as it:
                        for entry in it:
                            if not entry.is_dir(follow_symlinks=True):
                                continue
                            if match and not entry.name.lower().startswith(match):
                                continue
                            if entry.name.startswith('.') and not match.startswith('.'):
                                continue
                            raw_dirs.append((entry.name, entry.path))
                except PermissionError:
                    pass
                raw_dirs.sort(key=lambda x: x[0])
                for name, path in raw_dirs[:15]:
                    suggestions.append({"name": name, "path": path,
                                        "is_git": os.path.isdir(os.path.join(path, ".git"))})
            self._json({"suggestions": suggestions})
        elif p.path == "/api/recent_paths":
            self._json({"paths": _b.load_recent_paths()})
        elif p.path == "/api/prompts":
            self._json(_b.prompt_config)
        elif p.path == "/api/tools":
            self._json({"tools": _b._registry.discover()})
        elif p.path == "/api/role_config":
            tools = _b._registry.discover()
            rc = _b.get_role_config()
            executor = _b._registry.resolve_executor(
                _b.RoleConfig(rc["planner_tool_id"], rc["reviewer_tool_id"]))
            self._json({
                "planner_tool_id": rc["planner_tool_id"],
                "reviewer_tool_id": rc["reviewer_tool_id"],
                "executor_tool_id": executor.id,
                "tools": tools,
            })
        else:
            self.send_error(404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/start":
            body = self._body()
            task = body.get("task", "").strip()
            project_raw = body.get("project_path", "").strip()
            rounds = int(body.get("max_rounds", 5))
            if not task:
                return self._json({"error": "请输入任务描述"}, 400)
            if not project_raw:
                return self._json({"error": "请输入项目路径"}, 400)
            project = str(Path(project_raw).expanduser().resolve())
            if not os.path.isdir(project):
                return self._json({"error": f"项目路径无效: {project_raw}"}, 400)
            sid = uuid.uuid4().hex[:8]
            sess = _b.create_session(sid, task, project, rounds)
            with sessions_lock:
                sessions[sid] = sess
            _b._persist_session(sess)
            recent = _b.load_recent_paths()
            if project in recent:
                recent.remove(project)
            recent.insert(0, project)
            _b.save_recent_paths(recent[:10])
            threading.Thread(target=_b.run_negotiation, args=(sess,), daemon=True).start()
            self._json({"ok": True, "session_id": sid})
        elif p.path == "/api/execute":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            with sess.status_lock:
                if sess.status not in _b.EXECUTABLE_STATES:
                    return self._json({"error": "当前不在可执行状态"}, 400)
                sess.status = "executing"
                sess.phase = "execution"
                sess.interrupt_reason = None
            threading.Thread(target=_b.run_execution, args=(sess,), daemon=True).start()
            self._json({"ok": True})
        elif p.path == "/api/pause":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            self._stop_process_group(sess)
            with sess.status_lock:
                sess.status = "paused"
                sess.interrupt_reason = "user_pause"
            add_event(sess, "status_change", {"status": "paused", "msg": "用户中断", "msg_key": "be.user_paused"})
            self._json({"ok": True})
        elif p.path == "/api/stop":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            self._stop_process_group(sess)
            with sess.status_lock:
                sess.status = "aborted"
                sess.interrupt_reason = "user_abort"
            add_event(sess, "status_change", {"status": "aborted", "msg": "用户中止", "msg_key": "be.user_aborted"})
            self._json({"ok": True})
        elif p.path == "/api/review_fix":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            with sess.status_lock:
                if sess.status not in _b.FIXABLE_STATES:
                    return self._json({"error": "当前不在待修复状态"}, 400)
                sess.status = "review_pending"
                sess.phase = "review"
                sess.interrupt_reason = None
            sess.stop_flag.clear()
            threading.Thread(target=_b.run_review_fix_cycle, args=(sess,), daemon=True).start()
            self._json({"ok": True})
        elif p.path == "/api/review_skip":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            with sess.status_lock:
                if sess.status not in _b.REVIEW_SKIPPABLE_STATES:
                    return self._json({"error": "当前不在待修复状态"}, 400)
                sess.status = "done"
                sess.phase = "review"
            add_event(sess, "review_done", {"round": sess.review_round, "msg": "用户跳过修复，任务结束。", "success": False, "msg_key": "be.skip_review"})
            _b._try_persist(sess)
            self._json({"ok": True})
        elif p.path == "/api/review_continue":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            extra = int(body.get("extra_rounds", 3))
            if extra < 1 or extra > 20:
                return self._json({"error": "额外轮次须在 1-20 之间"}, 400)
            with sess.status_lock:
                if sess.status not in _b.REVIEW_CONTINUABLE_STATES:
                    return self._json({"error": "当前不在审查最大轮次状态"}, 400)
                sess.status = "review_pending"
                sess.phase = "review"
                sess.interrupt_reason = None
            sess.max_review_rounds += extra
            sess.stop_flag.clear()
            add_event(sess, "status_change", {"status": "review_pending", "msg": "继续审查", "msg_key": "be.continue_review"})
            threading.Thread(target=_b.run_review_fix_cycle, args=(sess,), daemon=True).start()
            self._json({"ok": True})
        elif p.path == "/api/prompts":
            body = self._body()
            # 先构造副本并持久化，成功后再替换内存，避免脏状态
            merged = dict(_b.prompt_config)
            merged.update(body)
            _b.save_prompts(merged)
            _b.prompt_config.clear()
            _b.prompt_config.update(merged)
            self._json({"ok": True})
        elif p.path == "/api/inject":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
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
            with sess.status_lock:
                if sess.status == "consensus":
                    return self._json({"error": "共识状态下请使用「继续协商」提交驳回理由"}, 400)
                add_history_event(sess, sess.history, entry, "agent_response")
            self._json({"ok": True})
        elif p.path == "/api/continue":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            extra = int(body.get("extra_rounds", 3))
            if extra < 1 or extra > 20:
                return self._json({"error": "额外轮次须在 1-20 之间"}, 400)
            with sess.status_lock:
                cur = sess.status
            if cur == "consensus":
                reason = body.get("message", "").strip()
                if not reason:
                    return self._json({"error": "驳回共识时必须提供理由"}, 400)
            elif cur not in _b.CONTINUABLE_STATES:
                return self._json({"error": "只有在达到最大轮次或共识状态下才能继续协商"}, 400)
            with sess.status_lock:
                if sess.status != cur:
                    return self._json({"error": "状态已变更，请重试"}, 409)
                sess.status = "running"
                sess.phase = "negotiation"
                sess.interrupt_reason = None
                if cur == "consensus":
                    sess.consensus = False
                    sess.consensus_round = 0
            if cur == "consensus":
                entry = {
                    "round": sess.current_round, "role": "user",
                    "phase": "人工干预", "content": reason,
                    "timestamp": datetime.now().isoformat(),
                }
                add_history_event(sess, sess.history, entry, "agent_response")
            lcr = _b.last_complete_round(sess.history)
            start_round = lcr + 1
            sess.max_rounds = lcr + extra
            sess.stop_flag.clear()
            add_event(sess, "status_change", {"status": "running",
                      "msg": "驳回共识，继续协商" if cur == "consensus" else "继续协商",
                      "msg_key": "be.continue_rejected" if cur == "consensus" else "be.continue"})
            threading.Thread(
                target=_b.run_negotiation, args=(sess,),
                kwargs={"start_round": start_round}, daemon=True
            ).start()
            self._json({"ok": True})
        elif p.path == "/api/resume":
            body = self._body()
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            if sess.status not in ("paused", "interrupted"):
                return self._json({"error": "当前会话不可恢复"}, 400)
            _b.resume_session(sess)
            self._json({"ok": True})
        elif p.path == "/api/role_config":
            body = self._body()
            planner_id = body.get("planner_tool_id", "").strip()
            reviewer_id = body.get("reviewer_tool_id", "").strip()
            if not planner_id or not reviewer_id:
                return self._json({"error": "planner_tool_id 和 reviewer_tool_id 均为必填"}, 400)
            registered = _b._registry.list_tool_ids()
            if planner_id not in registered:
                return self._json({"error": f"未注册的工具: {planner_id}"}, 400)
            if reviewer_id not in registered:
                return self._json({"error": f"未注册的工具: {reviewer_id}"}, 400)
            for tid in [planner_id, reviewer_id]:
                if not _b._registry.get(tid).check_installed():
                    name = _b._registry.get(tid).display_name
                    return self._json({"error": f"{name} 未安装"}, 400)
            caps = [_b._registry.get(tid).capabilities for tid in [planner_id, reviewer_id]]
            if not any(c.get("dangerous_mode") for c in caps):
                return self._json({"error": "至少需要一个支持执行模式的工具"}, 400)
            with sessions_lock:
                active = [s for s in sessions.values()
                          if s.status in ("running", "executing", "review_pending")]
            if active:
                return self._json({"error": "存在进行中的会话，无法更改角色配置"}, 409)
            _b._update_role_config(planner_id, reviewer_id)
            self._json({"ok": True})
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


