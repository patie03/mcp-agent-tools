"""MCP client: starts the server as a child process and talks to it over stdio.

    async with MCPToolClient() as client:
        tools = client.tools                         # discovered via session.list_tools()
        openai_tools = to_openai_tools(tools)        # MCP inputSchema -> OpenAI function schema
        text, is_error = await client.call_tool("calculate_expression", {"expression": "2+2"})
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool

ROOT = Path(__file__).resolve().parents[2]
MAX_RESULT_CHARS = 4000  # keep tool output from bloating the conversation


@dataclass
class ToolCallResult:
    text: str
    is_error: bool
    latency_ms: float


def server_parameters() -> StdioServerParameters:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
    return StdioServerParameters(command=sys.executable, args=["-m", "src.server.mcp_server"],
                                 cwd=str(ROOT), env=env)


def to_openai_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    """Translate MCP tool definitions to the OpenAI Chat Completions `tools` format."""
    out = []
    for t in tools:
        params = dict(t.inputSchema or {"type": "object", "properties": {}})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        out.append({"type": "function",
                    "function": {"name": t.name, "description": t.description or "", "parameters": params}})
    return out


class MCPToolClient:
    """Async context manager owning the server process and the MCP session."""

    def __init__(self, params: StdioServerParameters | None = None):
        self.params = params or server_parameters()
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None
        self.tools: list[Tool] = []

    async def __aenter__(self) -> "MCPToolClient":
        read, write = await self._stack.enter_async_context(stdio_client(self.params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        self.tools = (await self.session.list_tools()).tools
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    @property
    def tool_names(self) -> set[str]:
        return {t.name for t in self.tools}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        assert self.session is not None, "use `async with MCPToolClient()`"
        start = time.perf_counter()
        result = await self.session.call_tool(name, arguments)
        latency = round((time.perf_counter() - start) * 1000, 1)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        is_error = bool(result.isError)
        if not is_error:
            try:
                is_error = "error" in json.loads(text)
            except (ValueError, TypeError):
                pass
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " …[truncated]"
        return ToolCallResult(text=text, is_error=is_error, latency_ms=latency)


async def discover_tools() -> list[Tool]:
    async with MCPToolClient() as client:
        return client.tools
