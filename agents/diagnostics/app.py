"""Diagnostics, self-healing recommendations, and fleet-contract probes."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from observability.metrics import PrometheusMiddleware


DEFAULT_AGENT_CARDS = {
    "artifact-management": "http://artifact_management_agent:8007/agent_card",
    "change-detection": "http://change_detection_agent:8000/agent_card",
    "test-generation": "http://test_generation_agent:8001/agent_card",
    "github-analysis": "http://github_analysis_agent:8010/agent_card",
    "github-commit": "http://github_commit_agent:8011/agent_card",
    "webpage-state-capture": "http://webpage_state_capture_agent:8002/agent_card",
    "test-execution": "http://test_execution_agent:8003/agent_card",
    "website-execution": "http://website_test_execution_agent:8013/agent_card",
    "knowledge-ingestion": "http://llm_fine_tuning_agent:8004/agent_card",
    "aqe-byoa": "http://aqe_byoa:8009/.well-known/agent.json",
}
EXPECTED_INTERNAL_SKILLS = {
    "artifact-management": ["upload_artifact"],
    "change-detection": ["detect_change_and_orchestrate"],
    "diagnostics": ["diagnostics.scan", "diagnostics.probe", "diagnostics.heal"],
    "github-analysis": ["source.inspect"],
    "github-commit": ["start_commit_consumer"],
    "knowledge-ingestion": ["ingest_knowledge", "ingest_ontology"],
    "test-generation": ["generate_tests"],
    "test-execution": ["qe.validate", "qe.repair"],
    "webpage-state-capture": ["capture_state"],
    "website-execution": ["qe.validate", "qe.repair"],
    "aqe-byoa": ["qe.run", "qe.validate", "qe.repair"],
}
GRAPH_EVENTS: deque[dict[str, Any]] = deque(maxlen=100)
OBSERVED_AGENTS: dict[str, dict[str, Any]] = {}


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
    return "*" in allowed or parsed.hostname in allowed


def _skills(card: dict[str, Any]) -> set[str]:
    return {
        str(skill.get("id"))
        for skill in card.get("skills", [])
        if isinstance(skill, dict) and skill.get("id")
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
        ontology = card.get("ontology")
        status = "healthy" if identity and not missing else "degraded"
        recommendation = None
        if not identity:
            recommendation = "Publish a stable name or agent_id in the Agent Card."
        elif missing:
            recommendation = f"Publish the missing declared skills: {', '.join(missing)}."
        return {
            "name": name,
            "status": status,
            "card_url": card_url,
            "identity": identity,
            "version": card.get("version"),
            "skills": sorted(available),
            "archetype": card.get("archetype") or (ontology.get("archetype") if isinstance(ontology, dict) else None),
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
    return {
        "status": "healthy" if healthy == len(results) else "degraded",
        "healthy": healthy,
        "total": len(results),
        "agents": results,
    }


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
        }
        for agent in fleet["agents"]
    ]
    nodes.extend(OBSERVED_AGENTS.values())
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
        {"source": "system:temporal", "target": "agent:test-execution", "type": "dispatch_agent_test"},
        {"source": "system:temporal", "target": "agent:website-execution", "type": "dispatch_website_test"},
        {"source": "agent:test-execution", "target": "catalog:github", "type": "publish_passed_test"},
        {"source": "agent:website-execution", "target": "catalog:github", "type": "publish_passed_test"},
        {"source": "agent:test-execution", "target": "store:postgres", "type": "persist_result"},
        {"source": "agent:website-execution", "target": "store:postgres", "type": "persist_result"},
    ]
    for event in GRAPH_EVENTS:
        nodes.extend(event.get("nodes", []))
        edges.extend(event.get("edges", []))
    unique_nodes = {node["id"]: node for node in nodes}
    return {
        "nodes": list(unique_nodes.values()),
        "edges": edges,
        "events": list(GRAPH_EVENTS),
        "stats": {"node_count": len(unique_nodes), "edge_count": len(edges)},
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
    return {
        "name": "aqe-diagnostics",
        "description": "Validates agent contracts and recommends safe recovery actions",
        "version": "2.0.0",
        "skills": [
            {"id": "diagnostics.scan", "description": "Validate configured internal agents"},
            {"id": "diagnostics.probe", "description": "Contract-test a known internal or external agent"},
            {"id": "diagnostics.heal", "description": "Retry checks and return safe remediation recommendations"},
        ],
    }


@app.get("/v1/diagnostics")
async def diagnostics() -> dict[str, Any]:
    return await scan()


@app.get("/v1/graph")
async def graph() -> dict[str, Any]:
    """Return the live AQE topology using the platform knowledge-graph shape."""
    return _base_graph(await scan())


@app.post("/v1/graph/events", status_code=202)
async def graph_event(event: dict[str, Any]) -> dict[str, str]:
    if event.get("kind") != "test_generated" or not event.get("task_id"):
        raise HTTPException(status_code=400, detail="kind=test_generated and task_id are required")
    task_id = str(event["task_id"])
    test_type = str(event.get("test_type", "agent"))
    target = event.get("target_agent") or {}
    test_node = {
        "id": f"test:{task_id}",
        "name": f"{test_type} test {task_id[:8]}",
        "type": "generated_test",
        "status": "pending",
        "test_type": test_type,
        "archetype": event.get("agent_archetype"),
    }
    executor = "website-execution" if test_type == "website" else "agent-execution"
    edges = [
        {"source": "agent:test-generation", "target": test_node["id"], "type": "generated"},
        {"source": test_node["id"], "target": f"agent:{executor}", "type": "routed_to"},
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
            "id": f"generation:{task_id}",
            "kind": "test_generated",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nodes": [test_node],
            "edges": edges,
        }
    )
    return {"status": "accepted", "event_id": f"generation:{task_id}"}


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
        "scenario": {"passed": passed, "http_status": response.status_code, "response": body},
    }


app = PrometheusMiddleware(app, "diagnostics")
