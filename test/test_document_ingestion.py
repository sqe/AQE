import asyncio
from types import SimpleNamespace

from agents.knowledge_ingestion.app import LLMFineTuningAgentLogic, extract_document_text, refine_requirements


def test_markdown_document_text_is_extracted():
    text = extract_document_text("requirements.md", b"The agent must respond within 2 seconds.")

    assert text == "The agent must respond within 2 seconds."


def test_requirements_are_deduplicated():
    requirements = refine_requirements("The agent must answer.\nThe agent must answer.")

    assert len(requirements) == 1


def test_requirement_is_classified_as_testable():
    requirements = refine_requirements("The agent must answer within 2 seconds.")

    assert requirements[0]["testable"] is True


def test_platform_stats_report_real_store_counts():
    class FakeQdrant:
        def collection_exists(self, _name):
            return True

        def count(self, **kwargs):
            count_filter = kwargs.get("count_filter")
            if count_filter is None:
                return SimpleNamespace(count=12)
            field = count_filter.must[0].key
            return SimpleNamespace(count=4 if field == "record_kind" else 6)

    class FakeStore:
        def count_objects(self, prefix):
            return {"requirements/": 3, "artifacts/": 9}[prefix]

    class FakeConnection:
        async def fetchval(self, query):
            return 7 if "test_runs" in query else 2

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return None

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    logic = LLMFineTuningAgentLogic.__new__(LLMFineTuningAgentLogic)
    logic.qdrant_client = FakeQdrant()
    logic.artifact_manager = SimpleNamespace(
        object_store=FakeStore(),
        db_pool=FakePool(),
        init_db_pool=lambda: asyncio.sleep(0),
    )

    stats = asyncio.run(logic.platform_stats())

    assert {name: item["count"] for name, item in stats["stores"].items()} == {
        "qdrant_vectors": 12,
        "rag_requirements": 4,
        "ontology_records": 6,
        "documents": 3,
        "evidence_objects": 9,
        "test_runs": 7,
        "active_versions": 2,
    }
