import asyncio
import json

import pytest

from agents.quality_oracle import app as oracle


class _ProviderResponse:
    headers = {}

    def __init__(self, status_code):
        self.status_code = status_code


class _TransientProviderClient:
    def __init__(self):
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        return _ProviderResponse(503 if self.calls < 3 else 200)


async def _completed_sleep(_delay):
    return None


def test_oracle_retries_transient_provider_failures(monkeypatch):
    client = _TransientProviderClient()
    monkeypatch.setattr(oracle, "MAX_ATTEMPTS", 3)
    monkeypatch.setattr(oracle.asyncio, "sleep", _completed_sleep)

    response = asyncio.run(oracle._post_with_retries(client, "https://model.invalid"))

    assert (response.status_code, client.calls) == (200, 3)


def test_high_impact_policy_includes_health_and_safety_critical():
    assert oracle.is_high_impact({"data_sensitivity": "health", "impact": "safety_critical"}) is True


def test_healthcare_archetype_requires_high_impact_review():
    assert oracle.is_high_impact({"agent_archetype": "healthcare"}) is True


def test_standard_domain_is_not_mislabeled_high_impact():
    assert oracle.is_high_impact({"data_sensitivity": "internal", "impact": "moderate"}) is False


def test_review_parser_requires_structured_evidence_lists():
    response = oracle.parse_review(
        json.dumps(
            {
                "decision": "APPROVED",
                "reasoning_summary": "Every declared skill and oracle is represented.",
                "issues": [],
                "missing_coverage": [],
                "evidence_citations": ["requirement:login-1"],
            }
        )
    )

    assert response["decision"] == "APPROVED"


def test_review_parser_rejects_approval_with_missing_coverage():
    with pytest.raises(ValueError, match="APPROVED requires"):
        oracle.parse_review(
            json.dumps(
                {
                    "decision": "APPROVED",
                    "reasoning_summary": "Looks mostly complete.",
                    "issues": [],
                    "missing_coverage": ["skill:payment.refund"],
                    "evidence_citations": ["requirement:payments"],
                }
            )
        )


def test_review_content_reads_openai_message():
    content = oracle.review_content(
        {"choices": [{"message": {"content": '{"decision":"REJECTED"}'}, "finish_reason": "stop"}]}
    )

    assert content == '{"decision":"REJECTED"}'


def test_review_content_combines_text_blocks():
    content = oracle.review_content(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "output_text", "text": '{"decision":'},
                            {"type": "text", "text": '"REJECTED"}'},
                        ]
                    }
                }
            ]
        }
    )

    assert content == '{"decision":"REJECTED"}'


def test_review_content_accepts_qwen_structured_output_in_reasoning_field():
    content = oracle.review_content(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": json.dumps(
                            {
                                "decision": "REJECTED",
                                "reasoning_summary": "A declared behavior is not tested.",
                                "issues": ["Missing assertion"],
                                "missing_coverage": ["scenario:status"],
                                "evidence_citations": ["scenario:status"],
                            }
                        ),
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )

    assert json.loads(content)["decision"] == "REJECTED"


def test_review_content_rejects_unstructured_qwen_reasoning_without_final_json():
    with pytest.raises(RuntimeError, match="finish_reason=length, reasoning_only=true"):
        oracle.review_content(
            {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "unfinished private reasoning"},
                        "finish_reason": "length",
                    }
                ]
            }
        )


def test_high_impact_review_rejects_same_model_before_reading_artifact(monkeypatch):
    monkeypatch.setattr(oracle, "REQUIRE_INDEPENDENT_HIGH_IMPACT", True)
    monkeypatch.setattr(oracle, "independently_configured", lambda: False)

    result = asyncio.run(
        oracle.review_generated_test(
            {"task_id": "task-1", "object_path": "tests/task-1.py", "impact": "high"}
        )
    )

    assert result["status"] == "REJECTED"


def test_approved_review_persists_provenance(monkeypatch):
    class FakeStore:
        writes = []

        def read_text(self, _path):
            return "def test_declared_behavior():\n    assert response.status_code == 200\n"

        def ensure_bucket(self):
            return None

        def write_bytes(self, path, payload, content_type):
            self.writes.append((path, payload, content_type))

    async def approve(_prompt):
        return json.dumps(
            {
                "decision": "APPROVED",
                "reasoning_summary": "The test is grounded in the declared requirement.",
                "issues": [],
                "missing_coverage": [],
                "evidence_citations": ["scenario:status"],
            }
        )

    store = FakeStore()
    monkeypatch.setattr(oracle, "ObjectStore", lambda: store)
    monkeypatch.setattr(oracle, "_reason", approve)
    monkeypatch.setattr(oracle, "independently_configured", lambda: True)

    result = asyncio.run(
        oracle.review_generated_test(
            {"task_id": "task-2", "object_path": "tests/task-2.py", "impact": "moderate"}
        )
    )

    assert result["evidence_path"] == "artifacts/oracle-reviews/task-2/review.json"
