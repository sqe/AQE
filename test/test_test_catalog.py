from utils.github_test_catalog import catalog_path, publish_test


def test_catalog_path_is_versioned_and_safe():
    path = catalog_path("Claims Agent / EU", "v2.1 beta", "task/42")

    assert path == "generated-tests/agent/claims-agent-eu/v2.1-beta/test_task-42.py"


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

    assert path == "generated-findings/agent/claims/2.1/test_task-42.py"
