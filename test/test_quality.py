import asyncio
import pytest
from agents.test_execution.app import _safe_environment, catalog_outcome, decode_json_column, execute_code
from agents.test_execution.test_quality import REQUIRED_SKILL_DIMENSIONS, inspect_test_code


def test_quality_gate_rejects_multiple_assertions():
    code = """
def test_checkout():
    assert 200 == 200
    assert "paid" == "paid"
"""

    issues = inspect_test_code(code)

    assert [issue.rule for issue in issues] == ["single-outcome"]


def test_execution_decodes_jsonb_profile_returned_as_text():
    profile = decode_json_column('{"skill_test_dimensions":{"github.tools.call":["positive"]}}', dict, "target_agent_profile")

    assert profile == {"skill_test_dimensions": {"github.tools.call": ["positive"]}}


def test_execution_rejects_jsonb_profile_with_wrong_shape():
    with pytest.raises(ValueError, match="target_agent_profile must decode to dict"):
        decode_json_column('["not", "an", "object"]', dict, "target_agent_profile")


def test_quality_gate_rejects_test_without_outcome():
    issues = inspect_test_code("def test_checkout():\n    value = 1\n")

    assert [issue.rule for issue in issues] == ["observable-outcome"]


def test_quality_gate_rejects_cluster_only_endpoint_without_ci_override():
    code = 'URL = "http://aqe-diagnostics:8006/agent_card"\n\ndef test_card():\n    assert URL\n'

    issues = inspect_test_code(code)

    assert [issue.rule for issue in issues] == ["portable-endpoint"]


def test_quality_gate_accepts_cluster_fallback_with_ci_override():
    code = 'import os\nURL = os.getenv("AGENT_CARD_URL", "http://aqe-diagnostics:8006/agent_card")\n\ndef test_card():\n    assert URL\n'

    issues = inspect_test_code(code)

    assert issues == []


def test_quality_gate_rejects_omitted_advertised_skill():
    code = 'AQE_TEST_LAYER = "agentic"\nAQE_SUITE_ID = "research-capabilities"\n\ndef test_card():\n    assert 200 == 200\n'

    issues = inspect_test_code(code, ["research.run"])

    assert [issue.rule for issue in issues] == ["skill-coverage-plan"]


def test_quality_gate_rejects_incomplete_skill_dimensions():
    code = '''
AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "research-capabilities"
AQE_SKILL_TESTS = {"research.run": {"positive": "test_research_run_positive"}}

def test_research_run_positive():
    assert 200 == 200
'''

    issues = inspect_test_code(code, ["research.run"])

    assert [issue.rule for issue in issues] == ["advertised-skill-coverage"]


def test_quality_gate_accepts_complete_atomic_skill_plan():
    code = '''
AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "research-capabilities"
AQE_SKILL_TESTS = {
    "research.run": {
        "positive": "test_research_run_positive",
        "protocol_schema": "test_research_run_protocol_schema",
        "malformed_input": "test_research_run_malformed_input",
        "latency": "test_research_run_latency",
    }
}

def test_research_run_positive():
    assert 200 == 200

def test_research_run_protocol_schema():
    assert isinstance({}, dict)

def test_research_run_malformed_input():
    assert 400 < 500

def test_research_run_latency():
    assert 10 < 1000
'''

    issues = inspect_test_code(code, ["research.run"])

    assert issues == []


def test_quality_gate_accepts_multiple_atomic_tests_per_skill_dimension():
    code = '''
AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "research-capabilities"
AQE_SKILL_TESTS = {
    "research.run": {
        "positive": "test_research_run_positive",
        "protocol_schema": [
            "test_research_run_protocol_schema_status",
            "test_research_run_protocol_schema_payload",
        ],
        "malformed_input": "test_research_run_malformed_input",
        "latency": "test_research_run_latency",
    }
}

def test_research_run_positive(): assert 200 == 200
def test_research_run_protocol_schema_status(): assert 200 == 200
def test_research_run_protocol_schema_payload(): assert isinstance({}, dict)
def test_research_run_malformed_input(): assert 400 < 500
def test_research_run_latency(): assert 10 < 1000
'''

    issues = inspect_test_code(code, ["research.run"])

    assert issues == []


def test_quality_gate_rejects_unconditional_success():
    issues = inspect_test_code("def test_skill():\n    assert True\n")

    assert [issue.rule for issue in issues] == ["non-trivial-outcome"]


def test_quality_gate_requires_semantic_test_when_scenario_has_oracle():
    code = '''
AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "teaching-explanations"
AQE_SKILL_TESTS = {
    "teach.explain": {
        "positive": "test_teach_explain_positive",
        "protocol_schema": "test_teach_explain_protocol_schema",
        "malformed_input": "test_teach_explain_malformed_input",
        "latency": "test_teach_explain_latency",
    }
}

def test_teach_explain_positive(): assert 200 == 200
def test_teach_explain_protocol_schema(): assert isinstance({}, dict)
def test_teach_explain_malformed_input(): assert 400 < 500
def test_teach_explain_latency(): assert 10 < 1000
'''

    issues = inspect_test_code(
        code,
        ["teach.explain"],
        {"teach.explain": [*sorted(REQUIRED_SKILL_DIMENSIONS), "semantic_accuracy"]},
    )

    assert [issue.rule for issue in issues] == ["advertised-skill-coverage"]


def test_quality_gate_requires_semantic_suite_name_for_catalog_candidates():
    issues = inspect_test_code(
        'AQE_TEST_LAYER = "api"\n\ndef test_checkout_total():\n    assert 42 == 42\n',
        require_semantic_names=True,
    )

    assert [issue.rule for issue in issues] == ["semantic-suite-name"]


def test_quality_gate_rejects_unknown_test_layer():
    code = 'AQE_TEST_LAYER = "misc"\nAQE_SUITE_ID = "checkout-total"\n\ndef test_checkout_total():\n    assert 42 == 42\n'

    issues = inspect_test_code(code, require_semantic_names=True)

    assert [issue.rule for issue in issues] == ["test-layer"]


def test_executor_reports_a_passing_atomic_test():
    result = asyncio.run(execute_code("def test_answer():\n    assert 6 * 7 == 42\n"))

    assert result["successful"] is True


def test_executor_exposes_target_auth_helper_to_generated_test():
    code = "from utils.target_auth import authenticated_client\n\ndef test_helper():\n    assert callable(authenticated_client)\n"

    result = asyncio.run(execute_code(code))

    assert result["successful"] is True


def test_executor_reports_failed_assertion():
    result = asyncio.run(execute_code("def test_answer():\n    assert 6 * 7 == 41\n"))

    assert result["summary"]["failed"] == 1


def test_executor_forwards_only_target_auth_credentials(monkeypatch):
    monkeypatch.setenv("TARGET_AUTH_TOKEN", "target-secret")
    monkeypatch.setenv("REPAIR_LLM_API_KEY", "platform-secret")

    environment = _safe_environment()

    assert {key for key in environment if key.endswith("API_KEY") or key.endswith("TOKEN")} == {"TARGET_AUTH_TOKEN"}


def test_assertion_failure_with_confirmed_evidence_is_cataloged_as_finding():
    result = {"successful": False, "summary": {"failed": 1, "errors": 0}}

    assert catalog_outcome(result, "confirmed_product_defect", "Reproduced against version 3.4.1") == "confirmed_product_defect"


def test_runtime_error_is_not_cataloged_as_product_finding():
    result = {"successful": False, "summary": {"failed": 0, "errors": 1}}

    assert catalog_outcome(result, "confirmed_product_defect", "Observed once") is None
