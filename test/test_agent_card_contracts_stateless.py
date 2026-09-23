import asyncio
import importlib
from pathlib import Path


def _assert_complete_contract(card):
    cases = {case["skill_id"]: case for case in card["evaluation"]["cases"]}
    for skill in card["skills"]:
        assert skill["invocation"]["url"]
        case = cases[skill["id"]]
        assert case["prompt"] is not None
        assert case["expected_response"]


def test_agent_builder_and_diagnostics_cards_have_business_contracts():
    from agents.agent_builder.app import agent_card as builder_card
    from agents.diagnostics.app import agent_card as diagnostics_card

    builder = asyncio.run(builder_card())
    diagnostics = asyncio.run(diagnostics_card())
    _assert_complete_contract(builder)
    _assert_complete_contract(diagnostics)
    assert builder["skills"][0]["invocation"]["url"].endswith("/v1/builds")
    assert [skill["invocation"]["url"].rsplit("/", 1)[-1] for skill in diagnostics["skills"]] == ["diagnostics", "agent-probes", "heal"]


def test_github_analysis_uses_allowlisted_immutable_revision(monkeypatch):
    monkeypatch.setenv("GITHUB_SOURCE_ALLOWED_REPOSITORIES", "sqe/AQE")
    monkeypatch.setenv("GITHUB_SOURCE_EVALUATION_REF", "a" * 40)
    module = importlib.import_module("agents.github_analysis.app")
    module = importlib.reload(module)

    card = asyncio.run(module.agent_card())
    _assert_complete_contract(card)
    assert card["evaluation"]["cases"][0]["prompt"]["repository"] == "sqe/AQE"
    assert card["evaluation"]["cases"][0]["prompt"]["ref"] == "a" * 40


def test_webpage_capture_targets_internal_aqe_frontend():
    source = Path("agents/webpage_state_capture/app.py").read_text()

    assert 'AQE_FRONTEND_URL = os.environ.get("AQE_FRONTEND_URL", "http://aqe-frontend:8080/")' in source
    assert '"url": f"{PUBLIC_BASE_URL}/capture"' in source
    assert '"prompt": {"url": AQE_FRONTEND_URL}' in source
    assert '"expected_response": {"url": AQE_FRONTEND_URL, "status": "CAPTURED", "elements": "array"}' in source
