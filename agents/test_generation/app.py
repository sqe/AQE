"""Ontology- and RAG-grounded test-generation agent entrypoint."""

import uvicorn
import logging
import os
import asyncio
import asyncpg
import hashlib
import json
import datetime
import uuid
from typing import Dict, Any, Tuple, Optional, List
from urllib.parse import urlparse, urlunparse
from qdrant_client import QdrantClient, models
import httpx 
from agents.test_execution.test_quality import inspect_test_code
from utils.agent_ontology import load_ontology, select_ontology_context
from utils.object_store import ObjectStore
from utils.test_types import resolve_test_type

# Starlette/CORS Imports
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.requests import Request

# Core A2A Framework Imports (Must be available in Docker environment)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from a2a.utils import new_agent_text_message
from observability.metrics import PrometheusMiddleware

# Configure logging for production tracing
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestGenerationAgent")

# --- Infrastructure Configuration ---
POSTGRES_DB_URL = os.environ.get("POSTGRES_URL", "postgresql://user:pass@postgres:5432/qe_db")

USER_ID = os.environ.get("USER_ID", "default-user") 
GENERATION_POLICY_VERSION = "agent-suite-v5"

# --- Qdrant and RAG Configuration ---
QDRANT_CLIENT = QdrantClient(
    host=os.environ.get("QDRANT_HOST", "qdrant"),
    port=int(os.environ.get("QDRANT_PORT", "6333")),
    api_key=os.environ.get("QDRANT_API_KEY") or None,
    https=os.environ.get("QDRANT_HTTPS", "false").lower() == "true",
)
COLLECTION_NAME = "product_knowledge"

# --- LLM Service Configuration (Dynamic Mode) ---
# Default mode is 'SELF_HOSTED', fallback for Gemini.
LLM_PROVIDER_MODE = os.environ.get("LLM_PROVIDER_MODE", "SELF_HOSTED").upper()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MAX_ATTEMPTS = int(os.environ.get("GEMINI_MAX_ATTEMPTS", "3"))

# Self-Hosted/LM Studio Endpoints (Only used if LLM_PROVIDER_MODE is 'SELF_HOSTED')
LLM_EMBEDDING_ENDPOINT = os.environ.get("LLM_EMBEDDING_ENDPOINT", "")
LLM_GENERATION_ENDPOINT = os.environ.get("LLM_GENERATION_ENDPOINT", "")
LLM_EMBEDDING_MODEL = os.environ.get("LLM_EMBEDDING_MODEL", "")
LLM_GENERATION_MODEL = os.environ.get("LLM_GENERATION_MODEL", "")
# Leave one minute for persistence and graph publication inside Temporal's
# ten-minute generation activity deadline.
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "540"))
LLM_MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "8192"))
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "none")
GRAPH_API_URL = os.environ.get("GRAPH_API_URL", "http://diagnostics_agent:8006/v1/graph/events")
GRAPH_EVENT_TIMEOUT_SECONDS = float(os.environ.get("GRAPH_EVENT_TIMEOUT_SECONDS", "3"))
PUBLIC_BASE_URL = os.environ.get("TEST_GENERATION_PUBLIC_URL", "http://test_generation_agent:8001").rstrip("/")
EMBEDDING_DIMENSION = 384 

# Gemini API Constants
GEMINI_EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001").removeprefix("models/")
GEMINI_GENERATION_MODEL = os.environ.get("GEMINI_GENERATION_MODEL", "gemini-3.6-flash")
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


async def llm_provider_status(
    *,
    mode: str,
    api_key: str,
    generation_endpoint: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Probe the configured provider without generating content or exposing credentials."""
    normalized_mode = mode.upper()
    if normalized_mode == "GEMINI":
        configured = bool(api_key)
        model = GEMINI_GENERATION_MODEL
        url = f"{GEMINI_API_BASE_URL}/models/{model}"
        headers = {"x-goog-api-key": api_key}
    else:
        normalized_mode = "SELF_HOSTED"
        configured = bool(generation_endpoint)
        model = LLM_GENERATION_MODEL or "provider-default"
        parsed = urlparse(generation_endpoint)
        path_prefix = parsed.path.partition("/v1/")[0]
        url = urlunparse(parsed._replace(path=f"{path_prefix}/v1/models", query="", fragment=""))
        headers = None
    if not configured:
        return {"mode": normalized_mode, "configured": False, "status": "not_configured", "model": model}

    started = asyncio.get_running_loop().time()
    owns_client = client is None
    probe_client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
    try:
        response = await probe_client.get(url, headers=headers)
        response.raise_for_status()
        return {
            "mode": normalized_mode,
            "configured": True,
            "status": "connected",
            "model": model,
            "latency_ms": round((asyncio.get_running_loop().time() - started) * 1000, 2),
        }
    except httpx.HTTPError as exc:
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        return {
            "mode": normalized_mode,
            "configured": True,
            "status": "unreachable",
            "model": model,
            **({"http_status": status_code} if status_code is not None else {}),
        }
    finally:
        if owns_client:
            await probe_client.aclose()


def resolve_target_identity(captured_state: Dict[str, Any]) -> str:
    target = captured_state.get("target_agent") or {}
    explicit = target.get("id") or captured_state.get("agent_name")
    if explicit:
        return str(explicit)
    endpoint = captured_state.get("agent_card_url") or captured_state.get("url")
    hostname = urlparse(str(endpoint or "")).hostname
    if hostname:
        return hostname
    raise ValueError("A stable target agent identity or URL is required; refusing a generic fallback")


def generation_fingerprint(captured_state: Dict[str, Any]) -> str:
    """Identify every stable input whose change can require a different generated suite."""
    source_analysis = captured_state.get("source_analysis") or {}
    target = captured_state.get("target_agent") or {}
    identity = {
        "policy_version": GENERATION_POLICY_VERSION,
        "test_type": resolve_test_type(captured_state),
        "target": {
            "id": target.get("id") or captured_state.get("agent_name"),
            "version": target.get("version") or captured_state.get("agent_version"),
            "card_url": target.get("card_url") or captured_state.get("agent_card_url"),
            "skills": sorted(captured_state.get("skills") or target.get("skills") or []),
        },
        "source": {
            "repository": captured_state.get("source_repository"),
            "revision": captured_state.get("source_ref"),
            "tree_sha": source_analysis.get("tree_sha"),
            "paths": sorted(captured_state.get("source_paths") or []),
        },
        "requirements": {
            "document_id": captured_state.get("knowledge_document_id"),
            "refined": captured_state.get("refined_requirements") or [],
            "spec": captured_state.get("spec"),
        },
        "contract": {
            "scenarios": captured_state.get("scenarios") or [],
            "archetype": captured_state.get("agent_archetype"),
            "max_latency_ms": captured_state.get("max_latency_ms"),
            "min_accuracy": captured_state.get("min_accuracy"),
        },
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _target_segment(identity: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in identity).strip("-.").lower()


def compile_declared_github_mcp_suite(captured_state: Dict[str, Any]) -> Optional[str]:
    """Compile the connector's complete declared contract without an LLM."""
    scenarios = {
        str(scenario.get("scenario_id")): scenario
        for scenario in captured_state.get("scenarios", [])
        if isinstance(scenario, dict)
    }
    list_scenario = scenarios.get("list-enabled-github-tools")
    call_scenario = scenarios.get("call-allowlisted-list-branches")
    if not list_scenario or not call_scenario:
        return None
    if list_scenario.get("skill_id") != "github.tools.list" or call_scenario.get("skill_id") != "github.tools.call":
        return None
    list_dimensions = set(list_scenario.get("required_dimensions") or [])
    call_dimensions = set(call_scenario.get("required_dimensions") or [])
    if list_dimensions != {"positive", "protocol_schema", "latency", "semantic_accuracy"}:
        return None
    if call_dimensions != {"positive", "protocol_schema", "malformed_input", "latency", "semantic_accuracy"}:
        return None

    list_expected = list_scenario.get("expected_response") or {}
    call_expected = call_scenario.get("expected_response") or {}
    malformed = call_expected.get("malformed_input") or {}
    allowed_tools = list_expected.get("allowed_tool_names")
    branch_schema = call_expected.get("branch_payload") or {}
    required_fields = branch_schema.get("item_required_fields")
    arguments = call_scenario.get("prompt")
    list_url = (list_scenario.get("invocation") or {}).get("url")
    call_url = (call_scenario.get("invocation") or {}).get("url")
    if not (
        isinstance(allowed_tools, list)
        and all(isinstance(tool, str) for tool in allowed_tools)
        and branch_schema.get("type") == "array"
        and branch_schema.get("may_be_empty") is True
        and isinstance(required_fields, list)
        and all(isinstance(field, str) for field in required_fields)
        and isinstance(arguments, dict)
        and isinstance(list_url, str)
        and isinstance(call_url, str)
        and isinstance(malformed.get("request"), dict)
        and malformed.get("isError") is True
        and isinstance(malformed.get("message_contains"), str)
    ):
        return None

    base_url = str(captured_state.get("url") or "").rstrip("/")
    list_path = urlparse(list_url).path
    call_path = urlparse(call_url).path
    list_latency_seconds = float(list_scenario.get("max_latency_ms", 5000)) / 1000
    call_latency_seconds = float(call_scenario.get("max_latency_ms", 10000)) / 1000
    malformed_status = int(malformed.get("status_code", 200))
    return f'''"""Deterministically compiled GitHub MCP connector contract tests."""

import json
import os
import time

import httpx
import pytest


AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "github-mcp-connector-contract"
AQE_SKILL_TESTS = {{
    "github.tools.list": {{
        "positive": "test_github_tools_list_positive",
        "protocol_schema": [
            "test_github_tools_list_protocol_schema_body_object",
            "test_github_tools_list_protocol_schema_tools_array",
            "test_github_tools_list_protocol_schema_tools_non_empty",
            "test_github_tools_list_protocol_schema_tool_objects",
            "test_github_tools_list_protocol_schema_tool_name_strings",
        ],
        "latency": "test_github_tools_list_latency",
        "semantic_accuracy": "test_github_tools_list_semantic_accuracy",
    }},
    "github.tools.call": {{
        "positive": "test_github_tools_call_positive",
        "protocol_schema": [
            "test_github_tools_call_protocol_schema_content_type",
            "test_github_tools_call_protocol_schema_content_array",
            "test_github_tools_call_protocol_schema_content_non_empty",
            "test_github_tools_call_protocol_schema_text_block",
            "test_github_tools_call_protocol_schema_json_decodable",
            "test_github_tools_call_protocol_schema_payload_array",
            "test_github_tools_call_protocol_schema_branch_objects",
            "test_github_tools_call_protocol_schema_branch_name_field",
            "test_github_tools_call_protocol_schema_branch_sha_field",
            "test_github_tools_call_protocol_schema_branch_protected_field",
        ],
        "malformed_input": [
            "test_github_tools_call_malformed_input_status",
            "test_github_tools_call_malformed_input_error_flag",
            "test_github_tools_call_malformed_input_message",
        ],
        "latency": "test_github_tools_call_latency",
        "semantic_accuracy": "test_github_tools_call_semantic_accuracy",
    }},
}}

AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", {base_url!r})
TOOLS_PATH = {list_path!r}
LIST_BRANCHES_PATH = {call_path!r}
ALLOWED_TOOL_NAMES = {sorted(allowed_tools)!r}
LIST_BRANCHES_ARGUMENTS = {arguments!r}
REQUIRED_BRANCH_FIELDS = {required_fields!r}
MALFORMED_ARGUMENTS = {malformed["request"]!r}
MALFORMED_MESSAGE = {malformed["message_contains"]!r}


@pytest.fixture
def client():
    with httpx.Client(base_url=AGENT_BASE_URL, timeout=30) as value:
        yield value


def _decoded_mcp_text(response):
    # The MCP envelope must contain a text block. Its decoded branch array may validly be [].
    body = response.json()
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("MCP response must contain at least one content block")
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text" or not isinstance(first.get("text"), str):
        raise ValueError("MCP response must start with a text content block")
    try:
        return json.loads(first["text"])
    except json.JSONDecodeError as exc:
        raise ValueError("MCP text content is not valid JSON") from exc


def test_github_tools_list_positive(client):
    response = client.get(TOOLS_PATH)
    assert response.status_code == 200


def test_github_tools_list_protocol_schema_body_object(client):
    body = client.get(TOOLS_PATH).json()
    assert isinstance(body, dict)


def test_github_tools_list_protocol_schema_tools_array(client):
    tools = client.get(TOOLS_PATH).json().get("tools")
    assert isinstance(tools, list)


def test_github_tools_list_protocol_schema_tools_non_empty(client):
    tools = client.get(TOOLS_PATH).json().get("tools")
    assert isinstance(tools, list) and bool(tools)


def test_github_tools_list_protocol_schema_tool_objects(client):
    tools = client.get(TOOLS_PATH).json().get("tools")
    assert isinstance(tools, list) and all(isinstance(tool, dict) for tool in tools)


def test_github_tools_list_protocol_schema_tool_name_strings(client):
    tools = client.get(TOOLS_PATH).json().get("tools")
    assert isinstance(tools, list) and all(isinstance(tool.get("name"), str) for tool in tools if isinstance(tool, dict))


def test_github_tools_list_latency(client):
    started = time.monotonic()
    client.get(TOOLS_PATH)
    assert time.monotonic() - started <= {list_latency_seconds!r}


def test_github_tools_list_semantic_accuracy(client):
    tools = client.get(TOOLS_PATH).json()["tools"]
    names = [tool["name"] for tool in tools]
    assert bool(names) and set(names).issubset(set(ALLOWED_TOOL_NAMES))


def test_github_tools_call_positive(client):
    response = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS)
    assert response.status_code == 200


def test_github_tools_call_protocol_schema_content_type(client):
    response = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS)
    assert response.headers.get("content-type", "").startswith("application/json")


def test_github_tools_call_protocol_schema_content_array(client):
    response = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS)
    content = response.json().get("content")
    assert isinstance(content, list)


def test_github_tools_call_protocol_schema_content_non_empty(client):
    content = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS).json().get("content")
    assert isinstance(content, list) and bool(content)


def test_github_tools_call_protocol_schema_text_block(client):
    content = client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS).json().get("content")
    assert isinstance(content, list) and bool(content) and isinstance(content[0], dict) and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)


def test_github_tools_call_protocol_schema_json_decodable(client):
    payload = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert payload is not None


def test_github_tools_call_protocol_schema_payload_array(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list)


def test_github_tools_call_protocol_schema_branch_objects(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list) and all(isinstance(branch, dict) for branch in branches)


def test_github_tools_call_protocol_schema_branch_name_field(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list) and all(isinstance(branch, dict) and "name" in branch for branch in branches)


def test_github_tools_call_protocol_schema_branch_sha_field(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list) and all(isinstance(branch, dict) and "sha" in branch for branch in branches)


def test_github_tools_call_protocol_schema_branch_protected_field(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    assert isinstance(branches, list) and all(isinstance(branch, dict) and "protected" in branch for branch in branches)


def test_github_tools_call_malformed_input_status(client):
    response = client.post(LIST_BRANCHES_PATH, json=MALFORMED_ARGUMENTS)
    assert response.status_code == {malformed_status!r}


def test_github_tools_call_malformed_input_error_flag(client):
    body = client.post(LIST_BRANCHES_PATH, json=MALFORMED_ARGUMENTS).json()
    assert body.get("isError") is True


def test_github_tools_call_malformed_input_message(client):
    response = client.post(LIST_BRANCHES_PATH, json=MALFORMED_ARGUMENTS)
    body = response.json()
    texts = [block.get("text", "") for block in body.get("content", []) if isinstance(block, dict)]
    assert any(MALFORMED_MESSAGE in text for text in texts)


def test_github_tools_call_latency(client):
    started = time.monotonic()
    client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS)
    assert time.monotonic() - started <= {call_latency_seconds!r}


def test_github_tools_call_semantic_accuracy(client):
    branches = _decoded_mcp_text(client.post(LIST_BRANCHES_PATH, json=LIST_BRANCHES_ARGUMENTS))
    # all(...) over [] is intentional because the declared branch payload may be empty.
    assert isinstance(branches, list) and all(isinstance(branch, dict) and all(field in branch for field in REQUIRED_BRANCH_FIELDS) for branch in branches)
'''


def compile_declared_dbt_builder_suite(captured_state: Dict[str, Any]) -> Optional[str]:
    """Compile the complete first-party DBT Builder contract without an LLM."""
    scenarios = {
        str(scenario.get("scenario_id")): scenario
        for scenario in captured_state.get("scenarios", [])
        if isinstance(scenario, dict)
    }
    build = scenarios.get("build-orders-project")
    validate = scenarios.get("validate-orders-project")
    dimensions = {"positive", "protocol_schema", "malformed_input", "latency", "semantic_accuracy"}
    if not build or not validate:
        return None
    if build.get("skill_id") != "dbt.blueprint.build" or validate.get("skill_id") != "dbt.project.validate":
        return None
    if set(build.get("required_dimensions") or []) != dimensions:
        return None
    if set(validate.get("required_dimensions") or []) != dimensions:
        return None

    build_prompt = build.get("prompt")
    validate_prompt = validate.get("prompt")
    build_expected = build.get("expected_response")
    validate_expected = validate.get("expected_response")
    build_url = (build.get("invocation") or {}).get("url")
    validate_url = (validate.get("invocation") or {}).get("url")
    if not (
        isinstance(build_prompt, dict)
        and isinstance(validate_prompt, dict)
        and isinstance(build_expected, dict)
        and isinstance(validate_expected, dict)
        and build_expected.get("status") == "REVIEW_REQUIRED"
        and build_expected.get("deployment_allowed") is False
        and validate_expected.get("status") == "VALID"
        and validate_expected.get("validation_errors") == []
        and isinstance(build_url, str)
        and isinstance(validate_url, str)
    ):
        return None

    base_url = str(captured_state.get("url") or "").rstrip("/")
    build_path = urlparse(build_url).path
    validate_path = urlparse(validate_url).path
    build_latency_seconds = float(build.get("max_latency_ms", 600000)) / 1000
    validate_latency_seconds = float(validate.get("max_latency_ms", 1000)) / 1000
    return f'''"""Deterministically compiled DBT Builder contract tests."""

import os
import time

import httpx
import pytest


AQE_TEST_LAYER = "db"
AQE_SUITE_ID = "dbt-builder-contract"
AQE_SKILL_TESTS = {{
    "dbt.blueprint.build": {{
        "positive": "test_dbt_blueprint_build_positive_status",
        "protocol_schema": "test_dbt_blueprint_build_protocol_schema_body_object",
        "malformed_input": "test_dbt_blueprint_build_malformed_input_status",
        "latency": "test_dbt_blueprint_build_latency_budget",
        "semantic_accuracy": [
            "test_dbt_blueprint_build_semantic_accuracy_status",
            "test_dbt_blueprint_build_semantic_accuracy_deployment_allowed",
        ],
    }},
    "dbt.project.validate": {{
        "positive": "test_dbt_project_validate_positive_status",
        "protocol_schema": "test_dbt_project_validate_protocol_schema_body_object",
        "malformed_input": "test_dbt_project_validate_malformed_input_status",
        "latency": "test_dbt_project_validate_latency_budget",
        "semantic_accuracy": [
            "test_dbt_project_validate_semantic_accuracy_status",
            "test_dbt_project_validate_semantic_accuracy_validation_errors",
        ],
    }},
}}

AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", {base_url!r})
BUILD_PATH = {build_path!r}
VALIDATE_PATH = {validate_path!r}
BUILD_REQUEST = {build_prompt!r}
VALIDATE_REQUEST = {validate_prompt!r}


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=AGENT_BASE_URL, timeout={build_latency_seconds + 30!r}) as value:
        yield value


@pytest.fixture(scope="module")
def dbt_blueprint_build_result(client):
    started = time.monotonic()
    response = client.post(BUILD_PATH, json=BUILD_REQUEST)
    return response, time.monotonic() - started


@pytest.fixture(scope="module")
def dbt_project_validate_result(client):
    started = time.monotonic()
    response = client.post(VALIDATE_PATH, json=VALIDATE_REQUEST)
    return response, time.monotonic() - started


def test_dbt_blueprint_build_positive_status(dbt_blueprint_build_result):
    response, _ = dbt_blueprint_build_result
    assert response.status_code == 200


def test_dbt_blueprint_build_protocol_schema_body_object(dbt_blueprint_build_result):
    response, _ = dbt_blueprint_build_result
    assert isinstance(response.json(), dict)


def test_dbt_blueprint_build_malformed_input_status(client):
    response = client.post(BUILD_PATH, json={{}})
    assert response.status_code == 400


def test_dbt_blueprint_build_latency_budget(dbt_blueprint_build_result):
    _, elapsed = dbt_blueprint_build_result
    assert elapsed <= {build_latency_seconds!r}


def test_dbt_blueprint_build_semantic_accuracy_status(dbt_blueprint_build_result):
    response, _ = dbt_blueprint_build_result
    assert response.json().get("status") == {build_expected["status"]!r}


def test_dbt_blueprint_build_semantic_accuracy_deployment_allowed(dbt_blueprint_build_result):
    response, _ = dbt_blueprint_build_result
    assert response.json().get("deployment_allowed") is False


def test_dbt_project_validate_positive_status(dbt_project_validate_result):
    response, _ = dbt_project_validate_result
    assert response.status_code == 200


def test_dbt_project_validate_protocol_schema_body_object(dbt_project_validate_result):
    response, _ = dbt_project_validate_result
    assert isinstance(response.json(), dict)


def test_dbt_project_validate_malformed_input_status(client):
    response = client.post(VALIDATE_PATH, json={{}})
    assert response.status_code == 400


def test_dbt_project_validate_latency_budget(dbt_project_validate_result):
    _, elapsed = dbt_project_validate_result
    assert elapsed <= {validate_latency_seconds!r}


def test_dbt_project_validate_semantic_accuracy_status(dbt_project_validate_result):
    response, _ = dbt_project_validate_result
    assert response.json().get("status") == {validate_expected["status"]!r}


def test_dbt_project_validate_semantic_accuracy_validation_errors(dbt_project_validate_result):
    response, _ = dbt_project_validate_result
    assert response.json().get("validation_errors") == {validate_expected["validation_errors"]!r}
'''


class LLMServiceClient:
    """Handles all asynchronous communication with the LLM service, supporting Gemini and Self-Hosted modes."""
    def __init__(self, mode: str, api_key: str):
        self.mode = mode
        self.api_key = api_key 
        self.client = httpx.AsyncClient(
            timeout=LLM_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
        logger.info(f"LLM Service Client initialized in mode: {mode}")

    async def close(self):
        """Closes the underlying httpx client connection pool."""
        await self.client.aclose()

    async def _post_gemini(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """Retry transient Gemini throttling and service failures within the activity deadline."""
        for attempt in range(GEMINI_MAX_ATTEMPTS):
            response = await self.client.post(
                url,
                json=payload,
                headers={"x-goog-api-key": self.api_key},
            )
            if response.status_code not in {429, 500, 502, 503, 504} or attempt + 1 == GEMINI_MAX_ATTEMPTS:
                return response
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2**attempt
            logger.warning(
                "Gemini returned HTTP %s; retrying in %.1fs (%s/%s)",
                response.status_code,
                delay,
                attempt + 1,
                GEMINI_MAX_ATTEMPTS,
            )
            await asyncio.sleep(min(delay, 10))
        raise RuntimeError("unreachable")

    async def get_embedding(self, text: str) -> List[float]:
        """Calls the configured embedding model asynchronously."""
        if self.mode == 'GEMINI':
            url = f"{GEMINI_API_BASE_URL}/models/{GEMINI_EMBEDDING_MODEL}:embedContent"
            payload = {
                "model": f"models/{GEMINI_EMBEDDING_MODEL}",
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": EMBEDDING_DIMENSION,
            }
            
            try:
                response = await self._post_gemini(url, payload)
                response.raise_for_status()
                return response.json()['embedding']['values']
            except Exception as e:
                logger.error(f"Gemini Embedding error: {e}")
                raise
        
        else: # SELF_HOSTED
            try:
                if not LLM_EMBEDDING_ENDPOINT:
                    raise RuntimeError("LLM_EMBEDDING_ENDPOINT is not configured")
                payload = {"input": text}
                if LLM_EMBEDDING_MODEL:
                    payload["model"] = LLM_EMBEDDING_MODEL
                response = await self.client.post(LLM_EMBEDDING_ENDPOINT, json=payload)
                response.raise_for_status()
                result_json = response.json()
                if 'data' in result_json and result_json['data']:
                    embedding = result_json['data'][0].get("embedding", [])
                else:
                    embedding = result_json.get("vector", [])
                if len(embedding) < EMBEDDING_DIMENSION:
                    raise RuntimeError(
                        f"Embedding model returned {len(embedding)} dimensions; "
                        f"AQE requires at least {EMBEDDING_DIMENSION}"
                    )
                return embedding[:EMBEDDING_DIMENSION]
            except Exception as e:
                logger.error(f"Self-Hosted Embedding error: {e}")
                raise

    async def generate_code(self, prompt: str) -> str:
        """Calls the configured generation model asynchronously."""
        if self.mode == 'GEMINI':
            url = f"{GEMINI_API_BASE_URL}/models/{GEMINI_GENERATION_MODEL}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": LLM_MAX_OUTPUT_TOKENS, "temperature": 0.1}
            }
            
            try:
                response = await self._post_gemini(url, payload)
                response.raise_for_status()
                candidate = response.json().get('candidates', [{}])[0]
                finish_reason = candidate.get("finishReason")
                if finish_reason == "MAX_TOKENS":
                    raise RuntimeError(
                        f"Gemini truncated generated test code at the {LLM_MAX_OUTPUT_TOKENS}-token output limit"
                    )
                text = "".join(
                    str(part.get("text") or "")
                    for part in candidate.get("content", {}).get("parts", [])
                    if isinstance(part, dict)
                )
                if not text:
                    raise RuntimeError(
                        f"Gemini generation returned no content (finish_reason={finish_reason})"
                    )
                return text
            except Exception as e:
                logger.error(f"Gemini Generation error: {e}")
                raise

        else: # SELF_HOSTED
            try:
                if not LLM_GENERATION_ENDPOINT:
                    raise RuntimeError("LLM_GENERATION_ENDPOINT is not configured")
                if LLM_GENERATION_ENDPOINT.rstrip("/").endswith("/chat/completions"):
                    payload = {
                        "messages": [{"role": "user", "content": f"/no_think\n{prompt}"}],
                        "max_tokens": LLM_MAX_OUTPUT_TOKENS,
                        "temperature": 0.1,
                        "reasoning_effort": LLM_REASONING_EFFORT,
                    }
                else:
                    payload = {"prompt": prompt, "max_tokens": LLM_MAX_OUTPUT_TOKENS, "temperature": 0.1}
                if LLM_GENERATION_MODEL:
                    payload["model"] = LLM_GENERATION_MODEL
                
                response = await self.client.post(LLM_GENERATION_ENDPOINT, json=payload)
                response.raise_for_status()
                result_json = response.json()
                choice = result_json.get("choices", [{}])[0]
                text = choice.get("text") or choice.get("message", {}).get("content")
                if not text:
                    raise RuntimeError(
                        f"LLM generation returned no content (finish_reason={choice.get('finish_reason')})"
                    )
                return text
            
            except Exception as e:
                logger.error(f"Self-Hosted Generation error: {e}")
                raise


# --- 1. Agent Logic (Pure Business Logic) ---

class TestGenerationAgentLogic:
    """
    Handles all infrastructure connections (RustFS, Postgres, Qdrant) and
    orchestrates the RAG and LLM test generation pipeline, now including 
    persistence of the test code artifact.
    """
    def __init__(self):
        self.object_store = ObjectStore()
        self.db_pool: Optional[asyncpg.Pool] = None
        # Initialize the LLM Service Client based on the configured mode
        self.llm_service = LLMServiceClient(LLM_PROVIDER_MODE, GEMINI_API_KEY)
        logger.info("Agent Logic initialized. RustFS and dynamic LLM service clients created.")

    async def generate_quality_candidate(
        self,
        prompt: str,
        captured_state: Dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Enforce deterministic suite quality before spending an Oracle review."""
        skills = [skill for skill in captured_state.get("skills", []) if isinstance(skill, str)]
        required_dimensions = {
            skill: sorted({
                dimension
                for scenario in captured_state.get("scenarios", [])
                if isinstance(scenario, dict) and scenario.get("skill_id") == skill
                for dimension in scenario.get("required_dimensions", [])
                if isinstance(dimension, str)
            })
            for skill in skills
        }
        candidate = ""
        issues: list[dict[str, Any]] = []
        candidate_prompt = prompt
        for attempt in range(2):
            candidate = (await self.llm_service.generate_code(candidate_prompt)).strip()
            if candidate.startswith("```python"):
                candidate = candidate.replace("```python", "").replace("```", "").strip()
            issues = [
                issue.as_dict()
                for issue in inspect_test_code(
                    candidate,
                    skills,
                    required_dimensions,
                    require_semantic_names=bool(skills),
                )
            ]
            if not issues:
                return candidate, []
            if attempt == 0:
                candidate_prompt = f"""{prompt}

DETERMINISTIC PRE-ORACLE QUALITY GATE FAILED.
Regenerate the complete suite and correct every violation below. Do not delete intended coverage,
weaken assertions, or add undeclared behavior. Return Python source only.
{json.dumps(issues, default=str)}
"""
        return candidate, issues

    async def init_db_pool(self):
        """
        Initializes the asynchronous PostgreSQL connection pool.
        """
        if not self.db_pool:
            try:
                self.db_pool = await asyncpg.create_pool(POSTGRES_DB_URL)
                
                async with self.db_pool.acquire() as conn:
                    
                    # Schema setup is non-destructive. Production migrations own
                    # schema evolution; an agent must never erase test history.
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS test_runs (
                            -- Use task_id for consistency with Reporting Service, using UUID as PRIMARY KEY
                            task_id TEXT PRIMARY KEY, 
                            
                            -- Agent persistence columns (for initial save and execution update)
                            app_id TEXT NOT NULL,
                            status TEXT NOT NULL,
                            generated_by_user_id TEXT,
                            timestamp_created TIMESTAMPTZ DEFAULT NOW(),
                            timestamp_completed TIMESTAMPTZ,
                            object_path TEXT NOT NULL,
                            execution_results JSONB DEFAULT NULL,

                            -- Reporting service columns (added for robustness if Reporting Service runs first)
                            url TEXT,
                            passed BOOLEAN DEFAULT FALSE,
                            summary JSONB,
                            raw_code TEXT,
                            data_artifact_version VARCHAR(50),
                            target_agent_id TEXT,
                            target_agent_version TEXT,
                            target_agent_card_url TEXT,
                            target_agent_skills JSONB,
                            target_agent_profile JSONB,
                            test_catalog JSONB,
                            test_type TEXT NOT NULL DEFAULT 'agent'
                        );
                    """)
                    await conn.execute("""
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_id TEXT;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_version TEXT;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_card_url TEXT;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_skills JSONB;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_profile JSONB;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS test_catalog JSONB;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS test_type TEXT NOT NULL DEFAULT 'agent';
                    """)
                    
                    # 2. Also ensure 'active_artifacts' table exists (if we use it)
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS active_artifacts (
                            artifact_type VARCHAR(50) PRIMARY KEY,
                            current_version_id VARCHAR(50) NOT NULL,
                            object_path TEXT NOT NULL,
                            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    
                logger.info("PostgreSQL connection pool established and schemas verified (test_runs forced updated).")
            except Exception as e:
                logger.error(f"Failed to connect to PostgreSQL or initialize schema: {e}")
                raise

    async def tear_down(self):
        """Gracefully close resources upon agent shutdown."""
        if self.db_pool:
            await self.db_pool.close()
            logger.info("PostgreSQL connection pool closed.")
        await self.llm_service.close() 
        logger.info("LLM Service client closed.")
            
    async def _get_artifact_metadata(self, artifact_type: str) -> Tuple[str, str]:
        """Queries PostgreSQL for the active artifact version ID and object path."""
        if not self.db_pool: await self.init_db_pool()
        async with self.db_pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "SELECT current_version_id, object_path FROM active_artifacts WHERE artifact_type = $1",
                    artifact_type
                )
                if not row:
                    raise FileNotFoundError(f"No active artifact found for type: {artifact_type}")
                return row['current_version_id'], row['object_path']
            except FileNotFoundError:
                raise
            except Exception as e:
                logger.error(f"Error querying artifact metadata: {e}")
                raise
    
    async def _fetch_artifact(self, object_path: str) -> Dict[str, Any]:
        """Fetches an artifact from RustFS without blocking the event loop."""
        logger.info(f"Fetching artifact from RustFS path: {object_path}")
        def blocking_download():
            return self.object_store.read_bytes(object_path)
        try:
            data_bytes = await asyncio.to_thread(blocking_download)
            return json.loads(data_bytes.decode('utf-8'))
        except Exception as e:
            logger.error(f"RustFS download failed for {object_path}: {e}")
            raise

    async def retrieve_knowledge(self, query: str) -> List[str]:
        """Queries Qdrant for knowledge semantically similar to the current task."""
        # 1. Embed the query
        query_vector = await self.llm_service.get_embedding(query)

        # 2. Search Qdrant
        def blocking_search():
            return QDRANT_CLIENT.search(
                collection_name=COLLECTION_NAME,
                query_vector=query_vector,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source",
                            match=models.MatchValue(value="product_knowledge"),
                        )
                    ]
                ),
                limit=3
            )

        search_result = await asyncio.to_thread(blocking_search)
        return [hit.payload['text_chunk'] for hit in search_result if hit.payload and 'text_chunk' in hit.payload]

    async def _store_test_artifact(self, task_id: str, test_code: str, target_identity: str) -> str:
        """
        Stores generated test code in RustFS and returns its object path.
        """
        # Define a consistent path structure for test code artifacts
        object_path = f"artifacts/targets/{_target_segment(target_identity)}/tests/{task_id}/test_code.py"
        data_bytes = test_code.encode('utf-8')
        data_size = len(data_bytes)
        
        logger.info(f"Uploading {data_size} bytes to RustFS path: {object_path}")

        def blocking_upload():
            self.object_store.write_bytes(object_path, data_bytes, "text/x-python")

        try:
            await asyncio.to_thread(blocking_upload)
            return object_path
        except Exception as e:
            logger.error(f"RustFS upload failed for {object_path}: {e}")
            raise

    async def _create_test_run_metadata_in_postgres(
        self,
        task_id: str,
        object_path: str,
        test_spec: str,
        url: str,
        captured_state: Dict[str, Any],
    ) -> None:
        """
        Creates the initial PENDING metadata entry in PostgreSQL.
        Uses task_id for consistency with reporting service.
        """
        if not self.db_pool: await self.init_db_pool()
        target_identity = resolve_target_identity(captured_state)
        target_profile = {
            key: captured_state[key]
            for key in (
                "agent_archetype", "autonomy", "data_sensitivity",
                "impact", "network_scope", "risk_labels",
            )
            if key in captured_state
        }
        target_profile["skill_test_dimensions"] = {
            skill_id: sorted(
                {
                    dimension
                    for scenario in captured_state.get("scenarios", [])
                    if isinstance(scenario, dict) and scenario.get("skill_id") == skill_id
                    for dimension in scenario.get("required_dimensions", [])
                }
            )
            for skill_id in captured_state.get("skills", [])
            if isinstance(skill_id, str)
        }
        target_profile["source_repository"] = captured_state.get("source_repository")
        target_profile["source_revision"] = captured_state.get("source_ref")
        target_profile["generation_fingerprint"] = generation_fingerprint(captured_state)
        target_profile["generation_policy_version"] = GENERATION_POLICY_VERSION

        async with self.db_pool.acquire() as conn:
            try:
                await conn.execute("""
                    INSERT INTO test_runs (
                        task_id, app_id, status, generated_by_user_id, object_path, url, raw_code,
                        target_agent_id, target_agent_version, target_agent_card_url, target_agent_skills,
                        target_agent_profile, test_type
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb, $13)
                    ON CONFLICT (task_id) DO UPDATE SET 
                        status = EXCLUDED.status, 
                        object_path = EXCLUDED.object_path,
                        url = EXCLUDED.url,
                        raw_code = EXCLUDED.raw_code,
                        target_agent_id = EXCLUDED.target_agent_id,
                        target_agent_version = EXCLUDED.target_agent_version,
                        target_agent_card_url = EXCLUDED.target_agent_card_url,
                        target_agent_skills = EXCLUDED.target_agent_skills,
                        target_agent_profile = EXCLUDED.target_agent_profile,
                        test_type = EXCLUDED.test_type;
                """,
                    task_id,
                    target_identity,
                    "PENDING", # Initial status
                    USER_ID,   # User ID of the agent that generated the run
                    object_path,
                    url,
                    "# Code is stored in RustFS.",
                    target_identity,
                    str((captured_state.get("target_agent") or {}).get("version") or captured_state.get("agent_version") or "unversioned"),
                    captured_state.get("agent_card_url"),
                    json.dumps(captured_state.get("skills", [])),
                    json.dumps(target_profile),
                    resolve_test_type(captured_state),
                )
                logger.info(f"PostgreSQL metadata created for task_id: {task_id}")
            except Exception as e:
                logger.error(f"PostgreSQL insert failed for {task_id}: {e}")
                raise


    async def _find_reusable_test(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        if not self.db_pool:
            await self.init_db_pool()
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT task_id, object_path, test_type, data_artifact_version
                FROM test_runs
                WHERE status = 'PASSED' AND passed = TRUE AND test_catalog IS NOT NULL
                  AND target_agent_profile->>'generation_fingerprint' = $1
                ORDER BY timestamp_completed DESC
                LIMIT 1
                """,
                fingerprint,
            )
        if row is None:
            return None
        return {
            "status": "SUCCESS",
            "task_id": row["task_id"],
            "rag_version_id": row["data_artifact_version"] or "REUSED",
            "object_path": row["object_path"],
            "test_type": row["test_type"],
            "grounding": {"reused_approved_suite": True, "generation_fingerprint": fingerprint},
            "reused": True,
        }


    async def generate_tests_and_persist(self, captured_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main orchestration method: generates code, persists it, and returns the ID.
        """
        fingerprint = generation_fingerprint(captured_state)
        reusable = await self._find_reusable_test(fingerprint)
        if reusable:
            logger.info("Reusing approved generated suite for fingerprint %s", fingerprint)
            return reusable

        # 1. Generate Test Code (using existing RAG/LLM logic)
        generation_result = await self.generate_tests(captured_state)
        test_code = generation_result.get("test_code")
        version_id = generation_result.get("artifact_version_used")
        grounding = generation_result.get("grounding") or {}
        test_spec = captured_state.get('spec', 'General Test')
        target_url = captured_state.get('url', 'N/A')

        # Check if the generation failed before attempting persistence
        if test_code.startswith("# Error:"):
            return {"status": "FAILED", "error": test_code}

        # 2. Persistence Layer
        try:
            # Generate a unique ID for this execution run, using the consistent name task_id
            task_id = str(uuid.uuid4())
            target_identity = resolve_target_identity(captured_state)
            object_path = await self._store_test_artifact(task_id, test_code, target_identity)
            
            await self._create_test_run_metadata_in_postgres(
                task_id, object_path, test_spec, target_url, captured_state
            )
            try:
                await asyncio.wait_for(
                    self.llm_service.client.post(
                        GRAPH_API_URL,
                        json={
                            "kind": "test_generated",
                            "task_id": task_id,
                            "test_type": resolve_test_type(captured_state),
                            "target_agent": captured_state.get("target_agent") or {},
                            "agent_archetype": captured_state.get("agent_archetype"),
                        },
                        timeout=GRAPH_EVENT_TIMEOUT_SECONDS,
                    ),
                    timeout=GRAPH_EVENT_TIMEOUT_SECONDS,
                )
            except (httpx.HTTPError, asyncio.TimeoutError):
                logger.warning("Live graph event could not be delivered", exc_info=True)
            logger.info("Generation response ready for task_id: %s", task_id)
            
            # Return the ID and metadata needed by the client/Execution Agent
            return {
                "status": "SUCCESS",
                "task_id": task_id, # Return task_id instead of test_run_id
                "rag_version_id": version_id,
                "object_path": object_path,
                "test_type": resolve_test_type(captured_state),
                "grounding": grounding,
            }
        except Exception as e:
            error_message = f"# Error during persistence (RustFS/Postgres): {str(e)}"
            logger.error(error_message, exc_info=True)
            return {"status": "FAILED", "error": error_message}
            

    async def generate_tests(self, captured_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generates production-ready Python for one explicitly selected test runtime.
        """
        compiled_suite = (
            compile_declared_github_mcp_suite(captured_state)
            or compile_declared_dbt_builder_suite(captured_state)
        )
        if compiled_suite:
            logger.info("Compiled declared first-party contract without generation-model inference")
            return {
                "test_code": compiled_suite,
                "artifact_version_used": "DECLARED_CONTRACT",
                "grounding": {
                    "generation_method": "declared_contract_compiler",
                    "rag_version_id": "NOT_REQUIRED",
                    "product_context": [],
                    "ontology_context": [],
                },
            }
        rag_artifact_type = "RAG_KNOWLEDGE_BASE" 
        version_id = "UNAVAILABLE"

        try:
            try:
                version_id, object_path = await self._get_artifact_metadata(rag_artifact_type)
                await self._fetch_artifact(object_path)
            except FileNotFoundError:
                logger.info("No product RAG artifact is active; generating from the scenario and ontology.")

            target_url = captured_state.get('url', 'N/A')
            test_spec = captured_state.get('spec', 'Run general tests.')
            kb_context = captured_state.get('kb', 'No extra knowledge provided.')
            target_agent = captured_state.get("target_agent") or {}
            agent_card_url = captured_state.get("agent_card_url", "")
            agent_skills = captured_state.get("skills", target_agent.get("skills", []))
            test_type = resolve_test_type(captured_state)
            source_analysis = captured_state.get("source_analysis") or {}
            agent_discovery = captured_state.get("agent_discovery") or {}
            refined_requirements = captured_state.get("refined_requirements") or []
            
            query_context = (
                f"Generate test for URL {target_url} based on spec: '{test_spec}'. "
                f"Use this additional context: '{kb_context}'"
            )
            
            product_context_chunks = await self.retrieve_knowledge(query_context)
            product_context = "\n".join([f"- {c}" for c in product_context_chunks])
            ontology_records = select_ontology_context(captured_state)
            ontology_context = "\n".join(f"- {record}" for record in ontology_records)
            ontology_version = load_ontology()["version"]
            
            rag_context_section = f"""
        ***
        PRODUCT CONTEXT (from Qdrant RAG V{version_id}):
        {product_context if product_context else "No specific product knowledge found. Rely on general web automation best practices."}

        APPLICABLE AGENT ONTOLOGY (V{ontology_version}):
        {ontology_context}
        ***"""
            
            llm_constraint = """
            ***
            TEST GENERATION CONSTRAINTS (CRITICAL for Execution Stability):
            1. **Explicit Waiting (Mandatory):** Always use explicit waiting functions (e.g., `locator.wait_for(state='visible')` or `page.wait_for_selector`) instead of fixed timeouts (`page.wait_for_timeout`).
            2. **Strict Mode Compliance (CRITICAL):** Playwright requires locators to resolve to a SINGLE element. **AVOID** generic locators (like `page.get_by_text("Link Text")`) if the text appears multiple times (e.g., in the header and footer).
            3. **Unique Locators (Mandatory):** For unique and critical elements, use highly specific methods:
                - **Primary Method:** `page.get_by_role("role_name", name="Accessible Name/Text")` (e.g., `page.get_by_role("link", name="Enrich Finance")`).
                - **Secondary Method:** If a general text is the only option, use `page.get_by_text("Text fragment", exact=True).first` or combine it with a unique container, like `page.locator("header").get_by_text("Enrich Finance")`.
            4. **Search Engine Target:** If the test objective involves a search engine for a generic test, **use DuckDuckGo (https://duckduckgo.com/)** instead of Google, as Playwright often gets blocked.
            5. **Atomic Tests:** Every `test_` function verifies exactly one observable outcome with exactly one Python `assert` or Playwright `expect(...)`. Split multiple outcomes into separate tests and share setup through fixtures.
            6. **Do Not Hide Failures:** Never weaken expected values, catch assertion failures, use arbitrary sleeps, or conditionally skip an assertion to make a test pass.
            7. **Isolation:** Tests must not depend on execution order or state left by another test. Use fixtures for setup and cleanup.
            8. **Exact Declared Scope:** Generate only each scenario's `required_dimensions`. Do not add authentication, tenancy, retries, cancellation, streaming, or audit requirements unless explicitly declared.
            9. **Malformed Input:** A malformed HTTP request must be rejected with a non-2xx status or a declared structured protocol error; never treat an unexplained 200 response as successful rejection.
            ***"""

            full_prompt = f"""
            You are an expert Python end-to-end test automation engineer.
            Generate a complete executable pytest file for TEST TYPE: {test_type}.
            If TEST TYPE is agent, use httpx; use utils.target_auth.authenticated_client only when
            authentication is explicitly declared. Do not import Playwright or depend on a browser.
            Validate the Agent Card, A2A/JSON-RPC/API,
            declared skill, and orchestration contracts. Resolve the runtime endpoint with
            `os.getenv("AGENT_CARD_URL", supplied_agent_card_url)` for Agent Card requests and
            `os.getenv("AGENT_BASE_URL", supplied_target_url)` for other requests. Never hardcode
            a cluster-only hostname without this environment override.
            If TEST TYPE is website, use Playwright and utils.target_auth browser helpers; test
            observable browser behavior. Resolve its runtime endpoint with
            `os.getenv("TARGET_BASE_URL", supplied_target_url)`. Use login_with_form only when
            form auth is configured.
            Do not assume a profession or business domain. Derive behavior only from the supplied
            Agent Card, declared skills, scenario, product evidence, and observable outcomes.
            
            Target URL: {target_url}
            Target Agent: {json.dumps(target_agent, default=str)}
            Agent Card URL: {agent_card_url or "Not supplied"}
            Declared Agent Skills: {json.dumps(agent_skills, default=str)}
            Discovered Agent Contracts and Evaluation Scenarios:
            {json.dumps(agent_discovery, default=str)}
            AUTOPILOT Scenarios Selected by the Fleet Controller:
            {json.dumps(captured_state.get("scenarios") or [], default=str)}
            Refined Requirements and Expected Outcomes:
            {json.dumps(refined_requirements, default=str)}
            GitHub Source Analysis (candidate evidence, not a confirmed defect):
            {json.dumps(source_analysis, default=str)}
            Test Objective/Specification: {test_spec}
            Additional User Context: {kb_context}
            Previous Quality Oracle Feedback (empty on the first attempt):
            {json.dumps(captured_state.get("oracle_feedback") or {}, default=str)}

            If Oracle feedback is present, regenerate the complete suite and correct every cited issue
            without weakening assertions, dropping declared coverage, or adding undeclared requirements.
            
            {llm_constraint}

            {rag_context_section}

            Generate version-safe E2E tests for the declared behavior. Validate protocol contracts,
            orchestration handoffs, and domain outcomes without replacing real expected values with mocks.
            Define `AQE_TEST_LAYER` as the suite's primary layer: `api`, `db`, `ui`, `agentic`,
            `integration`, `performance`, or `security`. Agent protocol/skill suites normally use
            `agentic`; dbt data-contract suites use `db`; browser journeys use `ui`.
            Define `AQE_SUITE_ID` as a semantic kebab-case capability or user-journey name for every
            generated suite; never use a UUID or generic name such as generated-test.
            For agent tests, define a literal module-level `AQE_SKILL_TESTS` dictionary. It must map
            every advertised skill ID to exactly the independently executable pytest dimensions listed
            in that skill's scenarios under `required_dimensions`; each dimension may name one atomic test
            function or a non-empty list of atomic test functions when multiple observable invariants are
            declared. Invoke the skill in every mapped test—Agent Card membership alone is not skill coverage.
            For MCP, test tools/list and invoke only declared safe tools.
            Every mapped pytest function name must include the normalized skill ID and dimension, for example
            `test_dbt_project_validate_malformed_input`; never use UUIDs or names such as test_case_1.
            For A2A, validate JSON-RPC envelopes, task states, and declared streaming/cancellation.
            Add semantic-accuracy tests only when an explicit expected response is supplied. If an
            invocation URL, payload, or prompt is absent, do not invent it or emit a fake passing test:
            emit a statically skipped contract marker whose reason starts with `REQUIREMENTS_NEEDED:`;
            never use `assert False` for missing requirements.
            When source findings are supplied, design observable black-box tests that could reproduce
            them; never assert that a source candidate is a real defect without runtime evidence.
            The response must be *only* the Python code block.
            """

            test_code, quality_issues = await self.generate_quality_candidate(full_prompt, captured_state)
            if quality_issues:
                details = "; ".join(issue["message"] for issue in quality_issues)
                return {
                    "test_code": f"# Error: Generated suite failed deterministic quality gate after regeneration: {details}",
                    "artifact_version_used": version_id,
                    "grounding": {
                        "rag_version_id": version_id,
                        "product_context": product_context_chunks,
                        "ontology_context": ontology_records,
                        "quality_issues": quality_issues,
                    },
                }

            return {
                "test_code": test_code,
                "artifact_version_used": version_id,
                "grounding": {
                    "rag_version_id": version_id,
                    "product_context": product_context_chunks,
                    "ontology_context": ontology_records,
                },
            }

        except Exception as e:
            detail = str(e) or type(e).__name__
            error_message = f"# Error: Failed during generation pipeline (RAG Version: {version_id}). Details: {detail}"
            logger.error(error_message, exc_info=True)
            return {
                "test_code": error_message,
                "artifact_version_used": version_id
            }


# --- 2. Agent Executor (A2A Protocol Implementation) ---

class TestGenerationAgentExecutor(AgentExecutor): 
    """
    Implements the A2A protocol methods (execute, cancel) and delegates 
    to the TestGenerationAgentLogic.
    """

    def __init__(self):
        # Instantiate the Agent Logic class
        self.agent = TestGenerationAgentLogic()
        logger.info("TestGenerationAgentExecutor initialized.")

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        # For A2A protocol, input arguments are used.
        captured_state = context.input_args.get("captured_state")
        
        if not captured_state or not isinstance(captured_state, dict):
            error_message = "Execution failed: Missing or invalid 'captured_state' argument in request."
            logger.error(error_message)
            await event_queue.enqueue_event(new_agent_text_message(error_message))
            return

        # Call the persistence orchestration method
        result = await self.agent.generate_tests_and_persist(captured_state)
        
        # Send the final result back to the user/caller via the EventQueue
        message = json.dumps(result, indent=2)
        await event_queue.enqueue_event(new_agent_text_message(f"Test Generation Complete:\n{message}"))
        logger.info("Test generation complete and result sent.")

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # Gracefully shut down clients if necessary
        await self.agent.tear_down()
        logger.warning('Agent shut down during cancellation.')


# --- 3. Custom Endpoint Handlers ---

# Global instance of the agent logic to be used by the custom handlers
AGENT_LOGIC = TestGenerationAgentLogic()

async def health_endpoint(request: Request):
    logger.debug("/health endpoint accessed.")
    provider = await llm_provider_status(
        mode=LLM_PROVIDER_MODE,
        api_key=GEMINI_API_KEY,
        generation_endpoint=LLM_GENERATION_ENDPOINT,
    )
    return JSONResponse({"status": "UP", "llm_provider": provider}, status_code=200)

async def agent_card_endpoint(request: Request):
    logger.debug("/agent_card endpoint accessed.")
    agent_id = os.environ.get("AGENT_ID", "TestGenerationAgent")
    prompt = {
        "url": "http://aqe-diagnostics:8006",
        "agent_card_url": "http://aqe-diagnostics:8006/agent_card",
        "test_type": "agent",
        "agent_name": "aqe-diagnostics",
        "agent_version": "2.0.0",
        "skills": ["diagnostics.scan"],
        "target_agent": {
            "id": "aqe-diagnostics",
            "version": "2.0.0",
            "card_url": "http://aqe-diagnostics:8006/agent_card",
            "skills": ["diagnostics.scan"],
        },
        "scenarios": [{
            "scenario_id": "candidate-diagnostics-scan",
            "skill_id": "diagnostics.scan",
            "prompt": {},
            "invocation": {
                "protocol": "rest",
                "method": "GET",
                "url": "http://aqe-diagnostics:8006/v1/diagnostics",
            },
            "expected_response": {"status": "healthy or degraded", "agents": "array"},
            "required_dimensions": ["positive", "protocol_schema", "latency", "semantic_accuracy"],
            "max_latency_ms": 30000,
            "min_accuracy": 1.0,
        }],
        "spec": "Generate atomic tests for the declared diagnostics.scan scenario only.",
    }
    return JSONResponse(
        {
            "status": "UP",
            "agent_id": agent_id,
            "version": "1.0.0",
            "skills": [{
                "id": "generate_tests",
                "description": "Generate and persist a grounded candidate test suite",
                "examples": [prompt],
                "invocation": {
                    "protocol": "rest",
                    "method": "POST",
                    "url": f"{PUBLIC_BASE_URL}/generate_test_plan",
                },
            }],
            "evaluation": {"cases": [{
                "id": "generate-candidate-diagnostics-suite",
                "skill_id": "generate_tests",
                "prompt": prompt,
                "expected_response": {
                    "status": "SUCCESS",
                    "task_id": "non-empty string",
                    "object_path": "non-empty RustFS artifact path",
                    "test_type": "agent",
                },
                "required_dimensions": [
                    "positive", "protocol_schema", "malformed_input", "latency", "semantic_accuracy"
                ],
                "max_latency_ms": int(LLM_TIMEOUT_SECONDS * 1000),
                "min_accuracy": 1.0,
            }]},
            "message": "Agent is healthy.",
        },
        status_code=200
    )


async def generate_tests_handler(request: Request):
    """
    Handles the custom HTTP POST request, generates tests, and persists the run.
    It returns the task_id.
    """
    try:
        body = await request.json()
        logger.info(f"Received JSON body for test generation: {body}")
        
        target_url = body.get('url')
        if not target_url:
            return JSONResponse({"status": "FAILED", "error": "Missing 'url'."}, status_code=400)

        # Call the new orchestration method
        result = await AGENT_LOGIC.generate_tests_and_persist(body)
        
        if result["status"] == "FAILED":
            return JSONResponse({
                "status": "FAILED", 
                "error_details": result["error"]
            }, status_code=500)

        # Preserve immutable artifact provenance for the Oracle and executor.
        return JSONResponse(result, status_code=200)

    except json.JSONDecodeError:
        logger.error("Error decoding JSON request body.")
        return JSONResponse({"status": "FAILED", "error": "Invalid JSON format."}, status_code=400)
    except Exception as e:
        logger.error(f"Error processing generate_tests request: {e}", exc_info=True)
        return JSONResponse({"status": "FAILED", "error": f"Internal server error: {e}"}, status_code=500)


# --- 4. Server Startup (The Executor that makes the agent runnable) ---

if __name__ == '__main__':
    # Configuration is pulled from the Docker environment variables
    AGENT_PORT = int(os.environ.get("AGENT_PORT", 8001))
    AGENT_ID = os.environ.get("AGENT_ID", "TestGenerationAgent")

    # 1. Define the Agent's capabilities (AgentCard)
    skill = AgentSkill(
        id='generate_tests',
        name='Generate Agent or Website Tests via RAG-LLM Pipeline',
        description='Generates isolated HTTP agent tests or Playwright website tests using versioned evidence.',
        tags=['qa', 'llm', 'rag', 'rustfs', 'postgres'],
        examples=['generate tests for the captured state'],
    )

    agent_card = AgentCard(
        name=AGENT_ID,
        description='Generates high-quality, grounded tests using product specs and LLMs.',
        url=f'http://0.0.0.0:{AGENT_PORT}/',
        version='1.0.0',
        default_input_modes=['args'],
        default_output_modes=['text'],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill], 
    )

    # 2. Instantiate the Executor
    executor = TestGenerationAgentExecutor()
    
    # 3. Create the Request Handler (A2A server plumbing)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    # 4. Create the A2A Starlette Application and build the ASGI app
    server_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )
    starlette_app = server_app.build()

    # Define the custom routes required by the client/orchestrator
    custom_routes = [
        Route("/health", endpoint=health_endpoint, methods=["GET", "OPTIONS"]), 
        Route("/agent_card", endpoint=agent_card_endpoint, methods=["GET", "OPTIONS"]),
        Route("/generate_test_plan", endpoint=generate_tests_handler, methods=["POST", "OPTIONS"]),
    ]
    
    for route in reversed(custom_routes): 
        starlette_app.routes.insert(0, route)

    # 5. Apply the CORS Middleware - this must wrap the entire application.
    cors_app = CORSMiddleware(
        app=starlette_app, 
        allow_origins=["*"], 
        allow_credentials=True,
        allow_methods=["*"], 
        allow_headers=["*"],
    )

    logger.info(f"Starting A2A Server for {AGENT_ID} on port {AGENT_PORT}...")
    
    # 6. Run the server using Uvicorn, pointing to the CORS-wrapped app
    uvicorn.run(PrometheusMiddleware(cors_app, "test-generation"), host='0.0.0.0', port=AGENT_PORT)
