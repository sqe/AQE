from agents.agent_builder.app import REQUIRED_FILES, validate_bundle


def bundle(test_source: str) -> dict[str, str]:
    return {
        "agent/app.py": "def health():\n    return {'status': 'healthy'}\n",
        "agent/requirements.txt": "fastapi==0.118.0\n",
        "agent/Dockerfile": "FROM python:3.12-slim\nUSER 65532:65532\n",
        "agent/agent.yaml": "name: example\nskills:\n  - example.run\n",
        "agent/tests/test_agent.py": test_source,
    }


def test_valid_review_bundle_passes_quality_gate():
    errors = validate_bundle(bundle("def test_response():\n    assert call_agent() == 'expected'\n"))

    assert errors == []


def test_multi_assert_test_is_rejected():
    errors = validate_bundle(
        bundle("def test_response():\n    assert call_agent() == 'expected'\n    assert latency() < 2\n")
    )

    assert any("single-outcome" in error for error in errors)


def test_builder_requires_exact_review_bundle_files():
    errors = validate_bundle({"agent/app.py": "pass\n"})

    assert sorted(REQUIRED_FILES)[0] in errors[0]
