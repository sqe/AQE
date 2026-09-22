"""Governed adapter between AQE workflows and the official GitHub MCP server."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from observability.metrics import PrometheusMiddleware
from utils.mcp_client import MCPClient, MCPError


MCP_URL = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
MCP_TOKEN = os.getenv("GITHUB_MCP_TOKEN", "")
ALLOWED_TOOLS = {
    item.strip()
    for item in os.getenv(
        "GITHUB_MCP_ALLOWED_TOOLS",
        "get_file_contents,get_commit,list_branches,search_code",
    ).split(",")
    if item.strip()
}


def _allowed_repositories() -> set[str]:
    return {
        item.strip().lower()
        for item in os.getenv("GITHUB_SOURCE_ALLOWED_REPOSITORIES", "").split(",")
        if item.strip()
    }


def _repository(arguments: dict[str, Any]) -> str | None:
    owner = arguments.get("owner")
    repo = arguments.get("repo") or arguments.get("repository")
    if isinstance(owner, str) and isinstance(repo, str):
        return f"{owner}/{repo}".lower()
    if isinstance(repo, str) and repo.count("/") == 1:
        return repo.lower()
    return None


def authorize_tool(name: str, arguments: dict[str, Any]) -> None:
    if name not in ALLOWED_TOOLS:
        raise HTTPException(status_code=403, detail=f"MCP tool is not allowlisted: {name}")
    repository = _repository(arguments)
    if repository and repository not in _allowed_repositories():
        raise HTTPException(status_code=403, detail=f"repository is not allowlisted: {repository}")


app = FastAPI(title="AQE GitHub MCP Connector", version="1.0.0")


def _client() -> MCPClient:
    if not MCP_TOKEN:
        raise HTTPException(status_code=503, detail="GITHUB_MCP_TOKEN is not configured")
    return MCPClient(MCP_URL, MCP_TOKEN)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy" if MCP_TOKEN else "degraded", "transport": "mcp-streamable-http"}


@app.get("/agent_card")
@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    return {
        "name": "aqe-github-connector",
        "version": "1.0.0",
        "status": "UP" if MCP_TOKEN else "DEGRADED",
        "description": "Governed GitHub MCP tool connector for AQE",
        "skills": [
            {"id": "github.tools.list", "description": "Discover enabled GitHub MCP tools"},
            {"id": "github.tools.call", "description": "Invoke an allowlisted GitHub MCP tool"},
        ],
        "recommendation": None if MCP_TOKEN else "Configure GITHUB_MCP_TOKEN in the runtime Secret.",
    }


@app.get("/v1/tools")
async def tools() -> dict[str, Any]:
    client = _client()
    try:
        discovered = await client.list_tools()
        return {"tools": [tool for tool in discovered if tool.get("name") in ALLOWED_TOOLS]}
    except (httpx.HTTPError, MCPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@app.post("/v1/tools/{name}")
async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    authorize_tool(name, arguments)
    client = _client()
    try:
        return await client.call_tool(name, arguments)
    except (httpx.HTTPError, MCPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


app = PrometheusMiddleware(app, "github-mcp-connector")
