from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import FastAPI
from starlette.responses import PlainTextResponse

logger = logging.getLogger("aqe.byoa")

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
REGISTRY_URL = os.getenv("REGISTRY_URL", "")
AGENT_ENDPOINT = os.getenv("AGENT_ENDPOINT", "http://aqe_byoa:8009")
EXECUTION_URL = os.getenv("TEST_EXECUTION_URL", "http://test_execution_agent:8003/run_tests")
WORKFLOW_URL = os.getenv("AQE_WORKFLOW_URL", "http://workflow_api:8008/v1/qe-runs")
TASK_TOPIC = "tasks.aqe"
RESULT_TOPIC = "results.aqe"

CARD = {
    "name": "aqe",
    "description": "Compound quality-engineering system for test generation, execution, and repair",
    "endpoint": AGENT_ENDPOINT,
    "task_topic": TASK_TOPIC,
    "result_topic": RESULT_TOPIC,
    "version": "2.0.0",
    "skills": [
        {"id": "qe.run", "description": "Start a durable end-to-end quality-engineering run", "invocation": {"protocol": "jsonrpc-kafka", "method": "qe.run", "url": f"kafka://{KAFKA_BOOTSTRAP_SERVERS}/{TASK_TOPIC}", "topic": TASK_TOPIC}},
        {"id": "qe.validate", "description": "Execute a persisted test run in the AQE sandbox", "invocation": {"protocol": "jsonrpc-kafka", "method": "qe.validate", "url": f"kafka://{KAFKA_BOOTSTRAP_SERVERS}/{TASK_TOPIC}", "topic": TASK_TOPIC}},
        {"id": "qe.repair", "description": "Repair and re-run a failing persisted test run", "invocation": {"protocol": "jsonrpc-kafka", "method": "qe.repair", "url": f"kafka://{KAFKA_BOOTSTRAP_SERVERS}/{TASK_TOPIC}", "topic": TASK_TOPIC}},
    ],
    "evaluation": {"cases": [
        {"id": "run-safe-candidate", "skill_id": "qe.run", "prompt": {"target_evidence": {"agent_card_url": "https://candidate.invalid/.well-known/agent.json", "skills": ["candidate.echo"], "expected_behavior": "echoes supplied text"}, "candidate_only": True, "production_mutation": False}, "expected_response": {"status": "accepted", "workflow_id": {"prefix": "qe-"}}, "required_dimensions": ["semantic_accuracy", "explicit_target_evidence", "no_production_mutation"]},
        {"id": "validate-persisted-candidate", "skill_id": "qe.validate", "prompt": {"task_id": "from disposable execution fixture"}, "expected_response": {"successful": True, "summary": {"passed": {"min": 1}}}, "required_dimensions": ["semantic_accuracy", "sandbox_execution"]},
        {"id": "repair-persisted-candidate", "skill_id": "qe.repair", "prompt": {"task_id": "from disposable execution fixture"}, "expected_response": {"attempts": {"min_items": 1}}, "required_dimensions": ["semantic_accuracy", "bounded_repair"]},
    ]},
}

processed_tasks = 0
failed_tasks = 0


def _normalize(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    request_id = str(payload.get("id", "unknown"))
    method = str(payload.get("method", ""))
    params = payload.get("params") or {}
    if method == "tasks.execute" and isinstance(params, dict):
        method = str(params.get("skill", ""))
        context = params.get("context") or {}
        params = context if isinstance(context, dict) else {}
    if not isinstance(params, dict):
        params = {}
    return request_id, method, params


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    request_id, method, params = _normalize(payload)
    if method == "qe.run":
        evidence = params.get("target_evidence")
        if not isinstance(evidence, dict) or not evidence.get("agent_card_url") or not evidence.get("skills") or not evidence.get("expected_behavior"):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "qe.run requires target_evidence with agent_card_url, skills, and expected_behavior"}}
        if params.get("candidate_only") is not True or params.get("production_mutation") is not False:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "qe.run requires candidate_only=true and production_mutation=false"}}
        url, body = WORKFLOW_URL, {
            **params,
            "url": evidence["agent_card_url"],
            "test_type": "agent",
            "spec": evidence["expected_behavior"],
            "candidate_only": True,
            "production_mutation": False,
        }
    elif method in {"qe.validate", "qe.repair"}:
        url = EXECUTION_URL
        body = {"task_id": params.get("task_id"), "repair": method == "qe.repair"}
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Unknown AQE skill: {method}"},
        }

    async with httpx.AsyncClient(timeout=30 if method == "qe.run" else 900) as client:
        response = await client.post(url, json=body)
        result = response.json()
    if response.status_code >= 500:
        raise RuntimeError(f"AQE service failed with HTTP {response.status_code}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def consume() -> None:
    global failed_tasks, processed_tasks
    consumer = AIOKafkaConsumer(
        TASK_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="aqe-agent",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda value: json.loads(value.decode()),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        acks="all",
        enable_idempotence=True,
        value_serializer=lambda value: json.dumps(value).encode(),
    )
    await producer.start()
    await consumer.start()
    try:
        async for message in consumer:
            try:
                result = await handle(message.value)
                processed_tasks += 1
            except Exception as exc:
                logger.exception("AQE task failed")
                failed_tasks += 1
                result = {
                    "jsonrpc": "2.0",
                    "id": str(message.value.get("id", "unknown")),
                    "error": {"code": -32000, "message": str(exc)},
                }
            await producer.send_and_wait(RESULT_TOPIC, result)
            await consumer.commit()
    finally:
        await consumer.stop()
        await producer.stop()


async def register_forever() -> None:
    if not REGISTRY_URL:
        return
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                await client.post(f"{REGISTRY_URL.rstrip('/')}/registry/register", json=CARD)
            except httpx.HTTPError:
                logger.warning("Registry registration failed; retrying", exc_info=True)
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(consume()), asyncio.create_task(register_forever())]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="AQE BYOA Adapter", version="2.0.0", lifespan=lifespan)


@app.get("/.well-known/agent.json")
async def discovery() -> dict[str, Any]:
    return CARD


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "agent": "aqe"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> PlainTextResponse:
    body = (
        "# TYPE aqe_tasks_processed_total counter\n"
        f"aqe_tasks_processed_total {processed_tasks}\n"
        "# TYPE aqe_tasks_failed_total counter\n"
        f"aqe_tasks_failed_total {failed_tasks}\n"
    )
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
