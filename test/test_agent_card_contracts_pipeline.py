import asyncio

import httpx

from agents.quality_oracle import app as oracle
from agents.test_execution import app as execution
from byoa_adapter import app as byoa


def test_oracle_card_contract_uses_persisted_candidate_fixture(monkeypatch):
    writes = {}

    class Store:
        def ensure_bucket(self):
            return None

        def write_bytes(self, path, payload, content_type):
            writes[path] = (payload, content_type)

    monkeypatch.setattr(oracle, "ObjectStore", Store)
    fixture = asyncio.run(oracle.candidate_fixture())
    card = asyncio.run(oracle.agent_card())
    case = card["evaluation"]["cases"][0]

    assert fixture["task_id"].startswith("aqe-candidate-smoke-")
    assert fixture["object_path"] in writes
    assert case["fixture"]["url"].endswith("/v1/fixtures/candidate")
    assert card["skills"][0]["invocation"]["url"].endswith("/v1/reviews")
    assert case["expected_response"]["status"] == "APPROVED"


def test_execution_fixture_persists_mode_metadata_and_card_is_executable(monkeypatch):
    writes, calls = {}, []

    class Store:
        def ensure_bucket(self):
            return None

        def write_bytes(self, path, payload, content_type):
            writes[path] = payload

    class Connection:
        async def execute(self, sql, *args):
            calls.append((sql, args))

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    monkeypatch.setattr(execution.LOGIC, "object_store", Store())

    async def pool():
        return Pool()

    monkeypatch.setattr(execution.LOGIC, "_pool", pool)

    async def exercise():
        app = execution.build_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            fixture = await client.post("/v1/fixtures/candidate", json={"outcome": "pass"})
            card = (await client.get("/agent_card")).json()
        return fixture, card

    fixture, card = asyncio.run(exercise())
    assert fixture.status_code == 200
    assert fixture.json()["test_type"] == execution.TEST_EXECUTION_MODE
    assert fixture.json()["object_path"] in writes
    assert calls[0][1][-1] == execution.TEST_EXECUTION_MODE
    assert {skill["id"] for skill in card["skills"]} == {"qe.validate", "qe.repair"}
    assert all(case["id"].endswith("candidate") for case in card["evaluation"]["cases"])


def test_byoa_run_requires_safe_explicit_evidence_and_forwards_it(monkeypatch):
    captured = {}

    class Response:
        status_code = 201

        def json(self):
            return {"workflow_id": "qe-candidate", "status": "accepted"}

    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json):
            captured.update(url=url, body=json)
            return Response()

    monkeypatch.setattr(byoa.httpx, "AsyncClient", Client)
    unsafe = asyncio.run(byoa.handle({"id": "1", "method": "qe.run", "params": {}}))
    safe_params = {
        "target_evidence": {
            "agent_card_url": "https://candidate.invalid/card",
            "skills": ["candidate.echo"],
            "expected_behavior": "echo supplied text",
        },
        "candidate_only": True,
        "production_mutation": False,
    }
    safe = asyncio.run(byoa.handle({"id": "2", "method": "qe.run", "params": safe_params}))

    assert unsafe["error"]["code"] == -32602
    assert safe["result"]["status"] == "accepted"
    assert captured["body"]["url"] == safe_params["target_evidence"]["agent_card_url"]
    assert captured["body"]["spec"] == safe_params["target_evidence"]["expected_behavior"]
    assert captured["body"]["production_mutation"] is False
    assert all(skill["invocation"]["url"].startswith("kafka://") for skill in byoa.CARD["skills"])
