"""Deterministically compiled GitHub MCP connector contract tests."""

import json
import os
import time

import httpx
import pytest


AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "github-mcp-connector-contract"
AQE_SKILL_TESTS = {
    "github.tools.list": {
        "positive": "test_github_tools_list_positive",
        "protocol_schema": [
            "test_github_tools_list_protocol_schema_body_object",
            "test_github_tools_list_protocol_schema_tools_array",
            "test_github_tools_list_protocol_schema_tools_non_empty",
            "test_github_tools_list_protocol_schema_tool_objects",
            "test_github_tools_list_protocol_schema_tool_name_strings",
        ],
        "latency": "test_github_tools_list_latency",
        "semantic_accuracy": "test_github_tools_list_semantic_accuracy",
    },
    "github.tools.call": {
        "positive": "test_github_tools_call_positive",
        "protocol_schema": [
            "test_github_tools_call_protocol_schema_content_type",
            "test_github_tools_call_protocol_schema_content_array",
            "test_github_tools_call_protocol_schema_content_non_empty",
            "test_github_tools_call_protocol_schema_text_block",
            "test_github_tools_call_protocol_schema_json_decodable",
            "test_github_tools_call_protocol_schema_payload_array",
            "test_github_tools_call_protocol_schema_branch_objects",
            "test_github_tools_call_protocol_schema_branch_name_field",
            "test_github_tools_call_protocol_schema_branch_sha_field",
            "test_github_tools_call_protocol_schema_branch_protected_field",
        ],
        "malformed_input": [
            "test_github_tools_call_malformed_input_status",
            "test_github_tools_call_malformed_input_error_flag",
            "test_github_tools_call_malformed_input_message",
        ],
        "latency": "test_github_tools_call_latency",
        "semantic_accuracy": "test_github_tools_call_semantic_accuracy",
    },
}

AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", 'http://aqe-github-connector:8014')
TOOLS_PATH = '/v1/tools'
LIST_BRANCHES_PATH = '/v1/tools/list_branches'
ALLOWED_TOOL_NAMES = ['get_commit', 'get_file_contents', 'list_branches', 'search_code']
LIST_BRANCHES_ARGUMENTS = {'owner': 'sqe', 'repo': 'aqe'}
REQUIRED_BRANCH_FIELDS = ['name', 'sha', 'protected']
MALFORMED_ARGUMENTS = {}
MALFORMED_MESSAGE = 'missing required parameter: owner'


@pytest.fixture
def client():
    with httpx.Client(base_url=AGENT_BASE_URL, timeout=30) as value:
        yield value


def _decoded_mcp_text(response):
    # The MCP envelope must contain a text block. Its decoded branch array may validly be [].
    body = response.json()
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("MCP response must contain at least one content block")
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text" or not isinstance(first.get("text"), str):
        raise ValueError("MCP response must start with a text content block")
    try:
        return json.loads(first["text"])
    except json.JSONDecodeError as exc:
        raise ValueError("MCP text content is not valid JSON") from exc


def test_github_tools_list_positive(client):
    response = client.get(TOOLS_PATH)
    assert response.status_code == 200


def test_github_tools_list_protocol_schema_body_object(client):
    body = client.get(TOOLS_PATH).json()
    assert isinstance(body, dict)


def test_github_tools_list_protocol_schema_tools_array(client):
    tools = client.get(TOOLS_PATH).json().get("tools")
    assert isinstance(tools, list)


def test_github_tools_list_protocol_schema_tools_non_empty(client):
    tools = client.get(TOOLS_PATH).json().get("tools")
    assert isinstance(tools, list) and bool(tools)


def test_github_tools_list_protocol_schema_tool_objects(client):
    tools = client.get(TOOLS_PATH).json().get("tools")
    assert isinstance(tools, list) and all(isinstance(tool, dict) for tool in tools)


def test_github_tools_list_protocol_schema_tool_name_strings(client):
    tools = client.get(TOOLS_PATH).json().get("tools")
    assert isinstance(tools, list) and all(isinstance(tool.get("name"), str) for tool in tools if isinstance(tool, dict))


def test_github_tools_list_latency(client):
    started = time.monotonic()
    client.get(TOOLS_PATH)
    assert time.monotonic() - started <= 5.0


def test_github_tools_list_semantic_accuracy(client):
    tools = client.get(TOOLS_PATH).json()["tools"]
    names = [tool["name"] for tool in tools]
    assert bool(names) and set(names).issubset(set(ALLOWED_TOOL_NAMES))


def test_github_tools_call_positive(client):
    response = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS)
    assert response.status_code == 200


def test_github_tools_call_protocol_schema_content_type(client):
    response = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS)
    assert response.headers.get("content-type", "").startswith("application/json")


def test_github_tools_call_protocol_schema_content_array(client):
    response = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS)
    content = response.json().get("content")
    assert isinstance(content, list)


def test_github_tools_call_protocol_schema_content_non_empty(client):
    content = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS).json().get("content")
    assert isinstance(content, list) and bool(content)


def test_github_tools_call_protocol_schema_text_block(client):
    content = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS).json().get("content")
    assert isinstance(content, list) and bool(content) and isinstance(content[0], dict) and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)


def test_github_tools_call_protocol_schema_json_decodable(client):
    payload = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert payload is not None


def test_github_tools_call_protocol_schema_payload_array(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list)


def test_github_tools_call_protocol_schema_branch_objects(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list) and all(isinstance(branch, dict) for branch in branches)


def test_github_tools_call_protocol_schema_branch_name_field(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list) and all(isinstance(branch, dict) and "name" in branch for branch in branches)


def test_github_tools_call_protocol_schema_branch_sha_field(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list) and all(isinstance(branch, dict) and "sha" in branch for branch in branches)


def test_github_tools_call_protocol_schema_branch_protected_field(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list) and all(isinstance(branch, dict) and "protected" in branch for branch in branches)


def test_github_tools_call_malformed_input_status(client):
    response = client.post(LIST_BRANCHES_PATH, json=MALFORMED_ARGUMENTS)
    assert response.status_code == 200


def test_github_tools_call_malformed_input_error_flag(client):
    body = client.post(LIST_BRANCHES_PATH, json=MALFORMED_ARGUMENTS).json()
    assert body.get("isError") is True


def test_github_tools_call_malformed_input_message(client):
    response = client.post(LIST_BRANCHES_PATH, json=MALFORMED_ARGUMENTS)
    body = response.json()
    texts = [block.get("text", "") for block in body.get("content", []) if isinstance(block, dict)]
    assert any(MALFORMED_MESSAGE in text for text in texts)


def test_github_tools_call_latency(client):
    started = time.monotonic()
    client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS)
    assert time.monotonic() - started <= 10.0


def test_github_tools_call_semantic_accuracy(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    # all(...) over [] is intentional because the declared branch payload may be empty.
    assert isinstance(branches, list) and all(isinstance(branch, dict) and all(field in branch for field in REQUIRED_BRANCH_FIELDS) for branch in branches)
