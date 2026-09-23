import asyncio

import httpx

from agents.dbt_builder import app as dbt
from agents.dbt_builder.app import validate_project


def project(sql: str = "select id, status from {{ source('raw', 'orders') }}") -> dict[str, str]:
    return {
        "dbt_project.yml": "name: aqe_analytics\nversion: 1.0.0\nprofile: aqe_generated\n",
        "models/sources.yml": "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: orders\n",
        "models/schema.yml": "version: 2\nmodels:\n  - name: fct_orders\n    columns:\n      - name: id\n        tests: [unique, not_null]\n",
        "models/staging/stg_orders.sql": sql,
        "models/intermediate/int_orders.sql": "select * from {{ ref('stg_orders') }}",
        "models/marts/fct_orders.sql": "select * from {{ ref('int_orders') }}",
        "README.md": "# AQE analytics\n",
    }


def test_valid_medallion_project_passes_static_validation():
    errors = validate_project(project(), "snowflake")

    assert errors == []


def test_write_statement_is_rejected():
    errors = validate_project(project("delete from raw.orders"), "snowflake")

    assert any("read-only" in error for error in errors)


def test_missing_data_tests_are_rejected():
    files = project()
    files["models/schema.yml"] = "version: 2\nmodels: []\n"

    assert any("unique and not_null" in error for error in validate_project(files, "snowflake"))


def request(method: str, path: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dbt.app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def test_agent_card_publishes_executable_contract_for_every_dbt_skill():
    card = request("GET", "/agent_card").json()

    assert all(skill.get("invocation", {}).get("url") for skill in card["skills"])


def test_validation_endpoint_accepts_business_complete_project():
    response = request("POST", "/v1/projects/validate", json={"files": project(), "dialect": "snowflake"})

    assert response.json() == {"status": "VALID", "validation_errors": []}


def test_validation_endpoint_reports_business_rule_failure():
    files = project()
    files.pop("models/marts/fct_orders.sql")

    response = request("POST", "/v1/projects/validate", json={"files": files, "dialect": "snowflake"})

    assert response.json()["status"] == "INVALID"


def test_validation_endpoint_rejects_malformed_contract():
    response = request("POST", "/v1/projects/validate", json={"files": []})

    assert response.status_code == 400


def test_dbt_metrics_expose_agent_latency_and_outcomes():
    response = request("GET", "/metrics")

    assert {"aqe_http_request_duration_seconds", "aqe_dbt_missions_total"} <= set(response.text.split())
