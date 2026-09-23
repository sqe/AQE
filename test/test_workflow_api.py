import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from temporal_worker import api
from temporal_worker.api import failure_message, normalize_source_repository


def test_repository_slug_is_unchanged():
    assert normalize_source_repository("sqe/AQE") == "sqe/AQE"


def test_github_repository_url_is_normalized():
    assert normalize_source_repository("https://github.com/sqe/AQE.git") == "sqe/AQE"


def test_non_github_repository_shape_is_rejected():
    with pytest.raises(ValueError, match="owner/name"):
        normalize_source_repository("https://example.com/sqe/AQE")


def test_nested_failure_reports_root_cause_first():
    root = RuntimeError("repository must be owner/name")
    wrapper = RuntimeError("activity failed")
    wrapper.__cause__ = root

    assert failure_message(wrapper) == "repository must be owner/name: activity failed"


def test_fleet_batch_starts_one_durable_parent_workflow(monkeypatch):
    class FakeClient:
        async def list_workflows(self, *_args, **_kwargs):
            if False:
                yield None

        async def start_workflow(self, *_args, **_kwargs):
            assert _kwargs["task_queue"] == "aqe-workflows"
            return None

    monkeypatch.setattr(api, "client", FakeClient())
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "")

    result = asyncio.run(api.start_batch({"runs": [{"url": "https://agent.example"}]}))

    assert result["agents"] == 1


def test_fleet_batch_rejects_overlapping_campaign(monkeypatch):
    class FakeClient:
        async def list_workflows(self, *_args, **_kwargs):
            yield SimpleNamespace(
                id="qe-fleet-active",
                workflow_type="FleetQualityEngineeringWorkflow",
                status=SimpleNamespace(name="RUNNING"),
                parent_id=None,
            )

    monkeypatch.setattr(api, "client", FakeClient())

    with pytest.raises(api.HTTPException) as error:
        asyncio.run(api.start_batch({"runs": [{"url": "https://agent.example"}]}))

    assert error.value.status_code == 409


def test_fleet_batch_override_cancels_active_campaign_before_start(monkeypatch):
    actions = []

    class FakeHandle:
        async def cancel(self):
            actions.append("cancel")

    class FakeClient:
        async def list_workflows(self, *_args, **_kwargs):
            yield SimpleNamespace(
                id="qe-fleet-active",
                workflow_type="FleetQualityEngineeringWorkflow",
                status=SimpleNamespace(name="RUNNING"),
                parent_id=None,
            )

        def get_workflow_handle(self, _workflow_id):
            return FakeHandle()

        async def start_workflow(self, *_args, **_kwargs):
            actions.append("start")

    monkeypatch.setattr(api, "client", FakeClient())

    result = asyncio.run(
        api.start_batch({"runs": [{"url": "https://agent.example"}], "replace_active": True})
    )

    assert actions == ["cancel", "start"]
    assert result["replaced_workflows"] == ["qe-fleet-active"]


def test_fleet_batch_rejects_empty_campaign():
    with pytest.raises(api.HTTPException) as error:
        asyncio.run(api.start_batch({"runs": []}))

    assert error.value.status_code == 400


def test_workflow_history_is_sourced_from_temporal(monkeypatch):
    class FakeClient:
        async def list_workflows(self, *_args, **_kwargs):
            yield SimpleNamespace(
                id="qe-42",
                run_id="run-7",
                workflow_type="QualityEngineeringWorkflow",
                status=SimpleNamespace(name="COMPLETED"),
                start_time=datetime(2026, 9, 23, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 23, 0, 1, tzinfo=timezone.utc),
                history_length=19,
                parent_id=None,
                task_queue="aqe-workflows",
            )

    monkeypatch.setattr(api, "client", FakeClient())

    result = asyncio.run(api.list_runs())

    assert result["workflows"][0]["history_length"] == 19
