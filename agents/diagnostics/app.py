"""Diagnostics, self-healing recommendations, and fleet-contract probes."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from observability.metrics import PrometheusMiddleware
from prometheus_client import Gauge
from utils.agent_ontology import classify_agent, discover_fleet_patterns


DEFAULT_AGENT_CARDS = {
    "agent-builder": "http://agent_builder_agent:8015/agent_card",
    "artifact-management": "http://artifact_management_agent:8007/agent_card",
    "change-detection": "http://change_detection_agent:8000/agent_card",
    "dbt-builder": "http://dbt_builder_agent:8016/agent_card",
    "test-generation": "http://test_generation_agent:8001/agent_card",
    "github-analysis": "http://github_analysis_agent:8010/agent_card",
    "github-connector": "http://github_connector_agent:8014/agent_card",
    "github-commit": "http://github_commit_agent:8011/agent_card",
    "webpage-state-capture": "http://webpage_state_capture_agent:8002/agent_card",
    "test-execution": "http://test_execution_agent:8003/agent_card",
    "website-execution": "http://website_test_execution_agent:8013/agent_card",
    "knowledge-ingestion": "http://llm_fine_tuning_agent:8004/agent_card",
    "quality-oracle": "http://quality_oracle_agent:8017/agent_card",
    "aqe-byoa": "http://aqe_byoa:8009/.well-known/agent.json",
}
PUBLIC_BASE_URL = os.getenv("DIAGNOSTICS_PUBLIC_URL", "http://diagnostics_agent:8008").rstrip("/")
EXPECTED_INTERNAL_SKILLS = {
    "agent-builder": ["agent.build.experimental"],
    "artifact-management": ["upload_artifact"],
    "change-detection": ["detect_change_and_orchestrate"],
    "dbt-builder": ["dbt.blueprint.build", "dbt.project.validate"],
    "diagnostics": ["diagnostics.scan", "diagnostics.probe", "diagnostics.heal"],
    "github-analysis": ["source.inspect"],
    "github-connector": ["github.tools.list", "github.tools.call"],
    "github-commit": ["start_commit_consumer"],
    "knowledge-ingestion": ["ingest_knowledge", "ingest_ontology"],
    "quality-oracle": ["oracle.review.generated_test"],
    "test-generation": ["generate_tests"],
    "test-execution": ["qe.validate", "qe.repair"],
    "webpage-state-capture": ["capture_state"],
    "website-execution": ["qe.validate", "qe.repair"],
    "aqe-byoa": ["qe.run", "qe.validate", "qe.repair"],
}
GRAPH_EVENTS: deque[dict[str, Any]] = deque(maxlen=100)
OBSERVED_AGENTS: dict[str, dict[str, Any]] = {}
LATEST_EVALUATION: dict[str, Any] = {}
EVALUATION_SCORE = Gauge("aqe_model_evaluation_score", "Latest model release evaluation score", ("shard",))
EVALUATION_SEMANTIC_SCORE = Gauge("aqe_model_semantic_score", "Latest semantic golden-set score", ("shard",))
EVALUATION_AVERAGE_LATENCY = Gauge("aqe_model_evaluation_average_latency_ms", "Mean model evaluation latency", ("shard",))
EVALUATION_CASES = Gauge("aqe_model_evaluation_cases", "Latest evaluated golden cases", ("shard", "result"))


def configured_cards() -> dict[str, str]:
    configured = os.getenv("AGENT_CARD_URLS", "")
    if not configured:
        return DEFAULT_AGENT_CARDS
    cards: dict[str, str] = {}
    for item in configured.split(","):
        name, separator, url = item.strip().partition("=")
        if separator and name and url:
            cards[name] = url
    return cards


def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return False
    allowed = {host.strip() for host in os.getenv("AGENT_PROBE_ALLOWED_HOSTS", "").split(",") if host.strip()}
    configured_hosts = {
        urlparse(card_url).hostname
        for card_url in configured_cards().values()
        if urlparse(card_url).hostname
    }
    return "*" in allowed or parsed.hostname in allowed or parsed.hostname in configured_hosts


def _skills(card: dict[str, Any]) -> set[str]:
    return {
        str(skill.get("id"))
        for skill in card.get("skills", [])
        if isinstance(skill, dict) and skill.get("id")
    }


def _evaluation_scenarios(
    card: dict[str, Any],
    *,
    default_max_latency_ms: int,
    default_min_accuracy: float,
) -> list[dict[str, Any]]:
    """Build measurable scenarios without inventing expected domain answers."""
    evaluation = card.get("evaluation") if isinstance(card.get("evaluation"), dict) else {}
    declared_cases = evaluation.get("cases", [])
    scenarios: list[dict[str, Any]] = []
    for skill in card.get("skills", []):
        if not isinstance(skill, dict) or not skill.get("id"):
            continue
        skill_id = str(skill["id"])
        matching_cases = [
            case
            for case in declared_cases
            if isinstance(case, dict) and str(case.get("skill_id")) == skill_id
        ]
        if not matching_cases:
            matching_cases = [{}]
        examples = skill.get("examples", [])
        for case_index, declared in enumerate(matching_cases):
            prompt = declared.get("prompt") or (examples[case_index] if case_index < len(examples) else None)
            expected = declared.get("expected_response")
            invocation = declared.get("invocation") or skill.get("invocation")
            if invocation is None and card.get("url"):
                invocation = {
                    "protocol": "a2a_jsonrpc",
                    "url": card["url"],
                    "method": "tasks.execute",
                }
            contract_gaps = []
            if not isinstance(invocation, dict) or not invocation.get("url"):
                contract_gaps.append("invocation")
            if prompt is None:
                contract_gaps.append("prompt")
            if expected is None:
                contract_gaps.append("semantic_oracle")
            declared_dimensions = declared.get("required_dimensions")
            dimensions = (
                declared_dimensions
                if isinstance(declared_dimensions, list) and declared_dimensions
                else ["positive", "protocol_schema", "malformed_input", "latency"]
            )
            required_dimensions = list(
                dict.fromkeys([*dimensions, *(["semantic_accuracy"] if expected is not None else [])])
            )
            scenarios.append(
                {
                    "scenario_id": str(declared.get("id") or f"{skill_id}-{case_index + 1}"),
                    "skill_id": skill_id,
                    "prompt": prompt,
                    "expected_response": expected,
                    "invocation": invocation,
                    "required_dimensions": required_dimensions,
                    "max_latency_ms": int(
                        declared.get("max_latency_ms", default_max_latency_ms)
                    ),
                    "min_accuracy": float(
                        declared.get("min_accuracy", default_min_accuracy)
                    ),
                    "evaluation_mode": "semantic_accuracy" if expected is not None else "protocol_only",
                    "oracle_status": "declared" if expected is not None else "requirements_needed",
                    "execution_status": "executable" if not set(contract_gaps) - {"semantic_oracle"} else "requirements_needed",
                    "contract_gaps": contract_gaps,
                }
            )
    return scenarios


async def discover_agents(
    card_urls: list[str],
    *,
    max_depth: int = 2,
    max_agents: int = 50,
    default_max_latency_ms: int = 5000,
    default_min_accuracy: float = 0.8,
) -> dict[str, Any]:
    """Discover allowlisted Agent Cards and bounded card-declared peers."""
    queue = deque((url, 0) for url in card_urls)
    visited: set[str] = set()
    agents: list[dict[str, Any]] = []
    while queue and len(visited) < max_agents:
        card_url, depth = queue.popleft()
        if card_url in visited:
            continue
        visited.add(card_url)
        result = await probe_card(f"discovered-{len(agents) + 1}", card_url)
        if result["status"] in {"blocked", "unhealthy"}:
            agents.append(result)
            continue
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                response = await client.get(card_url)
                response.raise_for_status()
                card = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            agents.append({**result, "status": "unhealthy", "recommendation": str(exc)})
            continue
        result["scenarios"] = _evaluation_scenarios(
            card,
            default_max_latency_ms=default_max_latency_ms,
            default_min_accuracy=default_min_accuracy,
        )
        result["invocation_url"] = card.get("url")
        result["card"] = card
        agents.append(result)
        if depth >= max_depth:
            continue
        peers = card.get("peers", [])
        orchestration = card.get("orchestration")
        if isinstance(orchestration, dict):
            peers = [*peers, *orchestration.get("agents", [])]
        for peer in peers:
            peer_url = peer.get("card_url") if isinstance(peer, dict) else peer
            if isinstance(peer_url, str):
                resolved = urljoin(card_url, peer_url)
                if resolved not in visited:
                    queue.append((resolved, depth + 1))
    return {
        "status": "completed",
        "agents": agents,
        "summary": {
            "discovered": len(agents),
            "testable_scenarios": sum(len(agent.get("scenarios", [])) for agent in agents),
            "executable_scenarios": sum(
                scenario.get("execution_status") == "executable"
                for agent in agents
                for scenario in agent.get("scenarios", [])
            ),
            "missing_execution_contracts": sum(
                scenario.get("execution_status") == "requirements_needed"
                for agent in agents
                for scenario in agent.get("scenarios", [])
            ),
            "missing_oracles": sum(
                scenario.get("oracle_status") == "requirements_needed"
                for agent in agents
                for scenario in agent.get("scenarios", [])
            ),
        },
    }


async def probe_card(
    name: str,
    card_url: str,
    expected_skills: list[str] | None = None,
) -> dict[str, Any]:
    if not _allowed(card_url):
        return {
            "name": name,
            "status": "blocked",
            "card_url": card_url,
            "recommendation": "Add the hostname to AGENT_PROBE_ALLOWED_HOSTS after reviewing its trust boundary.",
        }
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            response = await client.get(card_url)
            response.raise_for_status()
            card = response.json()
        if not isinstance(card, dict):
            raise ValueError("Agent Card must be a JSON object")
        available = _skills(card)
        missing = sorted(set(expected_skills or []) - available)
        identity = card.get("name") or card.get("agent_id")
        classification = classify_agent({"probe_name": name, **card})
        declared_status = str(card.get("status", "UP")).upper()
        status = "healthy" if identity and not missing and declared_status not in {"DEGRADED", "DOWN", "FAILED"} else "degraded"
        recommendation = None
        if not identity:
            recommendation = "Publish a stable name or agent_id in the Agent Card."
        elif missing:
            recommendation = f"Publish the missing declared skills: {', '.join(missing)}."
        elif status == "degraded":
            recommendation = card.get("recommendation") or f"Agent reports status {declared_status}."
        return {
            "name": name,
            "status": status,
            "card_url": card_url,
            "identity": identity,
            "version": card.get("version"),
            "skills": sorted(available),
            "archetype": classification["archetype"],
            "ontology_label": classification["label"],
            "ontology_product_name": classification["product_name"],
            "ontology_classification": classification,
            "missing_skills": missing,
            "recommendation": recommendation,
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "name": name,
            "status": "unhealthy",
            "card_url": card_url,
            "recommendation": f"Restore the endpoint and verify its Agent Card contract: {exc}",
        }


async def scan() -> dict[str, Any]:
    results = await asyncio.gather(
        *(
            probe_card(name, url, EXPECTED_INTERNAL_SKILLS.get(name, []))
            for name, url in configured_cards().items()
        )
    )
    healthy = sum(result["status"] == "healthy" for result in results)
    families = discover_fleet_patterns(results)
    model_provider = await probe_model_provider()
    return {
        "status": "healthy" if healthy == len(results) and model_provider.get("status") == "connected" else "degraded",
        "healthy": healthy,
        "total": len(results),
        "agents": results,
        "ontology_families": families,
        "model_provider": model_provider,
    }


async def probe_model_provider() -> dict[str, Any]:
    url = f"{os.getenv('TEST_GENERATION_URL', 'http://test_generation_agent:8001').rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            provider = response.json().get("llm_provider")
        if not isinstance(provider, dict):
            raise ValueError("test-generation health omitted llm_provider")
        return provider
    except (httpx.HTTPError, ValueError, AttributeError):
        return {"mode": "UNKNOWN", "configured": False, "status": "unavailable"}


def _base_graph(fleet: dict[str, Any]) -> dict[str, Any]:
    nodes = [
        {
            "id": f"agent:{agent['name']}",
            "name": agent.get("identity") or agent["name"],
            "type": "agent",
            "status": agent["status"],
            "version": agent.get("version"),
            "skills": agent.get("skills", []),
            "archetype": agent.get("archetype"),
            "ontology_label": agent.get("ontology_label"),
            "ontology_product_name": agent.get("ontology_product_name"),
        }
        for agent in fleet["agents"]
    ]
    nodes.extend(OBSERVED_AGENTS.values())
    nodes.extend(
        {
            "id": f"family:{family['id']}",
            "name": family.get("product_name") or family["label"],
            "type": "agent_family",
            "status": "review-required" if family["review_required"] else "classified",
            "archetype": family.get("archetype"),
            "ontology_label": family["label"],
            "ontology_product_name": family.get("product_name"),
            "skills": family["capabilities"],
        }
        for family in fleet.get("ontology_families", [])
    )
    nodes.extend(
        [
            {"id": "system:temporal", "name": "Temporal", "type": "orchestrator", "status": "configured"},
            {"id": "store:rustfs", "name": "RustFS", "type": "evidence", "status": "configured"},
            {"id": "store:postgres", "name": "PostgreSQL", "type": "state", "status": "configured"},
            {"id": "store:qdrant", "name": "Qdrant + ontology", "type": "knowledge", "status": "configured"},
            {"id": "catalog:github", "name": "Versioned test catalog", "type": "catalog", "status": "configured"},
        ]
    )
    edges = [
        {"source": "system:temporal", "target": "agent:test-generation", "type": "generate_tests"},
        {"source": "system:temporal", "target": "agent:github-analysis", "type": "inspect_source"},
        {"source": "agent:github-analysis", "target": "agent:test-generation", "type": "source_evidence"},
        {"source": "agent:test-generation", "target": "store:qdrant", "type": "retrieve_evidence"},
        {"source": "agent:test-generation", "target": "store:rustfs", "type": "write_candidate"},
        {"source": "agent:test-generation", "target": "agent:quality-oracle", "type": "request_grounded_review"},
        {"source": "agent:quality-oracle", "target": "agent:test-execution", "type": "approve_for_execution"},
        {"source": "system:temporal", "target": "agent:test-execution", "type": "dispatch_agent_test"},
        {"source": "system:temporal", "target": "agent:website-execution", "type": "dispatch_website_test"},
        {"source": "agent:test-execution", "target": "catalog:github", "type": "publish_passed_test"},
        {"source": "agent:website-execution", "target": "catalog:github", "type": "publish_passed_test"},
        {"source": "agent:test-execution", "target": "store:postgres", "type": "persist_result"},
        {"source": "agent:website-execution", "target": "store:postgres", "type": "persist_result"},
    ]
    agent_names_by_identity = {
        str(agent.get("identity") or agent["name"]): agent["name"] for agent in fleet["agents"]
    }
    for family in fleet.get("ontology_families", []):
        family_id = f"family:{family['id']}"
        edges.extend(
            {
                "source": family_id,
                "target": f"agent:{agent_names_by_identity[member]}",
                "type": "capability_member",
            }
            for member in family["members"]
            if member in agent_names_by_identity
        )
    # Events are stored newest-first. Apply oldest-first so the latest node
    # status wins when generation and execution update the same test node.
    for event in reversed(GRAPH_EVENTS):
        nodes.extend(event.get("nodes", []))
        edges.extend(event.get("edges", []))
    unique_nodes = {node["id"]: node for node in nodes}
    unique_edges = {
        (edge["source"], edge["target"], edge["type"]): edge
        for edge in edges
    }
    return {
        "nodes": list(unique_nodes.values()),
        "edges": list(unique_edges.values()),
        "events": list(GRAPH_EVENTS),
        "stats": {"node_count": len(unique_nodes), "edge_count": len(unique_edges)},
    }


app = FastAPI(title="AQE Diagnostics Agent", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin for origin in os.getenv("DIAGNOSTICS_CORS_ORIGINS", "http://localhost:3000").split(",") if origin],
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "agent": "aqe-diagnostics"}


@app.get("/agent_card")
@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    card_url = f"{PUBLIC_BASE_URL}/agent_card"
    return {
        "name": "aqe-diagnostics",
        "description": "Validates agent contracts and recommends safe recovery actions",
        "version": "2.0.0",
        "skills": [
            {"id": "diagnostics.scan", "description": "Validate configured internal agents", "examples": [{}], "invocation": {"protocol": "rest", "method": "GET", "url": f"{PUBLIC_BASE_URL}/v1/diagnostics"}},
            {"id": "diagnostics.probe", "description": "Contract-test a known internal or external agent", "examples": [{"name": "aqe-diagnostics", "card_url": card_url, "expected_skills": ["diagnostics.scan", "diagnostics.probe", "diagnostics.heal"]}], "invocation": {"protocol": "rest", "method": "POST", "url": f"{PUBLIC_BASE_URL}/v1/agent-probes"}},
            {"id": "diagnostics.heal", "description": "Retry checks and return safe remediation recommendations", "examples": [{}], "invocation": {"protocol": "rest", "method": "POST", "url": f"{PUBLIC_BASE_URL}/v1/diagnostics/heal"}},
        ],
        "evaluation": {"cases": [
            {"id": "scan-configured-fleet", "skill_id": "diagnostics.scan", "prompt": {}, "expected_response": {"status": "healthy or degraded", "healthy": "integer", "total": "integer", "agents": "array", "model_provider": "object"}, "max_latency_ms": 30000, "min_accuracy": 1.0},
            {"id": "probe-diagnostics-card", "skill_id": "diagnostics.probe", "prompt": {"name": "aqe-diagnostics", "card_url": card_url, "expected_skills": ["diagnostics.scan", "diagnostics.probe", "diagnostics.heal"]}, "expected_response": {"status": "healthy", "identity": "aqe-diagnostics", "missing_skills": []}, "max_latency_ms": 10000, "min_accuracy": 1.0},
            {"id": "retry-fleet-diagnostics-safely", "skill_id": "diagnostics.heal", "prompt": {}, "expected_response": {"status": "healthy or degraded", "action": "none or safe-retry", "agents": "array", "model_provider": "object"}, "max_latency_ms": 65000, "min_accuracy": 1.0},
        ]},
    }


@app.get("/v1/diagnostics")
async def diagnostics() -> dict[str, Any]:
    return await scan()


@app.get("/v1/graph")
async def graph() -> dict[str, Any]:
    """Return the live AQE topology using the platform knowledge-graph shape."""
    return _base_graph(await scan())


@app.get("/v1/evaluations/latest")
async def latest_evaluation() -> dict[str, Any]:
    return LATEST_EVALUATION or {"status": "not_evaluated"}


@app.post("/v1/evaluations", status_code=202)
async def record_evaluation(result: dict[str, Any]) -> dict[str, str]:
    required = ("passed", "total", "score", "semantic_score", "average_latency_ms")
    if any(not isinstance(result.get(field), (int, float)) for field in required):
        raise HTTPException(status_code=400, detail=f"numeric fields required: {', '.join(required)}")
    LATEST_EVALUATION.clear()
    LATEST_EVALUATION.update({**result, "recorded_at": datetime.now(timezone.utc).isoformat()})
    shard = str(result.get("shard_index", "all"))
    EVALUATION_SCORE.labels(shard).set(float(result["score"]))
    EVALUATION_SEMANTIC_SCORE.labels(shard).set(float(result["semantic_score"]))
    EVALUATION_AVERAGE_LATENCY.labels(shard).set(float(result["average_latency_ms"]))
    EVALUATION_CASES.labels(shard, "passed").set(float(result["passed"]))
    EVALUATION_CASES.labels(shard, "total").set(float(result["total"]))
    return {"status": "accepted"}


@app.post("/v1/graph/events", status_code=202)
async def graph_event(event: dict[str, Any]) -> dict[str, str]:
    kind = str(event.get("kind", ""))
    test_kinds = {"test_generated", "test_started", "test_completed"}
    workflow_kinds = {
        "workflow_started",
        "agent_discovery_started",
        "source_analysis_started",
        "test_generation_started",
        "oracle_review_started",
        "test_execution_started",
        "workflow_completed",
    }
    reference_id = event.get("task_id") or event.get("workflow_id")
    if kind not in test_kinds | workflow_kinds or not reference_id:
        raise HTTPException(
            status_code=400,
            detail="a supported event kind and task_id or workflow_id are required",
        )
    if kind in workflow_kinds:
        workflow_id = str(event.get("workflow_id") or reference_id)
        test_type = str(event.get("test_type", "agent"))
        executor = "website-execution" if test_type == "website" else "test-execution"
        routes = {
            "workflow_started": ("system:temporal", "agent:diagnostics", "campaign_started"),
            "agent_discovery_started": ("system:temporal", "agent:diagnostics", "discover_agent"),
            "source_analysis_started": ("system:temporal", "agent:github-analysis", "inspect_source"),
            "test_generation_started": ("system:temporal", "agent:test-generation", "generate_tests"),
            "oracle_review_started": ("agent:test-generation", "agent:quality-oracle", "request_grounded_review"),
            "test_execution_started": ("agent:quality-oracle", f"agent:{executor}", "approve_for_execution"),
            "workflow_completed": (f"agent:{executor}", "catalog:github", "publish_passed_test"),
        }
        source, target_id, interaction = routes[kind]
        status = str(event.get("status") or ("completed" if kind == "workflow_completed" else "running"))
        workflow_node = {
            "id": f"workflow:{workflow_id}",
            "name": workflow_id,
            "type": "workflow",
            "status": status,
            "test_type": test_type,
            "summary": event.get("summary") or {},
        }
        edges = [{"source": source, "target": target_id, "type": interaction}]
        recorded_at = datetime.now(timezone.utc).isoformat()
        GRAPH_EVENTS.appendleft(
            {
                "id": f"{kind}:{workflow_id}:{recorded_at}",
                "kind": kind,
                "timestamp": recorded_at,
                "status": status,
                "workflow_id": workflow_id,
                "task_id": event.get("task_id"),
                "test_type": test_type,
                "summary": event.get("summary") or {},
                "nodes": [workflow_node],
                "edges": edges,
            }
        )
        return {"status": "accepted", "event_id": f"{kind}:{workflow_id}"}

    task_id = str(event["task_id"])
    test_type = str(event.get("test_type", "agent"))
    target = event.get("target_agent") or {}
    status = {
        "test_generated": "queued",
        "test_started": "running",
        "test_completed": str(event.get("status", "completed")),
    }[kind]
    test_node = {
        "id": f"test:{task_id}",
        "name": f"{test_type} test {task_id[:8]}",
        "type": "generated_test",
        "status": status,
        "test_type": test_type,
        "archetype": event.get("agent_archetype"),
        "summary": event.get("summary") or {},
    }
    executor = "website-execution" if test_type == "website" else "test-execution"
    if kind == "test_generated":
        edges = [
            {"source": "agent:test-generation", "target": test_node["id"], "type": "generated"},
            {"source": test_node["id"], "target": f"agent:{executor}", "type": "routed_to"},
        ]
    else:
        edges = [
            {
                "source": f"agent:{executor}",
                "target": test_node["id"],
                "type": "executing" if kind == "test_started" else status,
            }
        ]
    if target.get("id"):
        target_id = f"target:{target['id']}"
        OBSERVED_AGENTS[target_id] = {
            "id": target_id,
            "name": str(target["id"]),
            "type": "target_agent",
            "status": "observed",
            "version": target.get("version"),
            "archetype": event.get("agent_archetype"),
        }
        edges.append({"source": test_node["id"], "target": target_id, "type": "validates"})
    GRAPH_EVENTS.appendleft(
        {
            "id": f"{kind}:{task_id}",
            "kind": kind,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "task_id": task_id,
            "test_type": test_type,
            "summary": event.get("summary") or {},
            "nodes": [test_node],
            "edges": edges,
        }
    )
    return {"status": "accepted", "event_id": f"{kind}:{task_id}"}


@app.post("/v1/diagnostics/heal")
async def heal() -> dict[str, Any]:
    first = await scan()
    if first["status"] == "healthy":
        return {**first, "action": "none"}
    await asyncio.sleep(1)
    retried = await scan()
    return {**retried, "action": "safe-retry", "previous_status": first["status"]}


@app.post("/v1/agent-probes")
async def agent_probe(request: dict[str, Any]) -> dict[str, Any]:
    card_url = request.get("card_url")
    if not isinstance(card_url, str) or not card_url:
        raise HTTPException(status_code=400, detail="card_url is required")
    expected = request.get("expected_skills", [])
    if not isinstance(expected, list) or not all(isinstance(skill, str) for skill in expected):
        raise HTTPException(status_code=400, detail="expected_skills must be a list of strings")
    result = await probe_card(str(request.get("name", "external-agent")), card_url, expected)
    if result.get("identity"):
        observed_id = f"target:{result['identity']}"
        OBSERVED_AGENTS[observed_id] = {
            "id": observed_id,
            "name": result["identity"],
            "type": "target_agent",
            "status": result["status"],
            "version": result.get("version"),
            "skills": result.get("skills", []),
            "archetype": result.get("archetype"),
            "ontology_label": result.get("ontology_label"),
            "ontology_product_name": result.get("ontology_product_name"),
        }
    invocation = request.get("invocation")
    if invocation is None or result["status"] != "healthy":
        return result
    if not isinstance(invocation, dict) or not isinstance(invocation.get("url"), str):
        raise HTTPException(status_code=400, detail="invocation.url is required")
    if not _allowed(invocation["url"]):
        raise HTTPException(status_code=403, detail="invocation host is not allowed")
    payload = {
        "jsonrpc": "2.0",
        "id": str(invocation.get("id", "aqe-diagnostic-probe")),
        "method": str(invocation.get("method", "tasks.execute")),
        "params": invocation.get("params", {}),
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.post(invocation["url"], json=payload)
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {**result, "status": "unhealthy", "scenario": {"passed": False, "error": str(exc)}}
    passed = response.is_success and isinstance(body, dict) and "error" not in body
    return {
        **result,
        "status": "healthy" if passed else "degraded",
        "scenario": {
            "passed": passed,
            "http_status": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "response": body,
        },
    }


@app.post("/v1/agent-discovery")
async def agent_discovery(request: dict[str, Any]) -> dict[str, Any]:
    card_urls = request.get("card_urls") or []
    if not isinstance(card_urls, list) or not all(isinstance(url, str) for url in card_urls):
        raise HTTPException(status_code=400, detail="card_urls must be a list of URLs")
    if not card_urls:
        card_urls = list(configured_cards().values())
    return await discover_agents(
        card_urls,
        max_depth=min(max(int(request.get("max_depth", 2)), 0), 4),
        max_agents=min(max(int(request.get("max_agents", 50)), 1), 100),
        default_max_latency_ms=int(request.get("max_latency_ms", 5000)),
        default_min_accuracy=float(request.get("min_accuracy", 0.8)),
    )


app = PrometheusMiddleware(app, "diagnostics")
