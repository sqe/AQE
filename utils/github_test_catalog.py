"""Optional versioned GitHub catalog for generated Python E2E tests."""

from __future__ import annotations

import json
import os
import re
from typing import Any


def _segment(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    return normalized or fallback


def catalog_path(
    agent_id: str,
    agent_version: str,
    task_id: str,
    test_type: str = "agent",
    outcome: str = "validated",
) -> str:
    root = "generated-findings" if outcome == "confirmed_product_defect" else "generated-tests"
    return "/".join(
        [
            root,
            _segment(test_type, "agent"),
            _segment(agent_id, "unknown-agent"),
            _segment(agent_version, "unversioned"),
            f"test_{_segment(task_id, 'generated')}.py",
        ]
    )


def publish_test(
    *,
    agent_id: str,
    agent_version: str,
    task_id: str,
    code: str,
    metadata: dict[str, Any],
    test_type: str = "agent",
    outcome: str = "validated",
) -> dict[str, str] | None:
    """Create immutable test and metadata files when a catalog is configured."""
    repository_name = os.getenv("TEST_CATALOG_REPOSITORY", "")
    token = os.getenv("TEST_CATALOG_GITHUB_TOKEN", "")
    if not repository_name or not token:
        return None

    from github import Github
    from github.GithubException import GithubException

    github = Github(token)
    repository = github.get_repo(repository_name)
    branch = os.getenv("TEST_CATALOG_BRANCH", "aqe-generated-tests")
    try:
        repository.get_branch(branch)
    except GithubException as exc:
        if exc.status != 404:
            raise
        base = repository.get_branch(repository.default_branch)
        repository.create_git_ref(ref=f"refs/heads/{branch}", sha=base.commit.sha)

    path = catalog_path(agent_id, agent_version, task_id, test_type, outcome)
    message = f"test({agent_id}@{agent_version}): catalog {outcome} case {task_id}"
    repository.create_file(path, message, code, branch=branch)
    metadata_path = path.removesuffix(".py") + ".json"
    repository.create_file(
        metadata_path,
        message,
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        branch=branch,
    )
    base_url = repository.html_url.rstrip("/")
    return {
        "repository": repository_name,
        "branch": branch,
        "path": path,
        "url": f"{base_url}/blob/{branch}/{path}",
    }
