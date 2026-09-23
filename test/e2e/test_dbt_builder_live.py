"""Opt-in network E2E checks for the deployed dbt builder.

Set DBT_BUILDER_E2E_URL to a routable service or port-forward. The model-backed
mission additionally requires RUN_DBT_MODEL_E2E=true.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest


BASE_URL = os.getenv("DBT_BUILDER_E2E_URL", "").rstrip("/")
RUN_MODEL = os.getenv("RUN_DBT_MODEL_E2E", "").lower() == "true"
REQUEST = json.loads(
    (Path(__file__).resolve().parents[2] / "examples" / "dbt-validation-request.json").read_text()
)
pytestmark = pytest.mark.skipif(not BASE_URL, reason="DBT_BUILDER_E2E_URL is not configured")


def test_dbt_builder_agent_card_exposes_both_executable_skills():
    card = httpx.get(f"{BASE_URL}/agent_card", timeout=10).json()

    assert {skill["id"] for skill in card["skills"] if skill.get("invocation")} == {
        "dbt.blueprint.build",
        "dbt.project.validate",
    }


def test_dbt_project_validation_accepts_complete_business_contract():
    response = httpx.post(f"{BASE_URL}/v1/projects/validate", json=REQUEST, timeout=10)

    assert response.json() == {"status": "VALID", "validation_errors": []}


def test_dbt_project_validation_rejects_missing_mart_layer():
    request = json.loads(json.dumps(REQUEST))
    request["files"].pop("models/marts/fct_orders.sql")

    response = httpx.post(f"{BASE_URL}/v1/projects/validate", json=request, timeout=10)

    assert response.json()["status"] == "INVALID"


def test_dbt_project_validation_meets_latency_budget():
    started = time.perf_counter()
    httpx.post(f"{BASE_URL}/v1/projects/validate", json=REQUEST, timeout=10).raise_for_status()

    assert (time.perf_counter() - started) * 1000 < 1000


def test_dbt_builder_exports_business_and_http_metrics():
    metrics = httpx.get(f"{BASE_URL}/metrics", timeout=10).text

    assert all(name in metrics for name in ("aqe_dbt_missions_total", "aqe_http_request_duration_seconds"))


@pytest.mark.skipif(not RUN_MODEL, reason="RUN_DBT_MODEL_E2E is not true")
def test_dbt_blueprint_build_is_review_only():
    response = httpx.post(
        f"{BASE_URL}/v1/missions",
        json={
            "goals": "Create documented order metrics",
            "sources": [{"name": "raw.orders", "columns": ["id", "status"]}],
            "dialect": "snowflake",
        },
        timeout=620,
    )

    assert response.json().get("deployment_allowed") is False
