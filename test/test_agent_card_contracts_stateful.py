import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agents.artifact_management import app as artifact
from agents.knowledge_ingestion import app as knowledge

if "redis" not in sys.modules:
    redis_package = types.ModuleType("redis")
    redis_asyncio = types.ModuleType("redis.asyncio")
    redis_asyncio.Redis = object
    redis_package.asyncio = redis_asyncio
    sys.modules["redis"] = redis_package
    sys.modules["redis.asyncio"] = redis_asyncio
if "aiokafka" not in sys.modules:
    aiokafka = types.ModuleType("aiokafka")
    aiokafka.AIOKafkaConsumer = object
    aiokafka.AIOKafkaProducer = object
    sys.modules["aiokafka"] = aiokafka

from agents.change_detection import app as change


def _card(endpoint):
    return json.loads(asyncio.run(endpoint(None)).body)


def test_stateful_cards_have_executable_case_for_every_advertised_skill():
    for endpoint in (
        artifact.agent_card_endpoint,
        knowledge.agent_card_endpoint,
        change.agent_card_endpoint,
    ):
        card = _card(endpoint)
        skills = {item["id"] for item in card["skills"]}
        cases = card["evaluation"]["cases"]
        assert {case["skill_id"] for case in cases} == skills
        assert all(item["invocation"]["url"] for item in card["skills"])
        assert all(case["prompt"]["candidate_id"].startswith("aqe-candidate-smoke") for case in cases)


def test_candidate_knowledge_uses_and_deletes_isolated_collection():
    calls = []

    class Qdrant:
        def create_collection(self, **kwargs):
            calls.append(("create", kwargs["collection_name"]))

        def upsert(self, **kwargs):
            calls.append(("upsert", kwargs["collection_name"]))

        def count(self, **kwargs):
            return SimpleNamespace(count=1)

        def delete_collection(self, **kwargs):
            calls.append(("delete", kwargs["collection_name"]))

    logic = object.__new__(knowledge.LLMFineTuningAgentLogic)
    logic.qdrant_client = Qdrant()
    result = asyncio.run(logic.ingest_candidate(
        "aqe-candidate-smoke-test", [{"id": "aqe-candidate-smoke-record", "text": "safe fixture"}]
    ))

    assert result["status"] == "SUCCESS"
    assert calls[0][1].startswith("aqe-candidate-smoke-test-")
    assert calls[-1] == ("delete", calls[0][1])


def test_change_candidate_mode_executes_pipeline_without_infrastructure_publication():
    logic = change.ChangeDetectionAgentLogic("http://fixture", "spec", "owner", "repo")
    logic._ensure_clients_ready = AsyncMock(side_effect=AssertionError("Kafka must not initialize"))
    logic.capture_client.call = AsyncMock(return_value={"elements": []})
    logic.generation_client.call = AsyncMock(return_value={"test_code": "pass", "artifact_version_used": "fixture"})
    logic.execution_client.call = AsyncMock(return_value={"successful": True})

    result = asyncio.run(logic.detect_and_update(True, "aqe-candidate-smoke-change"))

    assert result["status"] == "SUCCESS"
    assert result["dry_run"] is True
    assert result["commit_published"] is False
    assert result["result_event_published"] is False
    logic._ensure_clients_ready.assert_not_awaited()
