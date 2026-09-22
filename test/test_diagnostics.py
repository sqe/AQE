import asyncio

from agents.diagnostics import app as agent


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"name": "research-agent", "skills": [{"id": "research.run"}]}


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, _):
        return FakeResponse()


def test_agent_probe_validates_declared_skill(monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_ALLOWED_HOSTS", "agent.example")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_: FakeClient())

    result = asyncio.run(
        agent.probe_card("research", "https://agent.example/card", ["research.run"])
    )

    assert result["status"] == "healthy"


def test_agent_probe_reports_missing_skill(monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_ALLOWED_HOSTS", "agent.example")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_: FakeClient())

    result = asyncio.run(
        agent.probe_card("research", "https://agent.example/card", ["report.generate"])
    )

    assert result["missing_skills"] == ["report.generate"]


def test_agent_probe_blocks_unapproved_host(monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_ALLOWED_HOSTS", "internal.example")

    result = asyncio.run(agent.probe_card("external", "https://external.example/card"))

    assert result["status"] == "blocked"


def test_generated_test_event_adds_graph_node():
    agent.GRAPH_EVENTS.clear()

    asyncio.run(agent.graph_event({"kind": "test_generated", "task_id": "task-42", "test_type": "agent"}))
    graph = agent._base_graph({"agents": []})

    assert any(node["id"] == "test:task-42" for node in graph["nodes"])
