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


def test_agent_probe_classifies_quality_agent_from_card_evidence(monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_ALLOWED_HOSTS", "agent.example")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_: FakeClient())

    result = asyncio.run(agent.probe_card("test-generation", "https://agent.example/card"))

    assert result["ontology_product_name"] == "AI Buster"


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


def test_configured_fleet_card_is_allowed_without_duplicate_host_allowlist(monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_ALLOWED_HOSTS", "")
    monkeypatch.setenv("AGENT_CARD_URLS", "builder=http://configured-builder:8015/agent_card")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_: FakeClient())

    result = asyncio.run(agent.probe_card("builder", "http://configured-builder:8015/agent_card"))

    assert result["status"] == "healthy"


def test_generated_test_event_adds_graph_node():
    agent.GRAPH_EVENTS.clear()

    asyncio.run(agent.graph_event({"kind": "test_generated", "task_id": "task-42", "test_type": "agent"}))
    graph = agent._base_graph({"agents": []})

    assert any(node["id"] == "test:task-42" for node in graph["nodes"])


def test_completed_event_replaces_generated_test_status():
    agent.GRAPH_EVENTS.clear()
    asyncio.run(agent.graph_event({"kind": "test_generated", "task_id": "task-42", "test_type": "agent"}))
    asyncio.run(
        agent.graph_event(
            {"kind": "test_completed", "task_id": "task-42", "test_type": "agent", "status": "passed"}
        )
    )

    graph = agent._base_graph({"agents": []})
    test_node = next(node for node in graph["nodes"] if node["id"] == "test:task-42")

    assert test_node["status"] == "passed"


def test_generation_stage_activates_temporal_to_generator_route():
    agent.GRAPH_EVENTS.clear()

    asyncio.run(
        agent.graph_event(
            {
                "kind": "test_generation_started",
                "workflow_id": "qe-fleet-42-agent-1",
                "test_type": "agent",
            }
        )
    )

    event = agent.GRAPH_EVENTS[0]
    assert event["status"] == "running"
    assert event["edges"] == [
        {"source": "system:temporal", "target": "agent:test-generation", "type": "generate_tests"}
    ]


def test_graph_deduplicates_repeated_lifecycle_routes():
    agent.GRAPH_EVENTS.clear()
    asyncio.run(
        agent.graph_event(
            {"kind": "test_generation_started", "workflow_id": "qe-fleet-42-agent-1"}
        )
    )

    graph = agent._base_graph({"agents": []})
    generation_routes = [edge for edge in graph["edges"] if edge["type"] == "generate_tests"]

    assert len(generation_routes) == 1


def test_graph_exposes_family_routing_direction():
    graph = agent._base_graph(
        {
            "agents": [{"name": "test-generation", "identity": "generator", "status": "healthy"}],
            "ontology_families": [
                {
                    "id": "canonical:software_quality_engineering",
                    "archetype": "software_quality_engineering",
                    "label": "Software Quality Engineering",
                    "product_name": "AI Buster",
                    "kind": "canonical",
                    "members": ["generator"],
                    "capabilities": ["generate_tests"],
                    "review_required": False,
                }
            ],
        }
    )

    assert any(edge["type"] == "capability_member" for edge in graph["edges"])


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


def test_discovery_preserves_multiple_golden_cases_for_one_skill():
    scenarios = agent._evaluation_scenarios(
        {
            "url": "https://teacher.example/a2a",
            "skills": [{"id": "teach.explain"}],
            "evaluation": {
                "cases": [
                    {"id": "gravity", "skill_id": "teach.explain", "prompt": "Explain gravity"},
                    {"id": "fractions", "skill_id": "teach.explain", "prompt": "Explain fractions"},
                ]
            },
        },
        default_max_latency_ms=3000,
        default_min_accuracy=0.9,
    )

    assert [scenario["scenario_id"] for scenario in scenarios] == ["gravity", "fractions"]


def test_declared_dimensions_omit_non_applicable_malformed_input():
    scenarios = agent._evaluation_scenarios(
        {
            "skills": [{"id": "tools.list", "invocation": {"url": "https://agent.example/tools"}}],
            "evaluation": {
                "cases": [
                    {
                        "id": "list-tools",
                        "skill_id": "tools.list",
                        "prompt": {},
                        "expected_response": {"tools": "array"},
                        "required_dimensions": ["positive", "protocol_schema", "latency"],
                    }
                ]
            },
        },
        default_max_latency_ms=3000,
        default_min_accuracy=1.0,
    )

    assert "malformed_input" not in scenarios[0]["required_dimensions"]


def test_discovery_marks_skill_without_invocation_as_not_executable():
    scenarios = agent._evaluation_scenarios(
        {"skills": [{"id": "finance.quote", "examples": ["Quote policy 42"]}]},
        default_max_latency_ms=3000,
        default_min_accuracy=0.9,
    )

    assert scenarios[0]["execution_status"] == "requirements_needed"


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
