import os
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError


GENERATION_URL = os.getenv(
    "TEST_GENERATION_URL", "http://test_generation_agent:8001/generate_test_plan"
)
AGENT_EXECUTION_URL = os.getenv("AGENT_TEST_EXECUTION_URL", "http://test_execution_agent:8003/run_tests")
WEBSITE_EXECUTION_URL = os.getenv(
    "WEBSITE_TEST_EXECUTION_URL", "http://website_test_execution_agent:8013/run_tests"
)
SOURCE_ANALYSIS_URL = os.getenv(
    "SOURCE_ANALYSIS_URL", "http://github_analysis_agent:8010/v1/analyze"
)
AGENT_DISCOVERY_URL = os.getenv(
    "AGENT_DISCOVERY_URL", "http://diagnostics_agent:8006/v1/agent-discovery"
)
QUALITY_ORACLE_URL = os.getenv(
    "QUALITY_ORACLE_URL", "http://quality_oracle_agent:8017/v1/reviews"
)
GRAPH_EVENTS_URL = os.getenv("GRAPH_EVENTS_URL") or "http://aqe-diagnostics:8006/v1/graph/events"


async def _post(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    allow_error_response: bool = False,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)
        if response.status_code >= 400 and (not allow_error_response or response.status_code >= 500):
            try:
                detail = response.json().get("detail") or response.text
            except ValueError:
                detail = response.text
            raise ApplicationError(
                f"{url} returned HTTP {response.status_code}: {detail}",
                non_retryable=400 <= response.status_code < 500,
            )
        try:
            return response.json()
        except ValueError as exc:
            body = response.text[:1000] or "<empty body>"
            raise ApplicationError(
                f"{url} returned non-JSON HTTP {response.status_code} "
                f"({response.headers.get('content-type', 'unknown content type')}): {body}",
                non_retryable=response.status_code < 500,
            ) from exc


@activity.defn(name="generate_tests")
async def generate_tests(request: dict[str, Any]) -> dict[str, Any]:
    return await _post(GENERATION_URL, request, 600)


@activity.defn(name="review_generated_tests")
async def review_generated_tests(request: dict[str, Any]) -> dict[str, Any]:
    return await _post(QUALITY_ORACLE_URL, request, 600)


@activity.defn(name="analyze_source")
async def analyze_source(request: dict[str, Any]) -> dict[str, Any]:
    return await _post(SOURCE_ANALYSIS_URL, request, 180)


@activity.defn(name="discover_agent")
async def discover_agent(request: dict[str, Any]) -> dict[str, Any]:
    return await _post(AGENT_DISCOVERY_URL, request, 60)


@activity.defn(name="execute_and_repair")
async def execute_and_repair(request: dict[str, Any]) -> dict[str, Any]:
    activity.heartbeat("dispatching sandbox execution")
    test_type = request.get("test_type", "agent")
    url = WEBSITE_EXECUTION_URL if test_type == "website" else AGENT_EXECUTION_URL
    return await _post(url, request, 900, allow_error_response=True)


@activity.defn(name="record_workflow_stage")
async def record_workflow_stage(event: dict[str, Any]) -> dict[str, Any]:
    """Publish best-effort live telemetry without making observability a workflow dependency."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(GRAPH_EVENTS_URL, json=event)
        return {"delivered": response.is_success}
    except httpx.HTTPError:
        return {"delivered": False}
