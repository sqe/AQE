"""Generate, validate, and package review-only dbt analytics projects."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import uuid
import zipfile
from pathlib import PurePosixPath
from typing import Any

import httpx
import sqlglot
import yaml
from fastapi import FastAPI, HTTPException
from observability.metrics import PrometheusMiddleware
from prometheus_client import Counter, Histogram
from utils.object_store import ObjectStore


GENERATION_ENDPOINT = os.getenv("LLM_GENERATION_ENDPOINT", "")
GENERATION_MODEL = os.getenv("LLM_GENERATION_MODEL", "")
PUBLIC_BASE_URL = os.getenv("DBT_BUILDER_PUBLIC_URL", "http://dbt_builder_agent:8016").rstrip("/")
MISSIONS = Counter("aqe_dbt_missions_total", "dbt blueprint missions", ("status",))
MISSION_DURATION = Histogram("aqe_dbt_mission_duration_seconds", "dbt blueprint duration")
REQUIRED_PATHS = {"dbt_project.yml", "models/sources.yml", "models/schema.yml", "README.md"}


def _static_sql(sql: str) -> str:
    sql = re.sub(r"\{\{\s*ref\(['\"]([^'\"]+)['\"]\)\s*\}\}", r"\1", sql)
    return re.sub(
        r"\{\{\s*source\(['\"]([^'\"]+)['\"],\s*['\"]([^'\"]+)['\"]\)\s*\}\}",
        r"\1.\2",
        sql,
    )


def validate_project(files: dict[str, str], dialect: str) -> list[str]:
    errors: list[str] = []
    missing = REQUIRED_PATHS - set(files)
    if missing:
        errors.append(f"missing required files: {', '.join(sorted(missing))}")
    for path in files:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            errors.append(f"unsafe path: {path}")
    for path in ("dbt_project.yml", "models/sources.yml", "models/schema.yml"):
        if path not in files:
            continue
        try:
            payload = yaml.safe_load(files[path])
            if not isinstance(payload, dict):
                errors.append(f"{path} must contain a YAML object")
        except yaml.YAMLError as exc:
            errors.append(f"{path} is invalid YAML: {exc}")
    sql_paths = [path for path in files if path.endswith(".sql")]
    if not sql_paths:
        errors.append("project must contain dbt SQL models")
    for path in sql_paths:
        try:
            statements = sqlglot.parse(_static_sql(files[path]), read=dialect)
            if len(statements) != 1:
                errors.append(f"{path} must contain exactly one model query")
            elif not isinstance(statements[0], sqlglot.exp.Query):
                errors.append(f"{path} must be a read-only query")
        except sqlglot.errors.ParseError as exc:
            errors.append(f"{path} is invalid {dialect} SQL: {exc}")
    schema = files.get("models/schema.yml", "")
    if "unique" not in schema or "not_null" not in schema:
        errors.append("models/schema.yml must include unique and not_null data tests")
    for layer in ("staging", "intermediate", "marts"):
        if not any(path.startswith(f"models/{layer}/") for path in files):
            errors.append(f"missing {layer} model layer")
    return errors


def _files_from_response(content: str) -> dict[str, str]:
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[1].rsplit("```", 1)[0]
    files = json.loads(candidate).get("files")
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise ValueError("response must contain a string-to-string files object")
    return files


async def run_blueprint(request: dict[str, Any]) -> dict[str, Any]:
    goals = request.get("goals")
    sources = request.get("sources")
    if not isinstance(goals, str) or not goals.strip():
        raise HTTPException(status_code=400, detail="goals is required")
    if not isinstance(sources, list) or not sources:
        raise HTTPException(status_code=400, detail="sources must be a non-empty table inventory")
    if not GENERATION_ENDPOINT:
        raise HTTPException(status_code=503, detail="LLM_GENERATION_ENDPOINT is not configured")
    dialect = str(request.get("dialect", "snowflake"))
    prompt = f"""/no_think
Act as a dbt analytics engineer. Build a review-only dbt project from the supplied source inventory.
Return only JSON with a `files` object mapping paths to complete file contents. Include dbt_project.yml,
models/sources.yml, models/schema.yml, README.md, and SQL models under models/staging,
models/intermediate, and models/marts. Use ref() and source(), document every model and important
column, and add independently diagnosable unique, not_null, relationships, and accepted_values tests
where justified by evidence. Never invent source columns. Do not connect to or deploy into a warehouse.

Warehouse dialect: {dialect}
Project goals: {goals}
Source inventory: {json.dumps(sources, default=str)}
Business definitions: {json.dumps(request.get('business_definitions') or [], default=str)}
"""
    with MISSION_DURATION.time():
        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(
                GENERATION_ENDPOINT,
                json={
                    "model": GENERATION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 12288,
                    "reasoning_effort": "none",
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    try:
        files = _files_from_response(content)
    except (json.JSONDecodeError, ValueError) as exc:
        MISSIONS.labels("rejected").inc()
        raise HTTPException(status_code=422, detail=f"generated project contract failed: {exc}") from exc
    errors = validate_project(files, dialect)
    if errors:
        MISSIONS.labels("rejected").inc()
        return {"status": "REJECTED", "validation_errors": errors}
    mission_id = str(uuid.uuid4())
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
        archive.writestr("REVIEW_REQUIRED", "Warehouse deployment requires explicit human approval.\n")
    object_path = f"dbt-missions/{mission_id}/dbt-project.zip"
    store = ObjectStore()
    await asyncio.to_thread(store.ensure_bucket)
    await asyncio.to_thread(store.write_bytes, object_path, archive_buffer.getvalue(), "application/zip")
    MISSIONS.labels("review_required").inc()
    return {
        "status": "REVIEW_REQUIRED",
        "mission_id": mission_id,
        "object_path": object_path,
        "deployment_allowed": False,
        "phases": [
            "source-inventory", "staging", "intermediate", "marts",
            "documentation", "data-tests", "static-validation", "package",
        ],
        "files": sorted(files),
        "validation_errors": [],
    }


app = FastAPI(title="AQE dbt Blueprint Agent", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy" if GENERATION_ENDPOINT else "degraded"}


@app.get("/agent_card")
@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    sample_project = {
        "dbt_project.yml": "name: aqe_analytics\nversion: 1.0.0\nprofile: aqe_generated\n",
        "models/sources.yml": "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: orders\n",
        "models/schema.yml": "version: 2\nmodels:\n  - name: fct_orders\n    columns:\n      - name: id\n        tests: [unique, not_null]\n",
        "models/staging/stg_orders.sql": "select id from {{ source('raw', 'orders') }}",
        "models/intermediate/int_orders.sql": "select * from {{ ref('stg_orders') }}",
        "models/marts/fct_orders.sql": "select * from {{ ref('int_orders') }}",
        "README.md": "# AQE analytics\n",
    }
    return {
        "name": "aqe-dbt-builder",
        "version": "0.1.0",
        "status": "UP" if GENERATION_ENDPOINT else "DEGRADED",
        "description": "Review-only dbt medallion analytics project blueprint",
        "skills": [
            {
                "id": "dbt.blueprint.build",
                "description": "Build staging, intermediate, and mart models",
                "examples": ["Build an orders analytics project without deploying it"],
                "invocation": {"protocol": "rest", "method": "POST", "url": f"{PUBLIC_BASE_URL}/v1/missions"},
            },
            {
                "id": "dbt.project.validate",
                "description": "Validate SQL, YAML, docs, and data-test coverage",
                "examples": ["Validate this review-only dbt project"],
                "invocation": {"protocol": "rest", "method": "POST", "url": f"{PUBLIC_BASE_URL}/v1/projects/validate"},
            },
        ],
        "evaluation": {
            "cases": [
                {
                    "id": "build-orders-project",
                    "skill_id": "dbt.blueprint.build",
                    "prompt": {
                        "goals": "Create documented order metrics",
                        "sources": [{"name": "raw.orders", "columns": ["id", "status"]}],
                        "dialect": "snowflake",
                    },
                    "expected_response": {"status": "REVIEW_REQUIRED", "deployment_allowed": False},
                    "max_latency_ms": 600000,
                    "min_accuracy": 1.0,
                },
                {
                    "id": "validate-orders-project",
                    "skill_id": "dbt.project.validate",
                    "prompt": {"files": sample_project, "dialect": "snowflake"},
                    "expected_response": {"status": "VALID", "validation_errors": []},
                    "max_latency_ms": 1000,
                    "min_accuracy": 1.0,
                },
            ]
        },
        "recommendation": None if GENERATION_ENDPOINT else "Configure the generation model endpoint.",
    }


@app.post("/v1/missions")
async def mission(request: dict[str, Any]) -> dict[str, Any]:
    try:
        return await run_blueprint(request)
    except httpx.HTTPError as exc:
        MISSIONS.labels("failed").inc()
        raise HTTPException(status_code=502, detail=f"model request failed: {exc}") from exc


@app.post("/v1/projects/validate")
async def project_validation(request: dict[str, Any]) -> dict[str, Any]:
    files = request.get("files")
    if not isinstance(files, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items()):
        raise HTTPException(status_code=400, detail="files must be a string-to-string object")
    errors = validate_project(files, str(request.get("dialect", "snowflake")))
    return {"status": "VALID" if not errors else "INVALID", "validation_errors": errors}


app = PrometheusMiddleware(app, "dbt-builder")
