"""Minimal MCP (JSON-RPC 2.0) handler for a tools-only server over streamable HTTP, JSON responses."""
from __future__ import annotations

import json
import logging

from .tools import ToolError, Tools

log = logging.getLogger("nbmcp")
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "newsblur-reader", "version": "0.1.0"}
INSTRUCTIONS = ("Reads stories from the owner's NewsBlur account and, when enabled, marks stories as read. "
                "There is no subscribe, unsubscribe, delete or share tool.")


def _err(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(tools: Tools, msg):
    """Return a response object, or None for notifications/responses that need no reply."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "Invalid Request")
    method, id_ = msg.get("method"), msg.get("id")
    if method is None:                       # a response from the client: ignore
        return None
    if id_ is None:                          # notification
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in SUPPORTED else SUPPORTED[0]
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": tools.specs()}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _err(id_, -32602, "Invalid params")
        try:
            out = tools.call(name, args)
            text, is_error = json.dumps(out, ensure_ascii=False), False
        except ToolError as e:
            text, is_error = str(e), True
        except Exception as e:               # never leak internals or tokens
            log.exception("tool %s crashed", name)
            text, is_error = f"internal error ({type(e).__name__})", True
        log.info("tool %s -> %s", name, "error" if is_error else "ok")
        return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}
    return _err(id_, -32601, f"Method not found: {method}")
