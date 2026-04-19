"""Stdio MCP server for memU.

Implements the subset of the Model Context Protocol needed by coding
agents: ``initialize``, ``tools/list``, ``tools/call``, ``prompts/list``,
``prompts/get``. Uses JSON-RPC over stdio so it runs without the official
MCP SDK installed — the only dependency is the Python stdlib.

Every tool call is routed through :mod:`memu.mcp.tools`, so the same
dispatch logic powers Claude Code, Cursor, Zed, Continue, Aider, and any
LangGraph/PydanticAI/CrewAI adapter that can invoke an MCP server.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Optional

from ..storage.base import StorageBackend
from . import prompts, tools

log = logging.getLogger("memu.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memu"
SERVER_VERSION = "0.1.0"


async def run_stdio(backend: StorageBackend) -> None:
    """Main event loop over stdin/stdout.

    Each line of stdin is a JSON-RPC 2.0 request; each response is a
    single line on stdout. The server terminates when stdin closes.
    """
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )

    while True:
        line = await reader.readline()
        if not line:
            return
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            _write(_error_response(None, -32700, f"Parse error: {exc}"))
            continue
        response = await _handle(backend, msg)
        if response is not None:
            _write(response)


async def _handle(
    backend: StorageBackend, msg: dict[str, Any]
) -> Optional[dict[str, Any]]:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    try:
        if method == "initialize":
            return _ok(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "capabilities": {
                        "tools": {},
                        "prompts": {},
                        "resources": {},
                    },
                },
            )
        if method == "notifications/initialized":
            return None  # Notifications expect no response.
        if method == "tools/list":
            return _ok(msg_id, {"tools": tools.TOOL_SCHEMAS})
        if method == "tools/call":
            result = await tools.dispatch(
                backend, params["name"], params.get("arguments", {})
            )
            return _ok(
                msg_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(result, default=str)}
                    ],
                    "isError": False,
                },
            )
        if method == "prompts/list":
            return _ok(msg_id, {"prompts": prompts.PROMPTS})
        if method == "prompts/get":
            messages = prompts.render(params["name"], params.get("arguments", {}))
            return _ok(msg_id, {"messages": messages})
        return _error_response(msg_id, -32601, f"Method not found: {method}")
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("tool call failed")
        return _error_response(msg_id, -32000, str(exc))


def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def _write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


# Convenience for testing: run with a fresh markdown backend at a tmp dir.
if __name__ == "__main__":  # pragma: no cover
    from ..storage import get_backend

    async def _main() -> None:
        backend = get_backend()
        await backend.init()
        try:
            await run_stdio(backend)
        finally:
            await backend.close()

    asyncio.run(_main())
