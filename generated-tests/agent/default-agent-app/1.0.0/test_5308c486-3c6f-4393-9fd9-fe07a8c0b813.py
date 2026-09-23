import os
import pytest
import httpx
import json
import time
import uuid
import re

AGENT_CARD_URL = os.getenv("AGENT_CARD_URL", "http://aqe-artifact-management:8007/agent_card")
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://aqe-artifact-management:8007")
TIMEOUT = 10.0

@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=TIMEOUT) as c:
        yield c

@pytest.fixture(scope="module")
def agent_card(client):
    response = client.get(AGENT_CARD_URL)
    assert response.status_code == 200, f"Agent Card endpoint returned {response.status_code}"
    return response.json()

def test_agent_card_reachable(client):
    response = client.get(AGENT_CARD_URL)
    assert response.status_code == 200

def test_agent_card_identity(agent_card):
    assert agent_card.get("agent_id") == "ArtifactManagementAgent"

def test_agent_card_version(agent_card):
    assert agent_card.get("version") == "1.0.0"

def test_agent_card_status(agent_card):
    assert agent_card.get("status") == "UP"

def test_agent_card_declared_skill_present(agent_card):
    skills = agent_card.get("skills", [])
    skill_ids = [s.get("id") for s in skills if isinstance(s, dict)]
    assert "upload_artifact" in skill_ids