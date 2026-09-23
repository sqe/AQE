"""Generate reviewable agent source bundles without deploying generated code."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import uuid
import zipfile
from pathlib import PurePosixPath
from typing import Any

import httpx
import yaml
from agents.test_execution.test_quality import inspect_test_code
from fastapi import FastAPI, HTTPException
from observability.metrics import PrometheusMiddleware
from prometheus_client import Counter, Histogram
from utils.object_store import ObjectStore


GENERATION_ENDPOINT = os.getenv("LLM_GENERATION_ENDPOINT", "")
GENERATION_MODEL = os.getenv("LLM_GENERATION_MODEL", "")
SOURCE_ANALYSIS_URL = os.getenv("SOURCE_ANALYSIS_URL", "http://aqe-github-analysis:8010/v1/analyze")
PUBLIC_BASE_URL = os.getenv("AGENT_BUILDER_PUBLIC_URL", "http://agent_builder_agent:8015").rstrip("/")
REQUIRED_FILES = {
    "agent/app.py",
    "agent/requirements.txt",
    "agent/Dockerfile",
    "agent/agent.yaml",
    "agent/tests/test_agent.py",
}
BUILDS = Counter("aqe_agent_builder_builds_total", "Experimental agent builds", ("status",))
BUILD_DURATION = Histogram("aqe_agent_builder_duration_seconds", "Agent bundle generation time")


def validate_bundle(files: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if set(files) != REQUIRED_FILES:
        errors.append(f"bundle must contain exactly: {', '.join(sorted(REQUIRED_FILES))}")
        return errors
    for path in files:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            errors.append(f"unsafe path: {path}")
    for path in ("agent/app.py", "agent/tests/test_agent.py"):
        try:
            ast.parse(files[path])
        except SyntaxError as exc:
            errors.append(f"{path} is invalid Python: {exc.msg}")
    errors.extend(
        f"agent/tests/test_agent.py: {issue.rule}: {issue.message}"
        for issue in inspect_test_code(files["agent/tests/test_agent.py"])
    )
    dockerfile = files["agent/Dockerfile"]
    if "USER " not in dockerfile:
        errors.append("Dockerfile must set a non-root USER")
    requirements = [line.strip() for line in files["agent/requirements.txt"].splitlines() if line.strip()]
    if any("==" not in line for line in requirements):
        errors.append("all Python dependencies must be exactly pinned")
    try:
        card = yaml.safe_load(files["agent/agent.yaml"])
        if not isinstance(card, dict) or not card.get("name") or not card.get("skills"):
            errors.append("agent.yaml must declare name and skills")
    except yaml.YAMLError as exc:
        errors.append(f"agent.yaml is invalid: {exc}")
    return errors


def _parse_bundle(text: str) -> dict[str, str]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[1].rsplit("```", 1)[0]
    payload = json.loads(candidate)
    files = payload.get("files")
    if not isinstance(files, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items()):
        raise ValueError("model response must contain a string-to-string files object")
    return files


async def _source_evidence(request: dict[str, Any]) -> dict[str, Any]:
    repository = request.get("source_repository")
    if not repository:
        return request.get("source_analysis") or {}
    if not request.get("source_ref"):
        raise HTTPException(status_code=400, detail="source_ref is required with source_repository")
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            SOURCE_ANALYSIS_URL,
            json={"repository": repository, "ref": request["source_ref"], "paths": request.get("source_paths", [])},
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"source analysis failed: {response.text}")
        return response.json()


async def generate_bundle(request: dict[str, Any]) -> dict[str, Any]:
    spec = request.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        raise HTTPException(status_code=400, detail="spec is required")
    if not GENERATION_ENDPOINT:
        raise HTTPException(status_code=503, detail="LLM_GENERATION_ENDPOINT is not configured")
    source = await _source_evidence(request)
    prompt = f"""/no_think
You are an expert agent engineer. Create an EXPERIMENTAL Python HTTP/A2A agent from the supplied
requirements. Return one JSON object with a `files` object and exactly these paths:
{json.dumps(sorted(REQUIRED_FILES))}

The app must expose /health and /.well-known/agent.json, have explicit timeouts, structured errors,
Prometheus metrics, no embedded credentials, and domain behavior grounded only in the requirements.
Tests must be executable pytest semantic end-to-end tests, use realistic prompts and expected domain
responses, and have exactly one observable assertion per test. Dependencies must use exact versions.
The Dockerfile must be minimal and non-root. Do not emit Kubernetes resources and do not deploy.

CASE STUDY / SPECIFICATION:
{spec}

REFINED REQUIREMENTS:
{json.dumps(request.get('refined_requirements') or [], default=str)}

PINNED REPOSITORY EVIDENCE:
{json.dumps(source, default=str)}
"""
    payload = {
        "model": GENERATION_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 8192,
        "reasoning_effort": "none",
    }
    with BUILD_DURATION.time():
        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(GENERATION_ENDPOINT, json=payload)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
    try:
        files = _parse_bundle(text)
    except (json.JSONDecodeError, ValueError) as exc:
        BUILDS.labels("rejected").inc()
        raise HTTPException(status_code=422, detail=f"generated bundle contract failed: {exc}") from exc
    errors = validate_bundle(files)
    if errors:
        BUILDS.labels("rejected").inc()
        return {"status": "REJECTED", "quality_errors": errors}
    build_id = str(uuid.uuid4())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
        archive.writestr("REVIEW_REQUIRED", "Generated code is experimental and must not be auto-deployed.\n")
    object_path = f"experimental-agent-builds/{build_id}/agent-bundle.zip"
    store = ObjectStore()
    await asyncio.to_thread(store.ensure_bucket)
    await asyncio.to_thread(store.write_bytes, object_path, buffer.getvalue(), "application/zip")
    BUILDS.labels("review_required").inc()
    return {
        "status": "REVIEW_REQUIRED",
        "build_id": build_id,
        "object_path": object_path,
        "files": sorted(files),
        "quality_errors": [],
        "deployment_allowed": False,
    }


app = FastAPI(title="AQE Experimental Agent Builder", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy" if GENERATION_ENDPOINT else "degraded"}


@app.get("/agent_card")
@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    return {
        "name": "aqe-experimental-agent-builder",
        "version": "0.1.0",
        "status": "UP" if GENERATION_ENDPOINT else "DEGRADED",
        "description": "Builds review-only agent source bundles from specifications and pinned evidence",
        "skills": [
            {
                "id": "agent.build.experimental",
                "description": "Generate and validate a review-only agent bundle",
                "examples": [{"spec": "Create a review-only HTTP agent that returns a fixed greeting."}],
                "invocation": {"protocol": "rest", "method": "POST", "url": f"{PUBLIC_BASE_URL}/v1/builds"},
            }
        ],
        "evaluation": {
            "cases": [
                {
                    "id": "build-minimal-review-only-agent",
                    "skill_id": "agent.build.experimental",
                    "prompt": {"spec": "Create a minimal review-only HTTP agent with one fixed greeting endpoint. Do not deploy it."},
                    "expected_response": {
                        "status": "REVIEW_REQUIRED",
                        "deployment_allowed": False,
                        "quality_errors": [],
                        "files": sorted(REQUIRED_FILES),
                    },
                    "max_latency_ms": 600000,
                    "min_accuracy": 1.0,
                }
            ]
        },
        "recommendation": None if GENERATION_ENDPOINT else "Configure the generation model endpoint.",
    }


@app.post("/v1/builds")
async def build(request: dict[str, Any]) -> dict[str, Any]:
    try:
        return await generate_bundle(request)
    except httpx.HTTPError as exc:
        BUILDS.labels("failed").inc()
        raise HTTPException(status_code=502, detail=f"model request failed: {exc}") from exc


app = PrometheusMiddleware(app, "experimental-agent-builder")
