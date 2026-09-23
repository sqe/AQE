import json
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from agents.github_connector import app as connector
from utils.mcp_client import MCPClient


def test_connector_rejects_unapproved_tool(monkeypatch):
    monkeypatch.setattr(connector, "ALLOWED_TOOLS", {"get_file_contents"})

    with pytest.raises(HTTPException) as error:
        connector.authorize_tool("push_files", {"owner": "sqe", "repo": "AQE"})

    assert error.value.status_code == 403


def test_connector_rejects_unapproved_repository(monkeypatch):
    monkeypatch.setattr(connector, "ALLOWED_TOOLS", {"get_file_contents"})
    monkeypatch.setenv("GITHUB_SOURCE_ALLOWED_REPOSITORIES", "sqe/AQE")

    with pytest.raises(HTTPException) as error:
        connector.authorize_tool("get_file_contents", {"owner": "other", "repo": "private"})

    assert error.value.status_code == 403


def test_connector_rejects_code_search_without_repository_scope(monkeypatch):
    monkeypatch.setattr(connector, "ALLOWED_TOOLS", {"search_code"})

    with pytest.raises(HTTPException) as error:
        connector.authorize_tool("search_code", {"query": "filename:agent.yaml"})

    assert error.value.status_code == 403


def test_mcp_client_decodes_streamable_http_event():
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'result': {'tools': []}})}\n\n",
    )

    assert MCPClient._decode(response)["result"]["tools"] == []


def test_status_lists_connected_allowlisted_repositories(monkeypatch):
    class FakeClient:
        async def list_tools(self):
            return [{"name": "get_file_contents"}, {"name": "push_files"}]

        async def close(self):
            return None

    monkeypatch.setattr(connector, "MCP_TOKEN", "configured")
    monkeypatch.setattr(connector, "ALLOWED_TOOLS", {"get_file_contents"})
    monkeypatch.setenv("GITHUB_SOURCE_ALLOWED_REPOSITORIES", "sqe/AQE")
    monkeypatch.setattr(connector, "_client", FakeClient)

    result = asyncio.run(connector.status())

    assert result["source_repositories"] == ["sqe/aqe"]


def test_agent_card_declares_executable_contract_for_every_skill(monkeypatch):
    monkeypatch.setattr(connector, "MCP_TOKEN", "configured")

    card = asyncio.run(connector.agent_card())

    assert all(skill["invocation"]["url"] for skill in card["skills"])


def test_agent_card_declares_safe_semantic_cases_for_connected_repository(monkeypatch):
    monkeypatch.setattr(connector, "MCP_TOKEN", "configured")
    monkeypatch.setattr(connector, "ALLOWED_TOOLS", {"list_branches"})
    monkeypatch.setenv("GITHUB_SOURCE_ALLOWED_REPOSITORIES", "sqe/AQE")

    card = asyncio.run(connector.agent_card())
    branch_case = next(case for case in card["evaluation"]["cases"] if case["skill_id"] == "github.tools.call")

    assert branch_case["invocation"]["url"].endswith("/v1/tools/list_branches")
    assert branch_case["prompt"] == {"owner": "sqe", "repo": "aqe"}
    assert branch_case["expected_response"]["malformed_input"]["isError"] is True


def test_repository_discovery_reads_agent_manifests(monkeypatch):
    class FakeClient:
        async def call_tool(self, name, _arguments):
            if name == "search_code":
                return {"content": [{"type": "text", "text": json.dumps({"items": [{"path": "agents/lesson/agent.yaml", "sha": "blob"}]})}]}
            return {
                "content": [
                    {
                        "type": "resource",
                        "resource": {
                            "uri": f"repo://sqe/agents/sha/{'a' * 40}/contents/agents/lesson/agent.yaml",
                            "text": "kind: Agent\nmetadata: {name: lesson}\nspec: {skills: [lesson.explain]}",
                        },
                    }
                ]
            }

        async def close(self):
            return None

    monkeypatch.setattr(connector, "MCP_TOKEN", "configured")
    monkeypatch.setattr(connector, "ALLOWED_TOOLS", {"search_code", "get_file_contents"})
    monkeypatch.setenv("GITHUB_SOURCE_ALLOWED_REPOSITORIES", "sqe/agents")
    monkeypatch.setattr(connector, "_client", FakeClient)

    result = asyncio.run(connector.discover_repository_agents())

    assert result["agents"][0]["source_ref"] == "a" * 40
