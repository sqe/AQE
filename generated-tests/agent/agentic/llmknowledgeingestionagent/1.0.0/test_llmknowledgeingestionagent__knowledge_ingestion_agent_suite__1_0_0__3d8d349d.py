import os
import time
import httpx
import pytest

AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "knowledge-ingestion-agent-suite"

BASE_URL = os.getenv("AGENT_BASE_URL", "http://aqe-knowledge-ingestion:8004")
AGENT_CARD_URL = os.getenv("AGENT_CARD_URL", f"{BASE_URL}/agent_card")

KNOWLEDGE_INVOCATION_URL = f"{BASE_URL}/v1/candidate-smoke/knowledge"
ONTOLOGY_INVOCATION_URL = f"{BASE_URL}/v1/candidate-smoke/ontology"

AQE_SKILL_TESTS = {
    "ingest_knowledge": {
        "positive": ["test_ingest_knowledge_positive"],
        "protocol_schema": ["test_ingest_knowledge_protocol_schema_status", "test_ingest_knowledge_protocol_schema_candidate"],
        "state_isolation": ["test_ingest_knowledge_state_isolation_cleaned", "test_ingest_knowledge_state_isolation_id"],
        "latency": ["test_ingest_knowledge_latency"],
        "semantic_accuracy": [
            "test_ingest_knowledge_semantic_accuracy_status_code",
            "test_ingest_knowledge_semantic_accuracy_status",
            "test_ingest_knowledge_semantic_accuracy_candidate_id",
            "test_ingest_knowledge_semantic_accuracy_cleaned_up"
        ],
    },
    "ingest_ontology": {
        "positive": ["test_ingest_ontology_positive"],
        "protocol_schema": ["test_ingest_ontology_protocol_schema_status", "test_ingest_ontology_protocol_schema_candidate"],
        "state_isolation": ["test_ingest_ontology_state_isolation_cleaned", "test_ingest_ontology_state_isolation_id"],
        "latency": ["test_ingest_ontology_latency"],
        "semantic_accuracy": [
            "test_ingest_ontology_semantic_accuracy_status_code",
            "test_ingest_ontology_semantic_accuracy_status",
            "test_ingest_ontology_semantic_accuracy_candidate_id",
            "test_ingest_ontology_semantic_accuracy_cleaned_up"
        ],
    },
}

@pytest.fixture
def api_client():
    with httpx.Client(timeout=15.0) as client:
        yield client

@pytest.fixture
def knowledge_response(api_client):
    payload = {"candidate_id": "aqe-candidate-smoke-knowledge"}
    return api_client.post(KNOWLEDGE_INVOCATION_URL, json=payload)

@pytest.fixture
def ontology_response(api_client):
    payload = {"candidate_id": "aqe-candidate-smoke-knowledge"}
    return api_client.post(ONTOLOGY_INVOCATION_URL, json=payload)

@pytest.fixture
def agent_card_response(api_client):
    return api_client.get(AGENT_CARD_URL)


# Agent Card Validation Atomic Tests (Single Assertion Each)

def test_agent_card_status_code(agent_card_response):
    assert agent_card_response.status_code == 200

def test_agent_card_id(agent_card_response):
    data = agent_card_response.json()
    assert data.get("agent_id") == "LLMKnowledgeIngestionAgent"

def test_agent_card_version(agent_card_response):
    data = agent_card_response.json()
    assert data.get("version") == "1.0.0"

def test_agent_card_skills(agent_card_response):
    data = agent_card_response.json()
    skill_ids = [s.get("id") for s in data.get("skills", [])]
    assert "ingest_knowledge" in skill_ids and "ingest_ontology" in skill_ids


# Ingest Knowledge Atomic Tests

def test_ingest_knowledge_positive(api_client):
    payload = {"candidate_id": "aqe-candidate-smoke-knowledge"}
    response = api_client.post(KNOWLEDGE_INVOCATION_URL, json=payload)
    assert response.status_code == 200

def test_ingest_knowledge_protocol_schema_status(knowledge_response):
    data = knowledge_response.json()
    assert "status" in data

def test_ingest_knowledge_protocol_schema_candidate(knowledge_response):
    data = knowledge_response.json()
    assert "candidate_id" in data

def test_ingest_knowledge_state_isolation_cleaned(knowledge_response):
    data = knowledge_response.json()
    assert data.get("cleaned_up") is True

def test_ingest_knowledge_state_isolation_id(knowledge_response):
    data = knowledge_response.json()
    assert data.get("candidate_id") == "aqe-candidate-smoke-knowledge"

def test_ingest_knowledge_latency(api_client):
    payload = {"candidate_id": "aqe-candidate-smoke-knowledge"}
    start = time.perf_counter()
    api_client.post(KNOWLEDGE_INVOCATION_URL, json=payload)
    duration_ms = (time.perf_counter() - start) * 1000
    assert duration_ms <= 10000

def test_ingest_knowledge_semantic_accuracy_status_code(knowledge_response):
    assert knowledge_response.status_code == 200

def test_ingest_knowledge_semantic_accuracy_status(knowledge_response):
    data = knowledge_response.json()
    assert data.get("status") == "SUCCESS"

def test_ingest_knowledge_semantic_accuracy_candidate_id(knowledge_response):
    data = knowledge_response.json()
    assert data.get("candidate_id") == "aqe-candidate-smoke-knowledge"

def test_ingest_knowledge_semantic_accuracy_cleaned_up(knowledge_response):
    data = knowledge_response.json()
    assert data.get("cleaned_up") is True


# Ingest Ontology Atomic Tests

def test_ingest_ontology_positive(api_client):
    payload = {"candidate_id": "aqe-candidate-smoke-knowledge"}
    response = api_client.post(ONTOLOGY_INVOCATION_URL, json=payload)
    assert response.status_code == 200

def test_ingest_ontology_protocol_schema_status(ontology_response):
    data = ontology_response.json()
    assert "status" in data

def test_ingest_ontology_protocol_schema_candidate(ontology_response):
    data = ontology_response.json()
    assert "candidate_id" in data

def test_ingest_ontology_state_isolation_cleaned(ontology_response):
    data = ontology_response.json()
    assert data.get("cleaned_up") is True

def test_ingest_ontology_state_isolation_id(ontology_response):
    data = ontology_response.json()
    assert data.get("candidate_id") == "aqe-candidate-smoke-knowledge"

def test_ingest_ontology_latency(api_client):
    payload = {"candidate_id": "aqe-candidate-smoke-knowledge"}
    start = time.perf_counter()
    api_client.post(ONTOLOGY_INVOCATION_URL, json=payload)
    duration_ms = (time.perf_counter() - start) * 1000
    assert duration_ms <= 10000

def test_ingest_ontology_semantic_accuracy_status_code(ontology_response):
    assert ontology_response.status_code == 200

def test_ingest_ontology_semantic_accuracy_status(ontology_response):
    data = ontology_response.json()
    assert data.get("status") == "SUCCESS"

def test_ingest_ontology_semantic_accuracy_candidate_id(ontology_response):
    data = ontology_response.json()
    assert data.get("candidate_id") == "aqe-candidate-smoke-knowledge"

def test_ingest_ontology_semantic_accuracy_cleaned_up(ontology_response):
    data = ontology_response.json()
    assert data.get("cleaned_up") is True