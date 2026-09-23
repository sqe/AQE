"""Read-only GitHub source-analysis agent with bounded evidence collection."""

from __future__ import annotations

import ast
import os
from typing import Any
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from observability.metrics import PrometheusMiddleware


GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
PUBLIC_BASE_URL = os.getenv("GITHUB_ANALYSIS_PUBLIC_URL", "http://github_analysis_agent:8010").rstrip("/")
EVALUATION_REPOSITORY = os.getenv("GITHUB_SOURCE_EVALUATION_REPOSITORY", "sqe/AQE")
EVALUATION_REF = os.getenv("GITHUB_SOURCE_EVALUATION_REF", os.getenv("GIT_COMMIT_SHA", ""))
MAX_SOURCE_FILES = int(os.getenv("GITHUB_SOURCE_MAX_FILES", "30"))
MAX_SOURCE_BYTES = int(os.getenv("GITHUB_SOURCE_MAX_BYTES", "500000"))
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java"}


def _allowed_repository(repository: str) -> bool:
    allowed = {
        item.strip().lower()
        for item in os.getenv("GITHUB_SOURCE_ALLOWED_REPOSITORIES", "").split(",")
        if item.strip()
    }
    return repository.lower() in allowed


def inspect_python_source(path: str, source: str) -> list[dict[str, Any]]:
    """Return high-signal candidates; runtime tests must still reproduce them."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [{"rule": "invalid-python", "path": path, "line": exc.lineno, "message": exc.msg}]
    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append(
                {"rule": "bare-except", "path": path, "line": node.lineno, "message": "Bare except can hide cancellation and product failures."}
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
            findings.append(
                {"rule": "dynamic-code-execution", "path": path, "line": node.lineno, "message": f"Untrusted input reaching {node.func.id} may execute code."}
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"get", "post", "put", "patch", "delete"}:
                owner = node.func.value
                if isinstance(owner, ast.Name) and owner.id == "requests" and not any(
                    keyword.arg == "timeout" for keyword in node.keywords
                ):
                    findings.append(
                        {"rule": "http-without-timeout", "path": path, "line": node.lineno, "message": "Outbound request has no timeout and can stall an agent task."}
                    )
    return findings


async def analyze_repository(request: dict[str, Any]) -> dict[str, Any]:
    repository = str(request.get("repository", ""))
    ref = str(request.get("ref", ""))
    if not repository or repository.count("/") != 1:
        raise HTTPException(status_code=400, detail="repository must be owner/name")
    if not ref:
        raise HTTPException(status_code=400, detail="ref is required; use the agent version commit")
    if not _allowed_repository(repository):
        raise HTTPException(status_code=403, detail="repository is not allowlisted")

    token = os.getenv("GITHUB_SOURCE_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(base_url=GITHUB_API_URL, headers=headers, timeout=30) as client:
        tree_response = await client.get(
            f"/repos/{repository}/git/trees/{quote(ref, safe='')}", params={"recursive": "1"}
        )
        tree_response.raise_for_status()
        tree = tree_response.json()
        requested_paths = request.get("paths") or []
        candidates = [
            item
            for item in tree.get("tree", [])
            if item.get("type") == "blob"
            and any(str(item.get("path", "")).endswith(suffix) for suffix in SOURCE_SUFFIXES)
            and (not requested_paths or item.get("path") in requested_paths)
        ][:MAX_SOURCE_FILES]
        files: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        total_bytes = 0
        for item in candidates:
            if total_bytes + int(item.get("size", 0)) > MAX_SOURCE_BYTES:
                break
            response = await client.get(
                f"/repos/{repository}/contents/{quote(item['path'], safe='/')}",
                params={"ref": ref},
                headers={**headers, "Accept": "application/vnd.github.raw+json"},
            )
            response.raise_for_status()
            source = response.text
            total_bytes += len(source.encode())
            file_record = {"path": item["path"], "sha": item.get("sha"), "bytes": len(source.encode())}
            files.append(file_record)
            if item["path"].endswith(".py"):
                findings.extend(inspect_python_source(item["path"], source))

    return {
        "repository": repository,
        "ref": ref,
        "tree_sha": tree.get("sha"),
        "files": files,
        "findings": findings,
        "limits": {"max_files": MAX_SOURCE_FILES, "max_bytes": MAX_SOURCE_BYTES},
        "classification": "candidate_source_findings",
    }


app = FastAPI(title="AQE GitHub Source Analysis Agent", version="1.0.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "agent": "github-source-analysis"}


@app.get("/agent_card")
@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    evaluation_ready = _allowed_repository(EVALUATION_REPOSITORY) and len(EVALUATION_REF) == 40 and all(
        character in "0123456789abcdefABCDEF" for character in EVALUATION_REF
    )
    return {
        "name": "github-source-analysis",
        "version": "1.0.0",
        "description": "Reads allowlisted agent source at a pinned GitHub ref and produces test evidence",
        "status": "UP" if evaluation_ready else "DEGRADED",
        "skills": [{
            "id": "source.inspect",
            "description": "Find source-level defect candidates",
            "examples": [{"repository": EVALUATION_REPOSITORY, "ref": EVALUATION_REF, "paths": ["agents/github_analysis/app.py"]}],
            "invocation": {"protocol": "rest", "method": "POST", "url": f"{PUBLIC_BASE_URL}/v1/analyze"},
        }],
        "evaluation": {"cases": ([{
            "id": "inspect-pinned-aqe-source",
            "skill_id": "source.inspect",
            "prompt": {"repository": EVALUATION_REPOSITORY, "ref": EVALUATION_REF, "paths": ["agents/github_analysis/app.py"]},
            "expected_response": {"repository": EVALUATION_REPOSITORY, "ref": EVALUATION_REF, "classification": "candidate_source_findings", "files": "non-empty array", "findings": "array", "limits": {"max_files": MAX_SOURCE_FILES, "max_bytes": MAX_SOURCE_BYTES}},
            "max_latency_ms": 60000,
            "min_accuracy": 1.0,
        }] if evaluation_ready else [])},
        "recommendation": None if evaluation_ready else "Allowlist sqe/AQE and configure GITHUB_SOURCE_EVALUATION_REF with its immutable 40-character commit SHA.",
    }


@app.post("/v1/analyze")
async def analyze(request: dict[str, Any]) -> dict[str, Any]:
    try:
        return await analyze_repository(request)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API returned {exc.response.status_code}") from exc


app = PrometheusMiddleware(app, "github-analysis")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("AGENT_PORT", "8010")))
