import asyncio

from agents.test_generation import app as test_generation


class _EmbeddingResponse:
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
