import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agents.test_generation import app
from agents.test_generation.app import (
    TestGenerationAgentLogic as GenerationAgentLogic,
    compile_declared_github_mcp_suite,
    generate_tests_handler,
    generation_fingerprint,
    llm_provider_status,
    resolve_target_identity,
)
from agents.test_execution.test_quality import inspect_test_code


def test_declared_agent_identity_takes_precedence():
    identity = resolve_target_identity(
        {"target_agent": {"id": "claims-adjudicator"}, "url": "https://generic.example"}
    )

    assert identity == "claims-adjudicator"


def test_agent_card_hostname_is_stable_fallback():
    identity = resolve_target_identity(
        {"agent_card_url": "https://lesson-agent.example/.well-known/agent.json"}
    )

    assert identity == "lesson-agent.example"


def test_missing_target_identity_never_uses_generic_app_name():
    with pytest.raises(ValueError, match="stable target agent identity"):
        resolve_target_identity({})


def test_self_hosted_provider_connectivity_uses_models_endpoint():
    async def respond(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": []})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await llm_provider_status(
                mode="SELF_HOSTED",
                api_key="",
                generation_endpoint="http://model.internal:1234/v1/chat/completions",
                client=client,
            )

    result = asyncio.run(check())

    assert result["mode"] == "SELF_HOSTED"
    assert result["status"] == "connected"
    assert result["configured"] is True


def test_gemini_provider_connectivity_validates_key_without_exposing_it():
    async def respond(request):
        assert request.url.params["key"] == "secret-key"
        return httpx.Response(200, json={"name": "models/gemini-2.5-flash"})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await llm_provider_status(
                mode="GEMINI",
                api_key="secret-key",
                generation_endpoint="",
                client=client,
            )

    result = asyncio.run(check())

    assert result["status"] == "connected"
    assert "secret-key" not in json.dumps(result)


def test_gemini_provider_reports_missing_configuration_without_network_call():
    result = asyncio.run(
        llm_provider_status(mode="GEMINI", api_key="", generation_endpoint="")
    )

    assert result == {
        "mode": "GEMINI",
        "configured": False,
        "status": "not_configured",
        "model": app.GEMINI_GENERATION_MODEL,
    }


def test_generation_endpoint_preserves_artifact_provenance(monkeypatch):
    expected = {
        "status": "SUCCESS",
        "task_id": "task-42",
        "rag_version_id": "rag-7",
        "object_path": "artifacts/targets/teacher/tests/task-42/test_code.py",
        "test_type": "agent",
        "grounding": {"document_ids": ["requirements-3"]},
    }

    class Request:
        async def json(self):
            return {"url": "https://teacher.example"}

    async def generate(_body):
        return expected

    monkeypatch.setattr(app.AGENT_LOGIC, "generate_tests_and_persist", generate)

    response = asyncio.run(generate_tests_handler(Request()))

    assert json.loads(response.body) == expected


def test_generation_fingerprint_is_stable_for_reordered_skills():
    left = generation_fingerprint(
        {"url": "https://agent.example", "agent_name": "teacher", "skills": ["teach", "listen"]}
    )
    right = generation_fingerprint(
        {"url": "https://agent.example", "agent_name": "teacher", "skills": ["listen", "teach"]}
    )

    assert left == right


def test_generation_fingerprint_changes_with_source_tree():
    previous = generation_fingerprint(
        {"url": "https://agent.example", "agent_name": "teacher", "source_analysis": {"tree_sha": "abc"}}
    )
    current = generation_fingerprint(
        {"url": "https://agent.example", "agent_name": "teacher", "source_analysis": {"tree_sha": "def"}}
    )

    assert previous != current


def test_unchanged_approved_suite_skips_model_generation(monkeypatch):
    logic = object.__new__(GenerationAgentLogic)

    async def find_reusable(_fingerprint):
        return {"status": "SUCCESS", "task_id": "approved-42", "reused": True}

    async def unexpected_generation(_captured_state):
        raise AssertionError("model generation must not run for an unchanged approved suite")

    monkeypatch.setattr(logic, "_find_reusable_test", find_reusable)
    monkeypatch.setattr(logic, "generate_tests", unexpected_generation)

    result = asyncio.run(
        logic.generate_tests_and_persist({"url": "https://agent.example", "agent_name": "teacher"})
    )

    assert result["reused"] is True


def test_generation_response_is_not_blocked_by_graph_telemetry(monkeypatch):
    logic = object.__new__(GenerationAgentLogic)

    async def no_reusable_candidate(_fingerprint):
        return None

    async def generated_suite(_captured_state):
        return {"test_code": "def test_contract():\n    assert True\n", "artifact_version_used": "rag-1"}

    async def store_artifact(_task_id, _test_code, _target_identity):
        return "artifacts/targets/connector/tests/task/test_code.py"

    async def create_metadata(*_args):
        return None

    async def hanging_graph_event(*_args, **_kwargs):
        await asyncio.Event().wait()

    logic.llm_service = SimpleNamespace(client=SimpleNamespace(post=hanging_graph_event))
    monkeypatch.setattr(logic, "_find_reusable_test", no_reusable_candidate)
    monkeypatch.setattr(logic, "generate_tests", generated_suite)
    monkeypatch.setattr(logic, "_store_test_artifact", store_artifact)
    monkeypatch.setattr(logic, "_create_test_run_metadata_in_postgres", create_metadata)
    monkeypatch.setattr(app, "GRAPH_EVENT_TIMEOUT_SECONDS", 0.01)

    result = asyncio.run(
        logic.generate_tests_and_persist(
            {"url": "https://connector.example", "agent_name": "github-connector"}
        )
    )

    assert result["status"] == "SUCCESS"


def test_declared_github_mcp_contract_compiles_to_deep_atomic_suite():
    scenarios = [
        {
            "scenario_id": "list-enabled-github-tools",
            "skill_id": "github.tools.list",
            "invocation": {"url": "http://aqe-github-connector:8014/v1/tools"},
            "expected_response": {
                "allowed_tool_names": ["search_code", "list_branches"],
            },
            "required_dimensions": ["positive", "protocol_schema", "latency", "semantic_accuracy"],
            "max_latency_ms": 5000,
        },
        {
            "scenario_id": "call-allowlisted-list-branches",
            "skill_id": "github.tools.call",
            "invocation": {"url": "http://aqe-github-connector:8014/v1/tools/list_branches"},
            "prompt": {"owner": "sqe", "repo": "aqe"},
            "expected_response": {
                "branch_payload": {
                    "type": "array",
                    "may_be_empty": True,
                    "item_required_fields": ["name", "sha", "protected"],
                },
                "malformed_input": {
                    "request": {},
                    "status_code": 200,
                    "isError": True,
                    "message_contains": "missing required parameter: owner",
                },
            },
            "required_dimensions": [
                "positive",
                "protocol_schema",
                "malformed_input",
                "latency",
                "semantic_accuracy",
            ],
            "max_latency_ms": 10000,
        },
    ]

    code = compile_declared_github_mcp_suite(
        {"url": "http://aqe-github-connector:8014", "scenarios": scenarios}
    )
    issues = inspect_test_code(
        code or "",
        required_skills=["github.tools.list", "github.tools.call"],
        required_dimensions_by_skill={
            scenario["skill_id"]: scenario["required_dimensions"] for scenario in scenarios
        },
        require_semantic_names=True,
    )

    assert (
        issues == []
        and (code or "").count("\ndef test_") == 24
        and "set(names).issubset(set(ALLOWED_TOOL_NAMES))" in (code or "")
        and "== ALLOWED_TOOL_NAMES" not in (code or "")
    )
