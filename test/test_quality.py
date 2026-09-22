import asyncio
from agents.test_execution.app import _safe_environment, catalog_outcome, execute_code
from agents.test_execution.test_quality import inspect_test_code


def test_quality_gate_rejects_multiple_assertions():
    code = """
def test_checkout():
    assert 200 == 200
    assert "paid" == "paid"
"""

    issues = inspect_test_code(code)

    assert [issue.rule for issue in issues] == ["single-outcome"]


def test_quality_gate_rejects_test_without_outcome():
    issues = inspect_test_code("def test_checkout():\n    value = 1\n")

    assert [issue.rule for issue in issues] == ["observable-outcome"]


def test_executor_reports_a_passing_atomic_test():
    result = asyncio.run(execute_code("def test_answer():\n    assert 6 * 7 == 42\n"))

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
