import httpx
import pytest
from utils.target_auth import authenticated_client

AGENT_CARD_URL = "http://aqe-diagnostics:8006/agent_card"


@pytest.fixture(scope="module")
def client():
    with authenticated_client() as c:
        yield c


@pytest.fixture(scope="module")
def agent_card(client):
    response = client.get(AGENT_CARD_URL)
    assert response.status_code == 200
    return response.json()


def test_agent_card_returns_200(client):
    response = client.get(AGENT_CARD_URL)
    assert response.status_code == 200


def test_agent_card_name_is_aqe_diagnostics(agent_card):
    assert agent_card["name"] == "aqe-diagnostics"


def test_agent_card_skills_include_diagnostics_scan(agent_card):
    skill_ids = [skill["id"] for skill in agent_card["skills"]]
    assert "diagnostics.scan" in skill_ids