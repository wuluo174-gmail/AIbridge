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
import threading
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
        elif p.path == "/api/history":
            sess = self._get_session_from_qs(qs)
            if not sess:
                return self._json({"entries": [], "execution_result": None,
                                   "review_entries": [], "review_round": 0,
                                   "review_status": None, "event_cursor": 0})
            with sess.event_lock:
                entries = [
                    {"round": h["round"], "role": h["role"], "phase": h["phase"],
                     "content": h["content"]}
                    for h in sess.history if h["role"] in ("claude", "codex")
                ]
                review_entries = [
                    {"round": h["round"], "role": h["role"], "phase": h["phase"],
                     "content": h["content"]}
                    for h in sess.review_history
                ]
                execution_result = sess.execution_result
                review_round = sess.review_round
                current_status = sess.status
                event_cursor = len(sess.events)
            review_status = None
            if current_status.startswith("review_") or review_round > 0:
                review_status = {"round": review_round, "status": current_status}
            self._json({
                "entries": entries,
                "execution_result": execution_result,
                "review_entries": review_entries,
                "review_round": review_round,
                "review_status": review_status,
                "event_cursor": event_cursor,
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
            sess = SessionState(sid, task, project, rounds)
            with sessions_lock:
                sessions[sid] = sess
            recent = _b.load_recent_paths()
            if project in recent:
                recent.remove(project)
            recent.insert(0, project)
            _b.save_recent_paths(recent[:10])
            threading.Thread(target=_b.run_negotiation, args=(sess,), daemon=True).start()
            self._json({"ok": True, "session_id": sid})
        elif p.path == "/api/execute":
            body = self._body()
            sess = get_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            with sess.status_lock:
                if sess.status not in _b.EXECUTABLE_STATES:
                    return self._json({"error": "当前不在可执行状态"}, 400)
                sess.status = "executing"
            threading.Thread(target=_b.run_execution, args=(sess,), daemon=True).start()
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
            with sess.status_lock:
                sess.status = "idle"
            add_event(sess, "status_change", {"status": "stopped", "msg": "用户中止"})
            self._json({"ok": True})
        elif p.path == "/api/review_fix":
            body = self._body()
            sess = get_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            with sess.status_lock:
                if sess.status not in _b.FIXABLE_STATES:
                    return self._json({"error": "当前不在待修复状态"}, 400)
                sess.status = "review_pending"
            sess.stop_flag.clear()
            threading.Thread(target=_b.run_review_fix_cycle, args=(sess,), daemon=True).start()
            self._json({"ok": True})
        elif p.path == "/api/review_skip":
            body = self._body()
            sess = get_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            with sess.status_lock:
                if sess.status not in _b.FIXABLE_STATES:
                    return self._json({"error": "当前不在待修复状态"}, 400)
                sess.status = "done"
            add_event(sess, "review_done", {"round": sess.review_round, "msg": "用户跳过修复，任务结束。", "success": False})
            self._json({"ok": True})
        elif p.path == "/api/prompts":
            body = self._body()
            _b.prompt_config.update(body)
            _b.save_prompts(_b.prompt_config)
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
            with sess.status_lock:
                if sess.status == "consensus":
                    return self._json({"error": "共识状态下请使用「继续协商」提交驳回理由"}, 400)
                add_history_event(sess, sess.history, entry, "agent_response")
            self._json({"ok": True})
        elif p.path == "/api/continue":
            body = self._body()
            sess = get_session(body.get("session_id"))
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
                      "msg": "驳回共识，继续协商" if cur == "consensus" else "继续协商"})
            threading.Thread(
                target=_b.run_negotiation, args=(sess,),
                kwargs={"start_round": start_round}, daemon=True
            ).start()
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
.controls .f-path{flex:0 0 320px}
.controls .f-task{flex:1 1 300px}
.controls .f-task textarea{min-height:80px;max-height:200px;resize:vertical}
.controls .f-rounds{flex:0 0 60px}
.controls .f-rounds input{width:60px;text-align:center}

.btn{padding:6px 14px;border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:600;font-family:-apple-system,sans-serif;transition:all .15s}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-go{background:var(--claude);color:#fff}
.btn-stop{background:var(--danger);color:#fff}
.btn-exec{background:var(--approve);color:#000}
.btn-cont{background:#e67e22;color:#fff}
.plan-preview{margin:4px 0 8px 0}
.plan-preview summary{cursor:pointer;color:var(--claude);font-size:12px;font-weight:600;user-select:none}
.plan-preview summary:hover{text-decoration:underline}
.plan-preview .plan-body{margin-top:6px;padding:8px 12px;background:rgba(124,92,252,.06);border-left:3px solid var(--claude);border-radius:0 4px 4px 0;font-size:12px;line-height:1.5;max-height:400px;overflow-y:auto;color:#c9d1d9;white-space:pre-wrap;word-wrap:break-word}

.status-bar{display:flex;align-items:center;gap:12px;margin-left:auto;font-family:-apple-system,sans-serif}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600;text-transform:uppercase}
.pill-idle{background:var(--border);color:var(--dim)}
.pill-running{background:rgba(124,92,252,.2);color:var(--claude)}
.pill-consensus{background:rgba(63,185,80,.2);color:var(--approve)}
.pill-executing{background:rgba(210,153,34,.2);color:var(--user)}
.pill-done{background:rgba(63,185,80,.3);color:var(--approve)}
.pill-error{background:rgba(248,81,73,.2);color:var(--danger)}
.pill-max_rounds{background:rgba(230,126,34,.2);color:#e67e22}
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
.term .sys,.sys{color:var(--dim);font-style:italic}
.term .err,.err{color:var(--danger)}
.term .ok,.ok{color:var(--approve);font-weight:700}
.log-sep{display:block;margin:6px 0;padding:2px 0}
.done-badge{background:rgba(63,185,80,.2);color:var(--approve);font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600;font-family:-apple-system,sans-serif;margin-left:8px}
.chunk-cmd{color:#79c0ff}
.chunk-fold{display:block;margin:4px 0;border-left:2px solid var(--border);padding-left:8px}
.chunk-fold summary{cursor:pointer;color:var(--dim);font-size:12px;font-weight:600;user-select:none}
.chunk-fold .fold-body{font-size:12px;color:#8b949e;max-height:300px;overflow-y:auto;white-space:pre-wrap;word-wrap:break-word}
.pill-review_pending{background:rgba(124,92,252,.2);color:var(--claude)}
.pill-review_fix{background:rgba(210,153,34,.2);color:var(--user)}
.btn-fix{background:var(--claude);color:#fff}
.btn-skip{background:var(--border);color:var(--dim)}

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

/* ── 版本历史 ── */
.result-wrap{display:flex;flex-direction:column;height:100%}
.ver-bar{display:flex;gap:2px;padding:4px 14px;border-bottom:1px solid var(--border);flex-wrap:wrap;flex-shrink:0}
.ver-bar:empty{display:none}
.ver-content{flex:1;overflow-y:auto;min-height:0;padding:12px 14px;font-size:13px;line-height:1.6;white-space:pre-wrap;word-wrap:break-word;color:#c9d1d9}
.ver-tab{background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--dim);padding:2px 8px;font-size:11px;cursor:pointer;font-family:-apple-system,sans-serif;font-weight:600;transition:all .15s}
.ver-tab:hover{border-color:var(--text);color:var(--text)}
.ver-tab.active{background:var(--claude);color:#fff;border-color:var(--claude)}
.ver-tab.vt-exec{border-color:var(--approve)}
.ver-tab.vt-exec.active{background:var(--approve);color:#000;border-color:var(--approve)}

/* ── 路径选择器 ── */
.path-wrap{position:relative;display:flex;gap:0}
.path-wrap input{flex:1;border-radius:4px 0 0 4px}
.btn-browse{background:var(--border);color:var(--text);border:1px solid var(--border);border-left:none;border-radius:0 4px 4px 0;padding:6px 8px;font-size:14px;cursor:pointer}
.btn-browse:hover{background:var(--claude);color:#fff}
.path-dropdown{display:none;position:absolute;top:100%;left:0;right:0;background:var(--surface);border:1px solid var(--border);border-top:none;border-radius:0 0 4px 4px;max-height:220px;overflow-y:auto;z-index:50}
.path-dropdown.open{display:block}
.pd-item{padding:6px 10px;cursor:pointer;font-size:12px;display:flex;align-items:center;gap:6px}
.pd-item:hover{background:var(--border)}
.pd-git{color:var(--approve);font-size:10px;font-weight:600}
.pd-section{padding:4px 10px;font-size:10px;color:var(--dim);text-transform:uppercase;font-weight:600}
.browse-bar{display:flex;gap:6px;padding:10px 14px;border-bottom:1px solid var(--border)}
.browse-bar input{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:6px 10px;font-size:12px;font-family:inherit;outline:none}
.browse-item{padding:8px 14px;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:8px;border-bottom:1px solid rgba(255,255,255,.03)}
.browse-item:hover{background:rgba(124,92,252,.08)}
.browse-item.selected{background:rgba(124,92,252,.15);border-left:3px solid var(--claude)}
.browse-item.bi-parent{color:var(--dim);font-style:italic}
.bi-icon{font-size:14px;flex-shrink:0}
.bi-name{flex:1}
.bi-git{color:var(--approve);font-size:10px;font-weight:600}

</style>
</head>
<body>

<!-- 控制栏 -->
<div class="controls">
  <div class="field f-path">
    <label>项目路径</label>
    <div class="path-wrap">
      <input type="text" id="inp_path" autocomplete="off" placeholder="输入路径或点击浏览...">
      <button class="btn btn-browse" id="btn_browse" title="浏览文件夹">📂</button>
      <div class="path-dropdown" id="pathDropdown"></div>
    </div>
  </div>
  <div class="field f-task"><label>任务描述</label><textarea id="inp_task" rows="3" placeholder="描述任务..."></textarea></div>
  <div class="field f-rounds"><label>轮次</label><input type="number" id="inp_rounds" value="5" min="1" max="20"></div>
  <button class="btn btn-go" id="btn_go" onclick="doStart()">▶ 开始</button>
  <button class="btn btn-stop" id="btn_stop" onclick="doStop()" disabled>⏹ 中止</button>
  <button class="btn btn-exec" id="btn_exec" onclick="doExec()" disabled>⚡ 执行</button>
  <button class="btn btn-cont" id="btn_cont" onclick="doContinue()" disabled style="display:none">继续协商</button>
  <button class="btn btn-fix" id="btn_fix" onclick="doReviewFix()" disabled style="display:none">🔧 确认修复</button>
  <button class="btn btn-skip" id="btn_skip" onclick="doReviewSkip()" disabled style="display:none">⏭ 跳过修复</button>
  <input type="number" id="inp_extra" value="3" min="1" max="20" title="额外轮次" style="display:none;width:50px;text-align:center;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:6px;font-size:13px">
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
      <div class="tab-pane" id="result_claude_wrap">
        <div class="result-wrap">
          <div class="ver-bar" id="ver_bar_claude"></div>
          <div class="ver-content" id="result_claude"></div>
        </div>
      </div>
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
      <div class="tab-pane" id="result_codex_wrap">
        <div class="result-wrap">
          <div class="ver-bar" id="ver_bar_codex"></div>
          <div class="ver-content" id="result_codex"></div>
        </div>
      </div>
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
        <label>执行提示词 (APPROVED)</label>
        <div class="cfg-hint">变量: {task} {plan_section} — Codex APPROVED 后 Claude 执行方案</div>
        <textarea id="cfg_execution" rows="4"></textarea>
      </div>
      <div class="cfg-field">
        <label>执行提示词 (未 APPROVED)</label>
        <div class="cfg-hint">变量: {task} {plan_section} — 达到最大轮次但未 APPROVED 时执行</div>
        <textarea id="cfg_execution_unapproved" rows="4"></textarea>
      </div>
      <div class="cfg-field">
        <label>执行后审查提示词 (Codex)</label>
        <div class="cfg-hint">变量: {task} {approved_plan} {execution_result} {diff_section}</div>
        <textarea id="cfg_codex_post_review" rows="6"></textarea>
      </div>
      <div class="cfg-field">
        <label>修复提示词 (Claude)</label>
        <div class="cfg-hint">变量: {review_feedback}</div>
        <textarea id="cfg_claude_post_fix" rows="4"></textarea>
      </div>
      <div class="cfg-field">
        <label>再审查提示词 (Codex)</label>
        <div class="cfg-hint">变量: {fix_result} {diff_section}</div>
        <textarea id="cfg_codex_post_review_followup" rows="4"></textarea>
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

<!-- 文件夹浏览弹窗 -->
<div class="modal-mask" id="browseModal">
  <div class="modal">
    <div class="modal-hdr">
      <span>选择项目文件夹</span>
      <button class="close" id="browse_close">&times;</button>
    </div>
    <div class="modal-body" style="padding:0">
      <div class="browse-bar">
        <input type="text" id="browse_path_input" placeholder="输入路径直接跳转...">
        <button class="btn" id="browse_go_btn">前往</button>
      </div>
      <div id="browse_list" style="overflow-y:auto;max-height:55vh"></div>
    </div>
    <div class="modal-foot">
      <div id="browse_current" style="flex:1;font-size:12px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>
      <button class="btn btn-cancel" id="browse_cancel">取消</button>
      <button class="btn btn-save" id="browse_select">选择此文件夹</button>
    </div>
  </div>
</div>

<script>
let sid=null,cursor=0,poll=null,st='idle';

// 版本缓存 — 纯前端状态，从事件流派生
var versions = {claude: [], codex: []};
var activeVer = {claude: -1, codex: -1}; // -1 = 跟随最新
var executionResult = null;
var showExecResult = false;

function renderVersionedResult(agent) {
  var bar = document.getElementById('ver_bar_' + agent);
  var el = document.getElementById('result_' + agent);
  if (!bar || !el) return;
  var vers = versions[agent];
  if (!vers.length && !(agent === 'claude' && executionResult != null)) {
    bar.innerHTML = '';
    el.innerHTML = '';
    return;
  }
  var idx = activeVer[agent] < 0 ? vers.length - 1 : Math.min(activeVer[agent], vers.length - 1);
  var tabs = '';
  vers.forEach(function(ver, i) {
    var cls = (!showExecResult || agent !== 'claude') && i === idx ? 'ver-tab active' : 'ver-tab';
    tabs += '<button class="' + cls + '" data-ver-agent="' + agent + '" data-ver-idx="' + i + '">v' + (i+1) + ' (R' + ver.round + ')</button>';
  });
  if (agent === 'claude' && executionResult != null) {
    tabs += '<button class="ver-tab vt-exec' + (showExecResult ? ' active' : '') + '" data-ver-agent="claude" data-ver-idx="-2">执行结果</button>';
  }
  bar.innerHTML = tabs;
  if (agent === 'claude' && showExecResult && executionResult != null) {
    el.innerHTML = '<span class="ok">── 执行结果 ──</span>\n' + esc(executionResult);
  } else if (vers.length) {
    var v = vers[idx];
    el.innerHTML = '<span class="ok">── R' + v.round + ' ' + v.phase + ' ──</span>\n' + esc(v.content);
  }
  el.scrollTop = el.scrollHeight;
}

// 事件委托：版本标签点击
document.addEventListener('click', function(e) {
  var btn = e.target.closest('[data-ver-agent]');
  if (!btn) return;
  var agent = btn.dataset.verAgent;
  var idx = parseInt(btn.dataset.verIdx);
  if (idx === -2) {
    showExecResult = true;
  } else {
    showExecResult = false;
    activeVer[agent] = idx;
  }
  renderVersionedResult(agent);
});

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
  versions = {claude: [], codex: []};
  activeVer = {claude: -1, codex: -1};
  executionResult = null;
  showExecResult = false;
  ['claude','codex'].forEach(function(a){
    document.getElementById('log_'+a).innerHTML='';
    document.getElementById('result_'+a).innerHTML='';
    document.getElementById('ver_bar_'+a).innerHTML='';
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
  try{
    const r=await api('POST','/api/execute',{session_id:sid});
    if(r.error){alert('执行启动失败: '+r.error);return;}
  }catch(e){alert('执行请求失败: '+e.message);}
}
async function doContinue(){
  if(!sid)return;
  const extra=parseInt(document.getElementById('inp_extra').value)||3;
  const payload={session_id:sid,extra_rounds:extra};
  const wasConsensus=st==='consensus';
  if(wasConsensus){
    const reason=document.getElementById('inp_inject').value.trim();
    if(!reason){alert('请在输入框中填写驳回理由');return;}
    payload.message=reason;
  }
  const r=await api('POST','/api/continue',payload);
  if(r.error){alert(r.error);return;}
  if(wasConsensus)document.getElementById('inp_inject').value='';
  if(!poll)poll=setInterval(pollEvt,300);
}
async function doInject(){
  if(!sid)return;
  if(st==='consensus'){alert('共识状态下请使用"继续协商"提交驳回理由');return;}
  const i=document.getElementById('inp_inject');
  if(!i.value.trim())return;
  await api('POST','/api/inject',{session_id:sid,message:i.value.trim()});
  i.value='';
}
async function doReviewFix(){
  if(!sid)return;
  if(!confirm('确认修复？Claude 将继续用 --dangerously-skip-permissions'))return;
  await api('POST','/api/review_fix',{session_id:sid});
}
async function doReviewSkip(){
  if(!sid)return;
  await api('POST','/api/review_skip',{session_id:sid});
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

// ── Issue 2: 折叠区管理 ──
var activeFold={claude:null,codex:null};
var foldSeq=0;
function openCollapsible(agent,type){
  var el=document.getElementById('log_'+agent);
  var fid='fold_'+(++foldSeq);
  var label={command_output:'命令输出',tool_result:'工具输出'}[type]||'输出';
  el.insertAdjacentHTML('beforeend','<details class="chunk-fold" id="'+fid+'" data-ctype="'+type+'" open><summary>'+label+'</summary><div class="fold-body"></div></details>');
  activeFold[agent]=fid;
  el.scrollTop=el.scrollHeight;
}
function appendOrGrowCollapsible(agent,type,html){
  if(activeFold[agent]){
    var fold=document.getElementById(activeFold[agent]);
    if(fold&&fold.dataset.ctype===type){
      fold.querySelector('.fold-body').insertAdjacentHTML('beforeend',html);
      fold.parentElement.scrollTop=fold.parentElement.scrollHeight;
      return;
    }
  }
  openCollapsible(agent,type);
  var fold=document.getElementById(activeFold[agent]);
  fold.querySelector('.fold-body').insertAdjacentHTML('beforeend',html);
}

function handle(e){
  switch(e.type){
    case 'round_start':
      var hdr='══════ 第 '+e.data.round+' / '+e.data.max+' 轮 ══════';
      appendLog('claude','<div class="log-sep sys">'+hdr+'</div>');
      appendLog('codex','<div class="log-sep sys">'+hdr+'</div>');
      break;
    case 'agent_thinking':
      var who=e.data.agent;
      switchTab(who,'log');
      appendLog(who,'<div class="log-sep sys">'+(who==='claude'?'[Claude 分析中...]':'[Codex 审查中...]')+'</div>');
      break;
    case 'agent_chunk':{
      var ct=e.data.chunk_type||'text';
      var ag=e.data.agent;
      var txt=esc(e.data.text);
      if(ct==='command'){
        appendLog(ag,'<span class="chunk-cmd">'+txt+'</span>');
      }else if(ct==='command_output'){
        appendOrGrowCollapsible(ag,ct,txt);
      }else{
        appendLog(ag,txt);
      }
      break;
    }
    case 'chunk_boundary':{
      var ag2=e.data.agent;
      if(activeFold[ag2]){
        var fold=document.getElementById(activeFold[ag2]);
        if(fold)fold.removeAttribute('open');
        activeFold[ag2]=null;
      }
      break;
    }
    case 'agent_stderr':
      if(e.data.is_mcp){
        appendLog(e.data.agent,'<span class="mcp-line">[MCP] '+esc(e.data.text)+'</span>');
      }
      break;
    case 'agent_result':
      break;
    case 'agent_response':{
      var role=e.data.role;
      if(role==='user'){
        appendLog('claude','<div class="log-sep sys">[你] '+esc(e.data.content)+'</div>');
        appendLog('codex','<div class="log-sep sys">[你] '+esc(e.data.content)+'</div>');
        break;
      }
      var ag3=role==='claude'?'claude':'codex';
      appendLog(ag3,'<div class="log-sep sys">── '+e.data.phase+' 完成 (R'+e.data.round+') ──</div>');
      if(e.data.content){
        if(!versions[ag3].some(function(v){return v.round===e.data.round;})){
          versions[ag3].push({round:e.data.round,phase:e.data.phase,content:e.data.content});
        }
        activeVer[ag3]=-1;
        showExecResult=false;
        renderVersionedResult(ag3);
      }
      switchTab(ag3,'result');
      if(role==='claude'){
        var h='<div class="log-sep sys">── Claude R'+e.data.round+' 方案已发送给 Codex ──</div>';
        if(e.data.content){
          h+='<details class="plan-preview"><summary>查看发送给 Codex 的方案内容</summary><div class="plan-body">'+esc(e.data.content)+'</div></details>';
        }
        appendLog('codex',h);
      }else if(role==='codex'){
        var h2='<div class="log-sep sys">── Codex R'+e.data.round+' 审查意见已发送给 Claude ──</div>';
        if(e.data.content){
          h2+='<details class="plan-preview"><summary>查看发送给 Claude 的审查意见</summary><div class="plan-body">'+esc(e.data.content)+'</div></details>';
        }
        appendLog('claude',h2);
      }
      break;
    }
    case 'consensus_reached':
      appendLog('claude','<div class="log-sep ok">✓ '+e.data.msg+'</div>');
      appendLog('codex','<div class="log-sep ok">✓ '+e.data.msg+'</div>');
      break;
    case 'max_rounds_reached':
      appendLog('claude','<div class="log-sep sys">⚠ '+e.data.msg+'</div>');
      appendLog('codex','<div class="log-sep sys">⚠ '+e.data.msg+'</div>');
      break;
    case 'execution_done':
      appendLog('claude','<div class="log-sep ok">══════ 执行完成 ══════</div>');
      executionResult=e.data.result;
      showExecResult=true;
      renderVersionedResult('claude');
      var head=document.querySelector('.dot-claude').parentElement;
      if(head&&!head.querySelector('.done-badge')){
        head.insertAdjacentHTML('beforeend','<span class="done-badge">✓ 执行完毕</span>');
      }
      break;
    case 'error':
      appendLog('claude','<div class="log-sep err">❌ '+esc(e.data.msg)+'</div>');
      appendLog('codex','<div class="log-sep err">❌ '+esc(e.data.msg)+'</div>');
      break;
    case 'warning':
      appendLog('claude','<div class="log-sep sys">⚠ '+esc(e.data.msg)+'</div>');
      break;
    case 'rollback':
      versions.claude=versions.claude.filter(function(v){return v.round<=e.data.round;});
      versions.codex=versions.codex.filter(function(v){return v.round<=e.data.round;});
      activeVer.claude=-1;
      activeVer.codex=-1;
      renderVersionedResult('claude');
      renderVersionedResult('codex');
      appendLog('claude','<div class="log-sep sys">⚠ '+esc(e.data.msg)+'</div>');
      appendLog('codex','<div class="log-sep sys">⚠ '+esc(e.data.msg)+'</div>');
      break;
    case 'status_change':
      if(e.data.status==='stopped'){
        appendLog('claude','<div class="log-sep sys">⏹ 已中止</div>');
        appendLog('codex','<div class="log-sep sys">⏹ 已中止</div>');
      }
      break;
    // ── Issue 4: 审查循环事件 ──
    case 'review_response':{
      var rrole=e.data.role;
      var rag=rrole==='claude'?'claude':'codex';
      appendLog(rag,'<div class="log-sep sys">── '+e.data.phase+' (审查轮 '+e.data.round+') ──</div>');
      if(rrole==='codex'&&e.data.content){
        appendLog('codex','<details class="plan-preview" open><summary>Codex 审查意见</summary><div class="plan-body">'+esc(e.data.content)+'</div></details>');
        appendLog('claude','<details class="plan-preview"><summary>查看 Codex 审查意见</summary><div class="plan-body">'+esc(e.data.content)+'</div></details>');
      }else if(rrole==='claude'&&e.data.content){
        appendLog('claude','<details class="plan-preview" open><summary>Claude 修复总结</summary><div class="plan-body">'+esc(e.data.content)+'</div></details>');
        appendLog('codex','<details class="plan-preview"><summary>查看 Claude 修复总结</summary><div class="plan-body">'+esc(e.data.content)+'</div></details>');
      }
      break;
    }
    case 'review_start':
      appendLog('claude','<div class="log-sep ok">══════ 执行后审查开始 ══════</div>');
      appendLog('codex','<div class="log-sep ok">══════ 执行后审查开始 ══════</div>');
      break;
    case 'review_round_start':
      appendLog('claude','<div class="log-sep sys">══════ 审查修复轮 '+e.data.round+' / '+e.data.max+' ══════</div>');
      appendLog('codex','<div class="log-sep sys">══════ 审查修复轮 '+e.data.round+' / '+e.data.max+' ══════</div>');
      break;
    case 'review_needs_fix':
      appendLog('claude','<div class="log-sep sys">⚠ '+esc(e.data.msg)+'</div>');
      appendLog('codex','<div class="log-sep sys">⚠ '+esc(e.data.msg)+'</div>');
      break;
    case 'review_done':
      if(e.data.success){
        appendLog('claude','<div class="log-sep ok">══════ 任务收口成功 ══════</div>');
        appendLog('codex','<div class="log-sep ok">══════ 任务收口成功 ══════</div>');
      }else{
        appendLog('claude','<div class="log-sep sys">⚠ '+esc(e.data.msg)+'</div>');
        appendLog('codex','<div class="log-sep sys">⚠ '+esc(e.data.msg)+'</div>');
      }
      var badge=document.querySelector('.done-badge');
      if(badge)badge.textContent=e.data.success?'✓ 收口成功':'⚠ 审查完成';
      break;
  }
}

function switchTab(agent, tab){
  document.querySelectorAll('.tab[data-agent="'+agent+'"]').forEach(function(t){t.classList.toggle('active',t.dataset.tab===tab)});
  document.getElementById('log_'+agent).classList.toggle('active',tab==='log');
  document.getElementById('result_'+agent+'_wrap').classList.toggle('active',tab==='result');
}

function appendLog(agent, html){
  const el=document.getElementById('log_'+agent);
  if(!el)return;
  el.insertAdjacentHTML('beforeend',html);
  el.scrollTop=el.scrollHeight;
}


function updSt(s,r,m){
  document.getElementById('rinfo').textContent=s==='idle'?'':'R'+r+'/'+m;
  if(s===st)return; st=s;
  const p=document.getElementById('pill');
  p.className='pill pill-'+s;
  p.textContent={idle:'IDLE',running:'NEGOTIATING',consensus:'CONSENSUS',max_rounds:'MAX ROUNDS',executing:'EXECUTING',review_pending:'REVIEWING',review_fix:'NEEDS FIX',done:'DONE',error:'ERROR'}[s]||s;
  document.getElementById('btn_go').disabled=!['idle','done','error'].includes(s);
  document.getElementById('btn_stop').disabled=!['running','executing','review_pending'].includes(s);
  document.getElementById('btn_exec').disabled=s!=='consensus'&&s!=='max_rounds';
  const showCont=s==='max_rounds'||s==='consensus';
  document.getElementById('btn_cont').style.display=showCont?'':'none';
  document.getElementById('btn_cont').disabled=!showCont;
  document.getElementById('inp_extra').style.display=showCont?'':'none';
  document.querySelector('.btn-inj').disabled=s==='consensus';
  document.getElementById('btn_fix').disabled=s!=='review_fix';
  document.getElementById('btn_fix').style.display=s==='review_fix'?'':'none';
  document.getElementById('btn_skip').disabled=s!=='review_fix';
  document.getElementById('btn_skip').style.display=s==='review_fix'?'':'none';
  if(['idle','done','error'].includes(s)&&poll){clearInterval(poll);poll=null;}
}

function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// ── 提示词配置 ──
const cfgKeys=['claude_first','claude_revise','codex_first','codex_review','execution','execution_unapproved','codex_post_review','claude_post_fix','codex_post_review_followup','user_inject_label_claude','user_inject_label_codex'];

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

// ── 路径自动补全 ──
var acTimer=null;
var inp_path=document.getElementById('inp_path');

inp_path.addEventListener('input',function(){
  clearTimeout(acTimer);
  acTimer=setTimeout(doAutoComplete,200);
});
inp_path.addEventListener('focus',showRecentIfEmpty);
inp_path.addEventListener('blur',function(){
  setTimeout(function(){document.getElementById('pathDropdown').classList.remove('open');},200);
});

async function showRecentIfEmpty(){
  if(inp_path.value.trim())return;
  var r=await api('GET','/api/recent_paths');
  var dd=document.getElementById('pathDropdown');
  if(!r.paths||!r.paths.length){dd.classList.remove('open');return;}
  var html='<div class="pd-section">最近使用</div>';
  r.paths.forEach(function(p){
    html+='<div class="pd-item" data-pick-path="'+escAttr(p)+'">'+esc(p)+'</div>';
  });
  dd.innerHTML=html;
  dd.classList.add('open');
}

async function doAutoComplete(){
  var prefix=inp_path.value.trim();
  if(!prefix){showRecentIfEmpty();return;}
  var r=await api('GET','/api/complete?prefix='+encodeURIComponent(prefix));
  var dd=document.getElementById('pathDropdown');
  if(!r.suggestions||!r.suggestions.length){dd.classList.remove('open');return;}
  var html='';
  r.suggestions.forEach(function(s){
    html+='<div class="pd-item" data-pick-path="'+escAttr(s.path)+'">'+
      esc(s.name)+(s.is_git?' <span class="pd-git">GIT</span>':'')+'</div>';
  });
  dd.innerHTML=html;
  dd.classList.add('open');
}

document.getElementById('pathDropdown').addEventListener('mousedown',function(e){
  var item=e.target.closest('[data-pick-path]');
  if(!item)return;
  e.preventDefault();
  inp_path.value=item.dataset.pickPath;
  document.getElementById('pathDropdown').classList.remove('open');
});

function escAttr(s){
  return(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── 文件夹浏览器 ──
var browseCurrent='';

document.getElementById('btn_browse').addEventListener('click',function(){
  document.getElementById('browseModal').classList.add('open');
  browseDir(inp_path.value.trim()||'');
});
document.getElementById('browse_close').addEventListener('click',closeBrowse);
document.getElementById('browse_cancel').addEventListener('click',closeBrowse);
document.getElementById('browse_select').addEventListener('click',function(){
  inp_path.value=browseCurrent;
  closeBrowse();
});
document.getElementById('browse_go_btn').addEventListener('click',function(){
  var p=document.getElementById('browse_path_input').value.trim();
  if(p)browseDir(p);
});
document.getElementById('browse_path_input').addEventListener('keydown',function(e){
  if(e.key==='Enter'){
    var p=this.value.trim();
    if(p)browseDir(p);
  }
});

function closeBrowse(){
  document.getElementById('browseModal').classList.remove('open');
}

async function browseDir(path){
  var r=await api('GET','/api/browse?path='+encodeURIComponent(path));
  if(r.error){alert(r.error);return;}
  browseCurrent=r.current;
  document.getElementById('browse_path_input').value=r.current;
  document.getElementById('browse_current').textContent=
    r.current+(r.is_git?'  (git repo)':'')+(r.truncated?'  [仅显示前200项]':'');
  var html='';
  if(r.parent){
    html+='<div class="browse-item bi-parent" data-browse-path="'+escAttr(r.parent)+'" data-browse-action="navigate">'+
      '<span class="bi-icon">⬆</span><span class="bi-name">..</span></div>';
  }
  r.dirs.forEach(function(d){
    html+='<div class="browse-item" data-browse-path="'+escAttr(d.path)+'">'+
      '<span class="bi-icon">📁</span><span class="bi-name">'+esc(d.name)+'</span>'+
      (d.is_git?'<span class="bi-git">GIT</span>':'')+'</div>';
  });
  if(!r.dirs.length&&r.parent){
    html+='<div style="padding:20px;text-align:center;color:var(--dim)">没有子文件夹</div>';
  }
  document.getElementById('browse_list').innerHTML=html;
}

(function(){
  var list=document.getElementById('browse_list');
  var clickTimer=null;
  list.addEventListener('click',function(e){
    var item=e.target.closest('[data-browse-path]');
    if(!item)return;
    var path=item.dataset.browsePath;
    if(item.dataset.browseAction==='navigate'){browseDir(path);return;}
    clearTimeout(clickTimer);
    clickTimer=setTimeout(function(){
      list.querySelectorAll('.browse-item.selected').forEach(function(el){el.classList.remove('selected');});
      item.classList.add('selected');
      browseCurrent=path;
      document.getElementById('browse_current').textContent=path;
    },200);
  });
  list.addEventListener('dblclick',function(e){
    var item=e.target.closest('[data-browse-path]');
    if(!item)return;
    clearTimeout(clickTimer);
    browseDir(item.dataset.browsePath);
  });
})();

// ── 初始化：从 URL 恢复会话 ──
(function(){
  var p=new URLSearchParams(location.search);
  if(p.get('project'))document.getElementById('inp_path').value=p.get('project');
  if(p.get('sid')){
    sid=p.get('sid');
    cursor=0; st='idle';
    api('GET','/api/history?sid='+sid).then(function(r){
      if(r.entries){
        r.entries.forEach(function(h){
          var ag=h.role;
          if(!versions[ag].some(function(v){return v.round===h.round;})){
            versions[ag].push({round:h.round,phase:h.phase,content:h.content});
          }
        });
        ['claude','codex'].forEach(function(ag){
          if(versions[ag].length){activeVer[ag]=-1;renderVersionedResult(ag);}
        });
      }
      if(r.execution_result!=null){
        executionResult=r.execution_result;
        showExecResult=true;
        renderVersionedResult('claude');
      }
      // ── 恢复审查上下文 ──
      if(r.review_status){
        var rs=r.review_status;
        appendLog('claude','<div class="log-sep ok">══════ 执行后审查开始 ══════</div>');
        appendLog('codex','<div class="log-sep ok">══════ 执行后审查开始 ══════</div>');
        if(r.review_entries&&r.review_entries.length){
          r.review_entries.forEach(function(h){
            var ag=h.role==='claude'?'claude':'codex';
            appendLog(ag,'<div class="log-sep sys">── '+h.phase+' (审查轮 '+h.round+') ──</div>');
            if(h.content){
              var label=h.role==='codex'?'Codex 审查意见':'Claude 修复总结';
              appendLog(ag,'<details class="plan-preview"><summary>'+label+'</summary><div class="plan-body">'+esc(h.content)+'</div></details>');
              var other=ag==='claude'?'codex':'claude';
              appendLog(other,'<details class="plan-preview"><summary>查看'+label+'</summary><div class="plan-body">'+esc(h.content)+'</div></details>');
            }
          });
        }
        if(rs.status==='done'){
          var lastCodex=null;
          if(r.review_entries){for(var i=r.review_entries.length-1;i>=0;i--){if(r.review_entries[i].role==='codex'){lastCodex=r.review_entries[i];break;}}}
          var success=lastCodex&&lastCodex.content&&lastCodex.content.split('\\n')[0].indexOf('任务收口成功')>=0;
          if(success){
            appendLog('claude','<div class="log-sep ok">══════ 任务收口成功 ══════</div>');
            appendLog('codex','<div class="log-sep ok">══════ 任务收口成功 ══════</div>');
          }else{
            appendLog('claude','<div class="log-sep sys">⚠ 审查完成（未达成收口确认）</div>');
            appendLog('codex','<div class="log-sep sys">⚠ 审查完成（未达成收口确认）</div>');
          }
          var head=document.querySelector('.dot-claude');
          if(head)head=head.parentElement;
          if(head&&!head.querySelector('.done-badge')){
            head.insertAdjacentHTML('beforeend','<span class="done-badge">'+(success?'✓ 收口成功':'⚠ 审查完成')+'</span>');
          }
        }else if(rs.status==='review_fix'){
          appendLog('claude','<div class="log-sep sys">⚠ Codex 发现问题，等待你确认是否修复。</div>');
          appendLog('codex','<div class="log-sep sys">⚠ Codex 发现问题，等待你确认是否修复。</div>');
        }else if(rs.status==='review_pending'){
          appendLog('codex','<div class="log-sep sys">[Codex 评审中...]</div>');
        }
      }
      // 用 event_cursor 跳过已恢复事件
      if(r.event_cursor!=null){cursor=r.event_cursor;}
      if(!poll)poll=setInterval(pollEvt,300);
    });
  }
})();
</script>
</body>
</html>"""
