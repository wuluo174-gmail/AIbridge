"""
Bridge HTTP server
==================
统一账本 + SSE 主链路的 HTTP API。
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
RECENT_PATHS_FILE = _ROOT / "recent_paths.json"


def load_recent_paths():
    if RECENT_PATHS_FILE.exists():
        try:
            return json.loads(RECENT_PATHS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_recent_paths(paths):
    try:
        RECENT_PATHS_FILE.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class _BridgeProxy:
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
:root{color-scheme:dark light;--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:24px}
.card{max-width:760px;background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:28px 30px}
code{background:rgba(255,255,255,.06);padding:2px 6px;border-radius:6px}
</style>
</head>
<body>
<main class="card">
  <h1>前端尚未构建</h1>
  <p>请执行 <code>cd frontend && npm run build</code> 后刷新页面。</p>
</main>
</body>
</html>"""


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    _DIST_DIR = Path(__file__).parent.parent / "frontend" / "dist"
    _MIME_MAP = {
        ".js": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
    }

    def log_message(self, *args):
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

    def _session_from_qs(self, qs):
        sid = qs.get("sid", [None])[0]
        return _b.get_or_load_session(sid)

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

        def _followup():
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    os.killpg(pgid, 0)
                except (ProcessLookupError, PermissionError):
                    with sess.proc_lock:
                        sess.active_pgid = None
                    return
                time.sleep(0.2)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            with sess.proc_lock:
                sess.active_pgid = None

        threading.Thread(target=_followup, daemon=True).start()

    def _serve_stream(self, sess, since):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        cursor = since
        try:
            while True:
                with sess.event_cond:
                    while cursor >= len(sess.stream_events):
                        sess.event_cond.wait(timeout=1.0)
                        if cursor >= len(sess.stream_events):
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                    pending = list(sess.stream_events[cursor:])
                for event in pending:
                    payload = json.dumps(_b.event_snapshot(event, include_projection=True), ensure_ascii=False)
                    chunk = f"id: {event['id']}\ndata: {payload}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    cursor = event["id"] + 1
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/":
            dist_index = self._DIST_DIR / "index.html"
            body = dist_index.read_bytes() if dist_index.is_file() else NO_DIST_BOOTSTRAP_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path.startswith("/assets/"):
            filepath = (self._DIST_DIR / parsed.path.lstrip("/")).resolve()
            dist_resolved = str(self._DIST_DIR.resolve()) + os.sep
            if filepath.is_file() and str(filepath).startswith(dist_resolved):
                body = filepath.read_bytes()
                mime = self._MIME_MAP.get(filepath.suffix, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)
            return

        if parsed.path == "/api/tools":
            return self._json({"tools": _b.list_tools()})

        if parsed.path == "/api/workflow_config":
            sess = self._session_from_qs(qs)
            cfg = _b.get_workflow_config(sess)
            return self._json(cfg)

        if parsed.path == "/api/session/state":
            sess = self._session_from_qs(qs)
            return self._json(_b.serialize_session_state(sess) if sess else _b.empty_session_state())

        if parsed.path == "/api/history":
            sess = self._session_from_qs(qs)
            return self._json(_b.history_payload(sess) if sess else _b.empty_history_payload())

        if parsed.path == "/api/sessions":
            limit = int(qs.get("limit", ["50"])[0])
            offset = int(qs.get("offset", ["0"])[0])
            return self._json({"sessions": _b.list_sessions(limit, offset)})

        if parsed.path == "/api/stream":
            sess = self._session_from_qs(qs)
            if not sess:
                self.send_error(404)
                return
            since = int(qs.get("since", ["0"])[0])
            return self._serve_stream(sess, since)

        if parsed.path == "/api/browse":
            raw = qs.get("path", [""])[0].strip()
            target = Path(raw).expanduser().resolve() if raw else Path.home()
            if not target.is_dir():
                return self._json({"error": f"非目录: {target}"}, 400)
            parent = str(target.parent) if target != target.parent else None
            dirs = []
            truncated = False
            try:
                with os.scandir(str(target)) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=True) and not entry.name.startswith("."):
                            dirs.append((entry.name, entry.path))
            except PermissionError:
                return self._json({"error": f"权限不足: {target}"}, 403)
            dirs.sort(key=lambda item: item[0])
            if len(dirs) > 200:
                dirs = dirs[:200]
                truncated = True
            payload = {
                "current": str(target),
                "parent": parent,
                "dirs": [{"name": name, "path": path, "is_git": (Path(path) / ".git").exists()} for name, path in dirs],
                "truncated": truncated,
            }
            return self._json(payload)

        if parsed.path == "/api/complete":
            prefix = qs.get("prefix", [""])[0].strip()
            if not prefix:
                return self._json({"suggestions": _b.load_recent_paths()[:15]})
            candidate = Path(prefix).expanduser()
            base = candidate if candidate.is_dir() else candidate.parent
            base = base if str(base) else Path(".")
            if not base.exists():
                return self._json({"suggestions": []})
            suggestions = []
            try:
                with os.scandir(str(base)) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        path = str((base / entry.name).resolve())
                        if path.startswith(str(candidate)):
                            suggestions.append(path)
            except PermissionError:
                pass
            suggestions.extend([path for path in _b.load_recent_paths() if path.startswith(prefix)])
            deduped = []
            seen = set()
            for item in suggestions:
                if item in seen:
                    continue
                seen.add(item)
                deduped.append(item)
            return self._json({"suggestions": deduped[:15]})

        if parsed.path == "/api/recent_paths":
            return self._json({"paths": _b.load_recent_paths()})

        if parsed.path == "/api/prompts":
            return self._json(_b.load_prompts())

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        body = self._body()

        if parsed.path == "/api/workflow_config":
            cfg = _b.update_workflow_config(body)
            return self._json(cfg)

        if parsed.path == "/api/session/start":
            task = (body.get("task") or "").strip()
            project_path = (body.get("project_path") or "").strip()
            if not task or not project_path:
                return self._json({"error": "task 和 project_path 均为必填"}, 400)
            sess = _b.start_session(project_path, task, body)
            return self._json({"ok": True, "session_id": sess.session_id})

        if parsed.path == "/api/session/pause":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            _b.pause_session(sess)
            self._stop_process_group(sess)
            return self._json({"ok": True})

        if parsed.path == "/api/session/resume":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            err = _b.resume_session(sess)
            if err:
                return self._json({"error": err}, 400)
            return self._json({"ok": True})

        if parsed.path == "/api/session/stop":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            _b.abort_session(sess)
            self._stop_process_group(sess)
            return self._json({"ok": True})

        if parsed.path == "/api/session/exec":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            err = _b.execute_session(sess)
            if err:
                return self._json({"error": err}, 400)
            return self._json({"ok": True})

        if parsed.path == "/api/session/continue":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            err = _b.continue_session(sess, body)
            if err:
                return self._json({"error": err}, 400)
            return self._json({"ok": True})

        if parsed.path == "/api/session/review_fix":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            err = _b.review_fix_session(sess)
            if err:
                return self._json({"error": err}, 400)
            return self._json({"ok": True})

        if parsed.path == "/api/session/review_skip":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            _b.review_skip_session(sess)
            return self._json({"ok": True})

        if parsed.path == "/api/session/review_continue":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            err = _b.review_continue_session(sess, body)
            if err:
                return self._json({"error": err}, 400)
            return self._json({"ok": True})

        if parsed.path == "/api/session/view_mode":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            err = _b.set_session_view_mode(sess, body.get("view_mode"))
            if err:
                return self._json({"error": err}, 400)
            return self._json({"ok": True})

        if parsed.path == "/api/input":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            result = _b.handle_input(sess, body)
            if result.get("stop_process"):
                self._stop_process_group(sess)
            code = 200 if result.get("ok") else 400
            return self._json(result, code)

        if parsed.path == "/api/terminal/resize":
            sess = _b.get_or_load_session(body.get("session_id"))
            if not sess:
                return self._json({"error": "会话不存在"}, 404)
            result = _b.resize_terminal_viewport(sess, body)
            code = 200 if result.get("ok") else 400
            return self._json(result, code)

        if parsed.path == "/api/prompts":
            _b.save_prompts(body)
            return self._json({"ok": True})

        self.send_error(404)
