import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client, WorkflowFailureError

from .workflow import FleetQualityEngineeringWorkflow, QualityEngineeringWorkflow


client: Client | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global client
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    yield
    client = None


app = FastAPI(title="AQE Workflow API", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin for origin in os.getenv("WORKFLOW_CORS_ORIGINS", "http://localhost:3000").split(",") if origin],
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


def normalize_source_repository(value: str) -> str:
    repository = value.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if repository and repository.count("/") != 1:
        raise ValueError("source_repository must be owner/name or a GitHub repository URL")
    return repository


def failure_message(error: BaseException) -> str:
    messages: list[str] = []
    current: BaseException | None = error
    while current is not None:
        message = str(current).strip()
        if message and message not in messages:
            messages.append(message)
        current = current.__cause__
    return ": ".join(reversed(messages))


async def active_fleet_workflow_ids() -> list[str]:
    assert client is not None
    active: list[str] = []
    async for execution in client.list_workflows(
        'WorkflowId STARTS_WITH "qe-fleet-"',
        page_size=100,
    ):
        if (
            execution.status
            and execution.status.name == "RUNNING"
            and execution.workflow_type == "FleetQualityEngineeringWorkflow"
            and not execution.parent_id
        ):
            active.append(execution.id)
    return active


@app.post("/v1/qe-runs", status_code=202)
async def start_run(request: dict[str, Any]) -> dict[str, str]:
    if not request.get("url"):
        raise HTTPException(status_code=400, detail="url is required")
    try:
        source_repository = normalize_source_repository(str(request.get("source_repository", "")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if source_repository and not request.get("source_ref"):
        raise HTTPException(status_code=400, detail="source_ref is required when source_repository is set")
    request = {**request, "source_repository": source_repository}
    workflow_id = str(request.get("workflow_id") or f"qe-{uuid.uuid4()}")
    assert client is not None
    await client.start_workflow(
        QualityEngineeringWorkflow.run,
        request,
        id=workflow_id,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE") or "aqe-workflows",
    )
    return {"workflow_id": workflow_id, "status": "accepted"}


@app.post("/v1/qe-runs/batch", status_code=202)
async def start_batch(request: dict[str, Any]) -> dict[str, Any]:
    runs = request.get("runs")
    if not isinstance(runs, list) or not runs or len(runs) > 50:
        raise HTTPException(status_code=400, detail="runs must contain between 1 and 50 requests")
    normalized_runs: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict) or not run.get("url"):
            raise HTTPException(status_code=400, detail="every run requires a url")
        try:
            source_repository = normalize_source_repository(str(run.get("source_repository", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if source_repository and not run.get("source_ref"):
            raise HTTPException(status_code=400, detail="source_ref is required when source_repository is set")
        normalized_runs.append({**run, "source_repository": source_repository})

    assert client is not None
    active_workflows = await active_fleet_workflow_ids()
    replace_active = request.get("replace_active") is True
    if active_workflows and not replace_active:
        raise HTTPException(
            status_code=409,
            detail=f"active fleet campaign already exists: {active_workflows[0]}; use replace_active to cancel it",
        )
    if replace_active:
        for active_workflow_id in active_workflows:
            await client.get_workflow_handle(active_workflow_id).cancel()

    workflow_id = str(request.get("workflow_id") or f"qe-fleet-{uuid.uuid4()}")
    await client.start_workflow(
        FleetQualityEngineeringWorkflow.run,
        {
            "runs": normalized_runs,
            "max_concurrency": min(max(int(request.get("max_concurrency", 1)), 1), 5),
        },
        id=workflow_id,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE") or "aqe-workflows",
    )
    return {
        "workflow_id": workflow_id,
        "status": "accepted",
        "agents": len(normalized_runs),
        "replaced_workflows": active_workflows if replace_active else [],
    }


@app.get("/v1/qe-runs")
async def list_runs(limit: int = 50) -> dict[str, Any]:
    """Expose bounded Temporal summaries; Temporal remains the history source of truth."""
    assert client is not None
    bounded_limit = min(max(limit, 1), 200)
    workflows = []
    async for execution in client.list_workflows(
        'WorkflowId STARTS_WITH "qe-"',
        limit=bounded_limit,
        page_size=min(bounded_limit, 100),
    ):
        workflows.append(
            {
                "workflow_id": execution.id,
                "run_id": execution.run_id,
                "workflow_type": execution.workflow_type,
                "status": execution.status.name if execution.status else "UNKNOWN",
                "start_time": execution.start_time.isoformat(),
                "close_time": execution.close_time.isoformat() if execution.close_time else None,
                "history_length": execution.history_length,
                "parent_workflow_id": execution.parent_id,
                "task_queue": execution.task_queue,
            }
        )
    return {
        "workflows": workflows,
        "count": len(workflows),
        "namespace": os.getenv("TEMPORAL_NAMESPACE", "default"),
        "temporal_ui_url": os.getenv("TEMPORAL_UI_URL", ""),
    }


@app.get("/v1/qe-runs/{workflow_id}")
async def describe_run(workflow_id: str) -> dict[str, Any]:
    assert client is not None
    handle = client.get_workflow_handle(workflow_id)
    description = await handle.describe()
    response: dict[str, Any] = {
        "workflow_id": workflow_id,
        "run_id": description.run_id,
        "status": description.status.name,
        "start_time": description.start_time.isoformat(),
        "close_time": description.close_time.isoformat() if description.close_time else None,
        "history_length": description.history_length,
    }
    if description.status.name == "COMPLETED":
        response["result"] = await handle.result()
    elif description.status.name in {"FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}:
        try:
            await handle.result()
        except WorkflowFailureError as exc:
            response["error"] = failure_message(exc)
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}
