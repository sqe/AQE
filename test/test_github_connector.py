import json

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


def test_mcp_client_decodes_streamable_http_event():
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'result': {'tools': []}})}\n\n",
    )

    assert MCPClient._decode(response)["result"]["tools"] == []
