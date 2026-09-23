import asyncio
import json

from agents.github_commit import app as github_commit


def agent_card() -> dict:
    response = asyncio.run(github_commit.agent_card_endpoint(None))
    return json.loads(response.body)


def test_agent_card_declares_executable_consumer_contract():
    skill = agent_card()["skills"][0]

    assert skill["invocation"]["protocol"] == "a2a_jsonrpc"


def test_agent_card_declares_idempotency_and_cancellation_evaluation():
    evaluation = agent_card()["evaluation"]["cases"][0]

    assert evaluation["required_dimensions"] == ["idempotency", "cancellation"]
