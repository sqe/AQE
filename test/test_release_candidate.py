import importlib.util

import pytest

spec = importlib.util.spec_from_file_location(
    "verify_release_candidate", ".github/scripts/verify-release-candidate.py"
)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def test_deployment_gate_rejects_mixed_candidate_images():
    payload = {
        "items": [
            {
                "metadata": {"name": "aqe-test-generation"},
                "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "ghcr.io/sqe/aqe-test-generation:main"}]}}},
                "status": {"availableReplicas": 1},
            }
        ]
    }

    with pytest.raises(SystemExit, match="wrong_images"):
        verify.verify_deployments(payload, "sha-1234567")


def test_fleet_gate_rejects_requirements_needed_as_false_pass():
    payload = {
        "status": "COMPLETED",
        "result": {
            "successful": True,
            "summary": {"failed": 0},
            "runs": [{"result": {"status": "REQUIREMENTS_NEEDED"}}],
        },
    }

    with pytest.raises(SystemExit, match="requirements_needed=1"):
        verify.verify_fleet(payload)


def test_connected_model_and_fully_passing_fleet_are_promotable():
    verify.verify_model({"llm_provider": {"configured": True, "status": "connected"}})
    verify.verify_fleet(
        {
            "status": "COMPLETED",
            "result": {
                "successful": True,
                "summary": {"failed": 0},
                "runs": [
                    {
                        "successful": True,
                        "result": {
                            "test_catalog": {
                                "commit_url": "https://github.com/sqe/AQE/commit/" + "a" * 40
                            }
                        },
                    }
                ],
            },
        }
    )


def test_catalog_commits_are_extracted_from_successful_fleet_evidence():
    commit = "a" * 40
    payload = {
        "result": {
            "runs": [
                {"result": {"test_catalog": {"commit_url": f"https://github.com/sqe/AQE/commit/{commit}"}}}
            ]
        }
    }

    assert verify.catalog_commits(payload) == [commit]
