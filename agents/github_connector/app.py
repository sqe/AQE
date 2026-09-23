"""Governed adapter between AQE workflows and the official GitHub MCP server."""

from __future__ import annotations

import os
import json
import re
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from observability.metrics import PrometheusMiddleware
from utils.mcp_client import MCPClient, MCPError


MCP_URL = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
MCP_TOKEN = os.getenv("GITHUB_MCP_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("GITHUB_CONNECTOR_PUBLIC_URL", "http://aqe-github-connector:8014").rstrip("/")
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
    if name == "search_code":
        repositories = {
            match.lower()
            for match in re.findall(r"(?:^|\s)repo:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", str(arguments.get("query", "")))
        }
        if not repositories:
            raise HTTPException(status_code=403, detail="search_code requires an explicit allowlisted repo:owner/name qualifier")
        blocked = repositories - _allowed_repositories()
        if blocked:
            raise HTTPException(status_code=403, detail=f"repository is not allowlisted: {sorted(blocked)[0]}")


app = FastAPI(title="AQE GitHub MCP Connector", version="1.0.0")


def _client() -> MCPClient:
    if not MCP_TOKEN:
        raise HTTPException(status_code=503, detail="GITHUB_MCP_TOKEN is not configured")
    return MCPClient(MCP_URL, MCP_TOKEN)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy" if MCP_TOKEN else "degraded", "transport": "mcp-streamable-http"}


@app.get("/v1/status")
async def status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "degraded",
        "transport": "mcp-streamable-http",
        "endpoint": MCP_URL,
        "source_repositories": sorted(_allowed_repositories()),
        "catalog_repository": os.getenv("TEST_CATALOG_REPOSITORY") or None,
        "catalog_branch": os.getenv("TEST_CATALOG_BRANCH", "aqe-generated-tests"),
        "enabled_tools": sorted(ALLOWED_TOOLS),
        "discovered_tools": [],
    }
    if not MCP_TOKEN:
        return {**result, "message": "GITHUB_MCP_TOKEN is not configured"}
    client = _client()
    try:
        discovered = await client.list_tools()
        result["discovered_tools"] = sorted(
            tool["name"] for tool in discovered
            if tool.get("name") in ALLOWED_TOOLS
        )
        return {**result, "status": "connected", "message": None}
    except (httpx.HTTPError, MCPError) as exc:
        return {**result, "message": str(exc)}
    finally:
        await client.close()


@app.get("/agent_card")
@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    skills = [
        {
            "id": "github.tools.list",
            "description": "Discover enabled GitHub MCP tools",
            "examples": [{}],
            "invocation": {"protocol": "rest", "method": "GET", "url": f"{PUBLIC_BASE_URL}/v1/tools"},
        },
        {
            "id": "github.tools.call",
            "description": "Invoke an allowlisted GitHub MCP tool",
            "examples": [{"name": "list_branches", "arguments": {"owner": "sqe", "repo": "AQE"}}],
            "invocation": {
                "protocol": "rest",
                "method": "POST",
                "url": f"{PUBLIC_BASE_URL}/v1/tools/{{name}}",
            },
        },
    ]
    evaluation_cases: list[dict[str, Any]] = [
        {
            "id": "list-enabled-github-tools",
            "skill_id": "github.tools.list",
            "prompt": {},
            "expected_response": {
                "status_code": 200,
                "tools": "non-empty array",
                "allowed_tool_names": sorted(ALLOWED_TOOLS),
                "rule": "every returned tool name is a member of allowed_tool_names",
            },
            "required_dimensions": ["positive", "protocol_schema", "latency"],
            "max_latency_ms": 5000,
            "min_accuracy": 1.0,
        }
    ]
    repositories = sorted(_allowed_repositories())
    if "list_branches" in ALLOWED_TOOLS and repositories:
        owner, repo = repositories[0].split("/", 1)
        evaluation_cases.append(
            {
                "id": "call-allowlisted-list-branches",
                "skill_id": "github.tools.call",
                "prompt": {"owner": owner, "repo": repo},
                "invocation": {
                    "protocol": "rest",
                    "method": "POST",
                    "url": f"{PUBLIC_BASE_URL}/v1/tools/list_branches",
                },
                "expected_response": {
                    "status_code": 200,
                    "content_type": "application/json",
                    "content": "non-empty array of MCP content blocks",
                    "branch_payload": {
                        "type": "array",
                        "may_be_empty": True,
                        "scope": "JSON decoded from the required non-empty MCP text content block",
                        "empty_array_semantics": "[] is valid; item field constraints apply to every returned item",
                        "item_required_fields": ["name", "sha", "protected"],
                    },
                    "malformed_input": {
                        "request": {},
                        "status_code": 200,
                        "isError": True,
                        "message_contains": "missing required parameter: owner",
                        "note": "GitHub MCP reports tool argument errors in-band by contract",
                    },
                },
                "required_dimensions": ["positive", "protocol_schema", "malformed_input", "latency"],
                "max_latency_ms": 10000,
                "min_accuracy": 1.0,
            }
        )
    return {
        "name": "aqe-github-connector",
        "version": "1.0.0",
        "status": "UP" if MCP_TOKEN else "DEGRADED",
        "description": "Governed GitHub MCP tool connector for AQE",
        "skills": skills,
        "evaluation": {"cases": evaluation_cases},
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


def _mcp_json(result: dict[str, Any]) -> dict[str, Any]:
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                return json.loads(item.get("text", ""))
            except json.JSONDecodeError:
                continue
    return {}


def _mcp_resource(result: dict[str, Any]) -> tuple[str, str]:
    for item in result.get("content", []):
        resource = item.get("resource") if item.get("type") == "resource" else None
        if isinstance(resource, dict) and isinstance(resource.get("text"), str):
            return resource["text"], str(resource.get("uri", ""))
    return "", ""


@app.get("/v1/agents/discover")
async def discover_repository_agents() -> dict[str, Any]:
    """Find declarative agent manifests in every allowlisted connected repository."""
    missing_tools = {"search_code", "get_file_contents"} - ALLOWED_TOOLS
    if missing_tools:
        raise HTTPException(status_code=503, detail=f"repository agent discovery requires tools: {sorted(missing_tools)}")
    client = _client()
    discovered: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    try:
        for repository in sorted(_allowed_repositories()):
            owner, repo = repository.split("/", 1)
            try:
                search = await client.call_tool(
                    "search_code",
                    {
                        "query": f"filename:agent.yaml repo:{repository}",
                        "perPage": 50,
                        "fields": ["name", "path", "sha"],
                    },
                )
                for match in _mcp_json(search).get("items", [])[:50]:
                    path = str(match.get("path", ""))
                    if not path:
                        continue
                    content = await client.call_tool(
                        "get_file_contents",
                        {"owner": owner, "repo": repo, "path": path},
                    )
                    text, uri = _mcp_resource(content)
                    manifest = yaml.safe_load(text) if text else None
                    if not isinstance(manifest, dict) or manifest.get("kind") != "Agent":
                        continue
                    metadata = manifest.get("metadata") or {}
                    spec = manifest.get("spec") or {}
                    if not isinstance(metadata, dict) or not isinstance(spec, dict) or not metadata.get("name"):
                        continue
                    source_ref_match = re.search(r"/sha/([0-9a-f]{40})/", uri)
                    discovered.append(
                        {
                            "repository": repository,
                            "path": path,
                            "source_ref": source_ref_match.group(1) if source_ref_match else None,
                            "blob_sha": match.get("sha"),
                            "name": str(metadata["name"]),
                            "version": str(spec.get("version") or manifest.get("version") or "unversioned"),
                            "skills": spec.get("skills") if isinstance(spec.get("skills"), list) else [],
                            "card_url": spec.get("cardUrl") or spec.get("card_url"),
                            "module": spec.get("module"),
                        }
                    )
            except (httpx.HTTPError, MCPError, yaml.YAMLError) as exc:
                errors.append({"repository": repository, "error": str(exc)})
        return {
            "status": "completed" if not errors else "partial",
            "repositories": sorted(_allowed_repositories()),
            "agents": discovered,
            "summary": {
                "repositories": len(_allowed_repositories()),
                "agents": len(discovered),
                "errors": len(errors),
            },
            "errors": errors,
        }
    finally:
        await client.close()


app = PrometheusMiddleware(app, "github-mcp-connector")
