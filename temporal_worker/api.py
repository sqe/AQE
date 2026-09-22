import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client

from .workflow import QualityEngineeringWorkflow


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


@app.post("/v1/qe-runs", status_code=202)
async def start_run(request: dict[str, Any]) -> dict[str, str]:
    if not request.get("url"):
        raise HTTPException(status_code=400, detail="url is required")
    workflow_id = str(request.get("workflow_id") or f"qe-{uuid.uuid4()}")
    assert client is not None
    await client.start_workflow(
        QualityEngineeringWorkflow.run,
        request,
        id=workflow_id,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "aqe-workflows"),
    )
    return {"workflow_id": workflow_id, "status": "accepted"}


@app.get("/v1/qe-runs/{workflow_id}")
async def describe_run(workflow_id: str) -> dict[str, Any]:
    assert client is not None
    handle = client.get_workflow_handle(workflow_id)
    description = await handle.describe()
    response: dict[str, Any] = {"workflow_id": workflow_id, "status": description.status.name}
    if description.status.name == "COMPLETED":
        response["result"] = await handle.result()
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}
