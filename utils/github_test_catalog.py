"""Optional versioned GitHub catalog for generated Python E2E tests."""

from __future__ import annotations

import ast
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
    suite_id: str = "capability-validation",
    source_revision: str | None = None,
    test_layer: str = "integration",
) -> str:
    root = "generated-findings" if outcome == "confirmed_product_defect" else "generated-tests"
    segments = [
        root,
        _segment(test_type, "agent"),
        _segment(test_layer, "integration"),
        _segment(agent_id, "unknown-agent"),
        _segment(agent_version, "unversioned"),
    ]
    if source_revision:
        segments.append(_segment(source_revision, "unversioned-source"))
    semantic_agent = _segment(agent_id, "unknown-agent").replace("-", "_")
    semantic_suite = _segment(suite_id, "capability-validation").replace("-", "_")
    semantic_version = _segment(agent_version, "unversioned").replace("-", "_").replace(".", "_")
    trace = _segment(task_id, "generated")[:8].strip("-_.")
    segments.append(f"test_{semantic_agent}__{semantic_suite}__{semantic_version}__{trace}.py")
    return "/".join(segments)


def suite_id_from_code(code: str) -> str:
    """Read the quality-gated semantic suite identifier without executing code."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "capability-validation"
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "AQE_SUITE_ID" for target in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                break
            if isinstance(value, str):
                return value
    return "capability-validation"


def layer_from_code(code: str) -> str:
    """Read the quality-gated test layer without executing code."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "integration"
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "AQE_TEST_LAYER" for target in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                break
            if isinstance(value, str):
                return value
    return "integration"


def publish_test(
    *,
    agent_id: str,
    agent_version: str,
    task_id: str,
    code: str,
    metadata: dict[str, Any],
    test_type: str = "agent",
    outcome: str = "validated",
    source_revision: str | None = None,
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

    suite_id = suite_id_from_code(code)
    test_layer = layer_from_code(code)
    path = catalog_path(
        agent_id,
        agent_version,
        task_id,
        test_type,
        outcome,
        suite_id,
        source_revision,
        test_layer,
    )
    source_label = f"#{source_revision}" if source_revision else ""
    message = f"test({agent_id}@{agent_version}{source_label}): catalog {outcome} {suite_id}"
    repository.create_file(path, message, code, branch=branch)
    metadata_path = path.removesuffix(".py") + ".json"
    metadata_result = repository.create_file(
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
        "layer": test_layer,
        "url": f"{base_url}/blob/{branch}/{path}",
        "commit_url": metadata_result["commit"].html_url,
    }
