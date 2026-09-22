"""Minimal MCP Streamable HTTP client for governed connector agents."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str, token: str, *, timeout: float = 60) -> None:
        self.url = url
        self.session_id: str | None = None
        self._next_id = 0
        self._initialized = False
        self._lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        if "text/event-stream" in response.headers.get("content-type", ""):
            for line in reversed(response.text.splitlines()):
                if line.startswith("data:"):
                    return json.loads(line.removeprefix("data:").strip())
            raise MCPError("MCP event stream returned no JSON data")
        return response.json()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        headers = {"MCP-Protocol-Version": "2025-06-18"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = await self.client.post(
            self.url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params},
        )
        response.raise_for_status()
        self.session_id = response.headers.get("mcp-session-id", self.session_id)
        payload = self._decode(response)
        if payload.get("error"):
            raise MCPError(str(payload["error"]))
        return payload.get("result", {})

    async def _notify(self, method: str) -> None:
        headers = {"MCP-Protocol-Version": "2025-06-18"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = await self.client.post(
            self.url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": method},
        )
        response.raise_for_status()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await self._request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "aqe-github-connector", "version": "1.0.0"},
                },
            )
            await self._notify("notifications/initialized")
            self._initialized = True

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.initialize()
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        return await self._request("tools/call", {"name": name, "arguments": arguments})
