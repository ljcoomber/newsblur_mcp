"""HTTP front. Only POST /<SECRET>/mcp and GET /healthz exist; everything else is 404/405."""
from __future__ import annotations

import hmac
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .protocol import handle
from .tools import Tools

log = logging.getLogger("nbmcp.http")
MAX_BODY = 1_000_000


def make_handler(cfg: Config, tools: Tools):
    secret = cfg.secret_path.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "nbmcp"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _safe_path(self):
            path = self.path.split("?")[0]
            if path == "/healthz":
                return path
            parts = path.split("/")
            if len(parts) == 3 and hmac.compare_digest(parts[1].encode(), secret):
                return f"/<secret>/{parts[2][:20]}"
            return "/<not-found>"

        def log_message(self, fmt, *args):
            log.info("%s %s -> %s", self.command, self._safe_path(), args[1] if len(args) > 1 else "")

        def _is_mcp_path(self):
            parts = self.path.split("?")[0].split("/")
            return len(parts) == 3 and parts[0] == "" and parts[2] == "mcp" and hmac.compare_digest(parts[1].encode(), secret)

        def do_GET(self):
            if self.path.split("?")[0] == "/healthz":
                return self._send(200, b"ok")
            if self._is_mcp_path():
                return self._send(405, b"This server only answers POST", extra={"Allow": "POST"})
            self._send(404, b"Not found")

        do_HEAD = do_GET

        def do_DELETE(self):
            if self._is_mcp_path():
                return self._send(405, b"", extra={"Allow": "POST"})
            self._send(404, b"Not found")

        def do_POST(self):
            if not self._is_mcp_path():
                self.close_connection = True
                return self._send(404, b"Not found")
            origin = self.headers.get("Origin")
            if origin and origin not in cfg.allowed_origins:
                self.close_connection = True
                return self._send(403, b"Origin not allowed")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                self.close_connection = True
                return self._send(413 if length > MAX_BODY else 400, b"Bad request body")
            try:
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                return self._reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            if isinstance(payload, list):
                replies = [r for r in (handle(tools, m) for m in payload) if r is not None]
                if not replies:
                    return self._send(202)
                return self._reply(200, replies)
            reply = handle(tools, payload)
            if reply is None:
                return self._send(202)
            self._reply(200, reply)

        def _reply(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(cfg, tools, host=None, port=None):
    port = cfg.port if port is None else port
    handler = make_handler(cfg, tools)
    if host is None:
        try:
            _Server.address_family = socket.AF_INET6
            return _Server(("::", port), handler)
        except OSError:
            _Server.address_family = socket.AF_INET
            return _Server(("0.0.0.0", port), handler)
    _Server.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return _Server((host, port), handler)


def serve_in_thread(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t
