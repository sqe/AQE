#!/usr/bin/env python3
"""Select only container images affected by paths supplied on stdin."""

import json
import sys


IMAGES = [
    ("agent-builder", "agents/agent_builder/Dockerfile", ("agents/agent_builder/",)),
    ("artifact-management", "agents/artifact_management/Dockerfile", ("agents/artifact_management/",)),
    ("change-detection", "agents/change_detection/Dockerfile", ("agents/change_detection/",)),
    ("dbt-builder", "agents/dbt_builder/Dockerfile", ("agents/dbt_builder/",)),
    ("diagnostics", "agents/diagnostics/Dockerfile", ("agents/diagnostics/", "diagnostics/")),
    ("github-analysis", "agents/github_analysis/Dockerfile", ("agents/github_analysis/",)),
    ("github-connector", "agents/github_connector/Dockerfile", ("agents/github_connector/",)),
    ("github-commit", "agents/github_commit/Dockerfile", ("agents/github_commit/",)),
    ("knowledge-ingestion", "agents/knowledge_ingestion/Dockerfile", ("agents/knowledge_ingestion/", "ontology/")),
    ("test-generation", "agents/test_generation/Dockerfile", ("agents/test_generation/", "ontology/")),
    ("test-execution", "agents/test_execution/Dockerfile", ("agents/test_execution/", "test_execution_agent/")),
    ("webpage-state-capture", "agents/webpage_state_capture/Dockerfile", ("agents/webpage_state_capture/",)),
    ("website-execution", "agents/website_execution/Dockerfile", ("agents/website_execution/",)),
    ("temporal", "Dockerfile.temporal", ("Dockerfile.temporal", "temporal_worker/")),
    ("byoa", "Dockerfile.byoa", ("Dockerfile.byoa", "byoa_adapter/")),
    ("reporting", "Dockerfile.reporting", ("Dockerfile.reporting", "service/")),
    ("frontend", "Dockerfile.frontend", ("Dockerfile.frontend", "frontend/")),
]

SHARED_IMAGE_PATHS = (
    ".github/workflows/ci.yml",
    "observability/",
    "requirements.in",
    "requirements.txt",
    "utils/",
)
E2E_PATHS = (
    "agents/",
    "byoa_adapter/",
    "docker-compose.yml",
    "Dockerfile.",
    "observability/",
    "service/",
    "temporal_worker/",
    "test/e2e/",
    "test_execution_agent/",
    "utils/",
)
DOCS_PATHS = (
    ".github/ISSUE_TEMPLATE/",
    ".github/pull_request_template.md",
    "docs/",
    "README.md",
)
CONFIG_PATHS = (
    "deploy/",
    "docker-compose.yml",
)


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(path == pattern or path.startswith(pattern) for pattern in patterns)


paths = {line.strip() for line in sys.stdin if line.strip()}
build_all = any(matches(path, SHARED_IMAGE_PATHS) for path in paths)
if paths and all(matches(path, DOCS_PATHS) for path in paths):
    lane = "docs"
elif paths and all(matches(path, DOCS_PATHS + CONFIG_PATHS) for path in paths):
    lane = "config"
else:
    lane = "code"
include = [
    {"name": name, "dockerfile": dockerfile}
    for name, dockerfile, patterns in IMAGES
    if build_all or any(matches(path, patterns) for path in paths)
]
print(json.dumps({
    "image_matrix": {"include": include},
    "image_count": len(include),
    "lane": lane,
    "run_e2e": any(matches(path, E2E_PATHS) for path in paths),
}, separators=(",", ":")))
