import asyncio

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
