#!/usr/bin/env python3
# =============================================================================
# MCP bridge — one API over both transports.
#
# The orchestrator never needs to know whether a tools server is a local
# subprocess or a remote HTTP endpoint. It opens a session, lists tools, calls
# them, and closes. Transport selection comes from the server's config entry,
# which config_loader already validated and, for HTTP, gave a derived `url`.
#
# Sessions are opened once per agent run and reused for every tool call in that
# run: with stdio that avoids respawning the server process per call.
# =============================================================================
from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

LOG = logging.getLogger("orchestrator.mcp")


class McpBridgeError(RuntimeError):
    """A tools server could not be reached or spoke an unusable dialect."""


@asynccontextmanager
async def open_session(
    name: str,
    server: Mapping[str, Any],
    pack_dir: Path,
) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP session for one configured server."""
    transport = str(server.get("transport") or "stdio")

    # The except-Exception blocks below wrap ONLY the connect/initialize steps,
    # never the `yield`. If they wrapped the yield too, an exception raised by
    # the CALLER's own code (e.g. the orchestrator's gateway-call failure) would
    # be thrown into this generator at the yield point, get caught by the same
    # broad except, and get relabeled as a misleading "server unreachable"
    # McpBridgeError — masking the caller's real, already-informative error
    # (observed in testing: a clean 502 from a bad model alias turned into an
    # opaque 500 this way). Setup failures are genuinely ours to translate;
    # whatever the caller does with an open session is not.
    if transport == "http":
        url = str(server.get("url") or "")
        if not url:
            raise McpBridgeError(f"server '{name}' has http transport but no url")
        async with AsyncExitStack() as setup_stack:
            try:
                read, write, _ = await setup_stack.enter_async_context(
                    streamablehttp_client(url)
                )
                session = await setup_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
            except Exception as exc:  # noqa: BLE001 - normalized for the caller
                raise McpBridgeError(
                    f"server '{name}' unreachable over http at {url}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            LOG.info("mcp session open (http) name=%s url=%s", name, url)
            yield session
        return

    command = list(server.get("command") or [])
    if not command:
        raise McpBridgeError(f"server '{name}' has stdio transport but no command")
    cwd = (pack_dir / str(server.get("cwd") or ".")).resolve()
    env = {k: str(v) for k, v in (server.get("env") or {}).items()}
    params = StdioServerParameters(
        command=command[0],
        args=command[1:],
        env=env or None,
        cwd=str(cwd),
    )
    async with AsyncExitStack() as setup_stack:
        try:
            read, write = await setup_stack.enter_async_context(stdio_client(params))
            session = await setup_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception as exc:  # noqa: BLE001 - normalized for the caller
            raise McpBridgeError(
                f"server '{name}' failed to start via stdio ({' '.join(command)}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        LOG.info("mcp session open (stdio) name=%s command=%s",
                 name, " ".join(command))
        yield session


class ToolRouter:
    """Tools from every server one agent is bound to, filtered by its allowlist.

    Tool names are assumed unique across an agent's servers; on a collision the
    first server wins and the duplicate is logged and skipped, so a call can
    never be routed ambiguously.
    """

    def __init__(self) -> None:
        self._route: dict[str, tuple[str, ClientSession]] = {}
        self._schemas: list[dict[str, Any]] = []

    @classmethod
    async def build(
        cls,
        stack: AsyncExitStack,
        server_names: list[str],
        servers: Mapping[str, Any],
        pack_dir: Path,
        allowlist: list[str],
    ) -> "ToolRouter":
        router = cls()
        allowed = set(allowlist)
        for name in server_names:
            server = servers.get(name)
            if not isinstance(server, Mapping):
                raise McpBridgeError(f"server '{name}' is not configured")
            if not server.get("enabled", True):
                LOG.info("mcp server disabled, skipping name=%s", name)
                continue

            session = await stack.enter_async_context(
                open_session(name, server, pack_dir)
            )
            listing = await session.list_tools()
            for tool in listing.tools:
                if tool.name not in allowed:
                    continue
                if tool.name in router._route:
                    LOG.warning("duplicate tool name ignored tool=%s server=%s",
                                tool.name, name)
                    continue
                router._route[tool.name] = (name, session)
                router._schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema
                            or {"type": "object", "properties": {}},
                        },
                    }
                )
        return router

    @property
    def schemas(self) -> list[dict[str, Any]]:
        """Tool definitions in the OpenAI function-calling shape."""
        return self._schemas

    def names(self) -> list[str]:
        return sorted(self._route)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool and return a JSON string for the model to read.

        Never raises: a routing failure or transport error comes back as the
        same {"ok": false, ...} envelope the tools themselves use, so the agent
        loop can keep going and the model can react to it.
        """
        entry = self._route.get(tool_name)
        if entry is None:
            return json.dumps({
                "ok": False,
                "data": None,
                "error": {
                    "code": "unknown_tool",
                    "message": f"'{tool_name}' is not available to this agent",
                    "details": {"available": self.names()},
                },
            })
        server_name, session = entry
        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
            LOG.warning("tool call failed tool=%s server=%s error=%s",
                        tool_name, server_name, exc)
            return json.dumps({
                "ok": False,
                "data": None,
                "error": {
                    "code": "tool_call_failed",
                    "message": f"calling '{tool_name}' failed",
                    "details": {"server": server_name,
                                "detail": f"{type(exc).__name__}: {exc}"},
                },
            })
        return _result_to_text(result, tool_name, server_name)


def _result_to_text(result: Any, tool_name: str, server_name: str) -> str:
    """Flatten an MCP CallToolResult into a single JSON string."""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return json.dumps(structured, default=str)

    chunks: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
            continue
        data = getattr(block, "data", None)
        if data is not None:
            chunks.append(f"<{getattr(block, 'type', 'blob')} omitted>")
    joined = "\n".join(chunks).strip()

    if getattr(result, "isError", False):
        return json.dumps({
            "ok": False,
            "data": None,
            "error": {
                "code": "tool_reported_error",
                "message": joined or f"'{tool_name}' reported an error",
                "details": {"server": server_name},
            },
        })
    if not joined:
        return json.dumps({"ok": True, "data": None, "error": None})
    # Tools in this pack already return the envelope as JSON text; pass it
    # through untouched rather than double-encoding it.
    try:
        json.loads(joined)
        return joined
    except json.JSONDecodeError:
        return json.dumps({"ok": True, "data": joined, "error": None})
