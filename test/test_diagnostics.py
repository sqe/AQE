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


def test_discovery_uses_declared_expected_response_as_oracle():
    scenarios = agent._evaluation_scenarios(
        {
            "skills": [{"id": "teach.explain", "examples": ["Explain gravity"]}],
            "evaluation": {
                "cases": [
                    {
                        "skill_id": "teach.explain",
                        "prompt": "Explain gravity",
                        "expected_response": "Masses attract each other.",
                    }
                ]
            },
        },
        default_max_latency_ms=3000,
        default_min_accuracy=0.9,
    )

    assert scenarios[0]["evaluation_mode"] == "semantic_accuracy"


def test_discovery_marks_missing_domain_oracle():
    scenarios = agent._evaluation_scenarios(
        {"skills": [{"id": "finance.quote", "examples": ["Quote policy 42"]}]},
        default_max_latency_ms=3000,
        default_min_accuracy=0.9,
    )

    assert scenarios[0]["oracle_status"] == "requirements_needed"


def test_evaluation_metrics_preserve_release_shard():
    asyncio.run(
        agent.record_evaluation(
            {
                "score": 0.9,
                "semantic_score": 0.85,
                "average_latency_ms": 420,
                "passed": 9,
                "total": 10,
                "shard_index": 7,
                "shard_count": 10,
            }
        )
    )

    assert agent.LATEST_EVALUATION["shard_index"] == 7
