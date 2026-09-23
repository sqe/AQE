from utils.github_test_catalog import catalog_path, layer_from_code, publish_test, suite_id_from_code


def test_catalog_path_is_versioned_and_safe():
    path = catalog_path(
        "Claims Agent / EU",
        "v2.1 beta",
        "task-42-unique",
        suite_id="claim-eligibility",
        source_revision="abc123def456",
        test_layer="agentic",
    )

    assert path == (
        "generated-tests/agent/agentic/claims-agent-eu/v2.1-beta/abc123def456/"
        "test_claims_agent_eu__claim_eligibility__v2_1_beta__task-42.py"
    )


def test_catalog_publish_is_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("TEST_CATALOG_REPOSITORY", raising=False)
    monkeypatch.delenv("TEST_CATALOG_GITHUB_TOKEN", raising=False)

    result = publish_test(
        agent_id="teacher",
        agent_version="1.0",
        task_id="task-1",
        code="def test_lesson():\n    assert True\n",
        metadata={},
    )

    assert result is None


def test_confirmed_finding_uses_separate_catalog_root():
    path = catalog_path("claims", "2.1", "task-42", outcome="confirmed_product_defect")

    assert path == (
        "generated-findings/agent/integration/claims/2.1/"
        "test_claims__capability_validation__2_1__task-42.py"
    )


def test_catalog_reads_semantic_suite_and_layer_without_execution():
    code = 'AQE_TEST_LAYER = "db"\nAQE_SUITE_ID = "dbt-project-validation"\n'

    assert (layer_from_code(code), suite_id_from_code(code)) == ("db", "dbt-project-validation")
