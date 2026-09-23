import asyncio
import json

from agents.test_generation import app as test_generation


class _EmbeddingResponse:
    status_code = 200
    headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"embedding": {"values": [0.1, 0.2]}}


class _EmbeddingClient:
    def __init__(self):
        self.request = None

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return _EmbeddingResponse()


class _TransientGeminiClient:
    def __init__(self):
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls < 3:
            return type("Response", (), {"status_code": 503, "headers": {}})()
        return _EmbeddingResponse()


def test_gemini_embedding_uses_current_model_resource(monkeypatch):
    monkeypatch.setattr(test_generation, "GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
    service = test_generation.LLMServiceClient("GEMINI", "secret")
    asyncio.run(service.client.aclose())
    service.client = _EmbeddingClient()

    values = asyncio.run(service.get_embedding("release evidence"))

    url, request = service.client.request
    assert (
        values,
        url,
        request["json"]["model"],
        request["json"]["outputDimensionality"],
    ) == (
        [0.1, 0.2],
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent",
        "models/gemini-embedding-001",
        test_generation.EMBEDDING_DIMENSION,
    )


def test_gemini_retries_transient_service_failures(monkeypatch):
    monkeypatch.setattr(test_generation, "GEMINI_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(test_generation.asyncio, "sleep", lambda _delay: _completed_sleep())
    service = test_generation.LLMServiceClient("GEMINI", "secret")
    asyncio.run(service.client.aclose())
    service.client = _TransientGeminiClient()

    response = asyncio.run(service._post_gemini("https://gemini.invalid", {}))

    assert (response.status_code, service.client.calls) == (200, 3)


async def _completed_sleep():
    return None


def test_agent_card_declares_executable_generation_contract():
    response = asyncio.run(test_generation.agent_card_endpoint(None))
    card = json.loads(response.body)

    assert (
        card["skills"][0]["invocation"]["url"],
        card["evaluation"]["cases"][0]["skill_id"],
        card["evaluation"]["cases"][0]["expected_response"]["status"],
    ) == (
        f"{test_generation.PUBLIC_BASE_URL}/generate_test_plan",
        "generate_tests",
        "SUCCESS",
    )


def test_generation_repairs_static_quality_before_oracle():
    invalid = """
AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "sample-contract"
AQE_SKILL_TESTS = {"sample.run": {"positive": "test_sample_run_positive"}}
def test_sample_run_positive():
    assert 1 == 1
    assert 2 == 2
"""
    valid = """
AQE_TEST_LAYER = "agentic"
AQE_SUITE_ID = "sample-contract"
AQE_SKILL_TESTS = {"sample.run": {"positive": "test_sample_run_positive"}}
def test_sample_run_positive():
    assert (1, 2) == (1, 2)
"""

    class Generator:
        def __init__(self):
            self.calls = []

        async def generate_code(self, prompt):
            self.calls.append(prompt)
            return invalid if len(self.calls) == 1 else valid

    logic = test_generation.TestGenerationAgentLogic()
    asyncio.run(logic.llm_service.close())
    logic.llm_service = Generator()

    candidate, issues = asyncio.run(logic.generate_quality_candidate(
        "generate tests",
        {
            "skills": ["sample.run"],
            "scenarios": [{"skill_id": "sample.run", "required_dimensions": ["positive"]}],
        },
    ))

    assert (
        issues,
        candidate,
        len(logic.llm_service.calls),
        "single-outcome" in logic.llm_service.calls[1],
    ) == ([], valid.strip(), 2, True)
