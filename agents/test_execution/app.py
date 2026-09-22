"""AQE sandboxed agent-test execution and bounded repair runtime.

Generated tests execute in a fresh temporary workspace with a scrubbed
environment, resource limits, and a hard timeout. The service container (or a
Kubernetes Job in production) remains the security boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message
from observability.metrics import PrometheusMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from utils.github_test_catalog import publish_test
from utils.object_store import ObjectStore

try:
    from .test_quality import inspect_test_code
except ImportError:  # Direct script execution in the container image.
    from test_quality import inspect_test_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("aqe.test-execution")

POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql://user:pass@postgres:5432/qe_db")
EXECUTION_TIMEOUT_SECONDS = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "180"))
MAX_REPAIR_ATTEMPTS = int(os.getenv("MAX_REPAIR_ATTEMPTS", "2"))
REPAIR_LLM_URL = os.getenv("REPAIR_LLM_URL", "")
REPAIR_LLM_MODEL = os.getenv("REPAIR_LLM_MODEL", "")
REPAIR_LLM_API_KEY = os.getenv("REPAIR_LLM_API_KEY", "")
TEST_EXECUTION_MODE = os.getenv("TEST_EXECUTION_MODE", "agent").lower()


def _sandbox_limits() -> None:
    """Bound a generated test process on Unix before exec."""
    limits = [
        (resource.RLIMIT_CPU, EXECUTION_TIMEOUT_SECONDS),
        (resource.RLIMIT_NOFILE, 256),
    ]
    if sys.platform.startswith("linux"):
        limits.append((resource.RLIMIT_AS, 2 * 1024 * 1024 * 1024))
    for limit, requested in limits:
        _, hard = resource.getrlimit(limit)
        bounded = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(limit, (bounded, bounded))


def _safe_environment() -> dict[str, str]:
    allowed = ("PATH", "PYTHONPATH", "PLAYWRIGHT_BROWSERS_PATH", "DISPLAY", "HOME", "LANG")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.startswith("TARGET_AUTH_")
            or key in {"TARGET_BASE_URL", "AGENT_BASE_URL", "AGENT_CARD_URL"}
        }
    )
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
    return environment


def execution_accepts(test_type: str) -> bool:
    return test_type == TEST_EXECUTION_MODE


def catalog_outcome(
    result: dict[str, Any], finding_disposition: str, finding_evidence: str
) -> str | None:
    if result["successful"]:
        return "validated"
    summary = result.get("summary", {})
    if (
        finding_disposition == "confirmed_product_defect"
        and finding_evidence.strip()
        and summary.get("failed", 0) > 0
        and summary.get("errors", 0) == 0
    ):
        return "confirmed_product_defect"
    return None


def _summary_from_junit(report: Path) -> dict[str, int]:
    if not report.exists():
        return {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    tests = sum(int(suite.get("tests", 0)) for suite in suites)
    failed = sum(int(suite.get("failures", 0)) for suite in suites)
    errors = sum(int(suite.get("errors", 0)) for suite in suites)
    skipped = sum(int(suite.get("skipped", 0)) for suite in suites)
    return {
        "tests": tests,
        "passed": max(0, tests - failed - errors - skipped),
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
    }


async def execute_code(code: str) -> dict[str, Any]:
    issues = inspect_test_code(code)
    if issues:
        return {
            "successful": False,
            "return_code": 2,
            "output": "",
            "error": "Generated test failed the quality gate",
            "quality_issues": [issue.as_dict() for issue in issues],
            "summary": {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
        }

    with tempfile.TemporaryDirectory(prefix="aqe-run-") as workspace:
        workdir = Path(workspace)
        test_path = workdir / "test_generated.py"
        report_path = workdir / "junit.xml"
        test_path.write_text(code, encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--disable-warnings",
            f"--junitxml={report_path}",
            str(test_path),
            cwd=workdir,
            env=_safe_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_sandbox_limits,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=EXECUTION_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return {
                "successful": False,
                "return_code": 124,
                "output": stdout.decode(errors="replace"),
                "error": f"Test execution exceeded {EXECUTION_TIMEOUT_SECONDS} seconds",
                "quality_issues": [],
                "summary": _summary_from_junit(report_path),
            }

        return_code = process.returncode or 0
        return {
            "successful": return_code == 0,
            "return_code": return_code,
            "output": stdout.decode(errors="replace"),
            "error": stderr.decode(errors="replace"),
            "quality_issues": [],
            "summary": _summary_from_junit(report_path),
        }


class RepairClient:
    async def repair(self, code: str, result: dict[str, Any]) -> str | None:
        if not REPAIR_LLM_URL:
            return None
        prompt = (
            f"Repair this {TEST_EXECUTION_MODE} pytest test without weakening, deleting, or changing its intended "
            "observable behavior. Use one assertion per test, split scenarios into atomic tests, use "
            "accessible resilient locators, and return only Python source.\n\n"
            f"TEST SOURCE:\n{code}\n\nEXECUTION RESULT:\n{json.dumps(result, default=str)}"
        )
        headers = {"Authorization": f"Bearer {REPAIR_LLM_API_KEY}"} if REPAIR_LLM_API_KEY else {}
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                REPAIR_LLM_URL,
                headers=headers,
                json={
                    "model": REPAIR_LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            candidate = response.json()["choices"][0]["message"]["content"].strip()
        if candidate.startswith("```python") and candidate.endswith("```"):
            candidate = candidate[len("```python") : -len("```")].strip()
        return candidate


class TestExecutionAgentLogic:
    def __init__(self, repair_client: RepairClient | None = None) -> None:
        self.object_store = ObjectStore()
        self.pool: asyncpg.Pool | None = None
        self.repair_client = repair_client or RepairClient()

    async def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            self.pool = await asyncpg.create_pool(POSTGRES_URL)
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_id TEXT;
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_version TEXT;
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_card_url TEXT;
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_skills JSONB;
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_profile JSONB;
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS test_catalog JSONB;
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS test_type TEXT NOT NULL DEFAULT 'agent';
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS finding_disposition TEXT NOT NULL DEFAULT 'untriaged';
                    ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS finding_evidence TEXT;
                    """
                )
        return self.pool

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def _load(self, task_id: str) -> tuple[str, str, dict[str, Any]]:
        pool = await self._pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT object_path, target_agent_id, target_agent_version,
                       target_agent_card_url, target_agent_skills, target_agent_profile, test_type
                FROM test_runs WHERE task_id = $1
                """,
                task_id,
            )
        if row is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        object_name = row["object_path"]

        def download() -> str:
            return self.object_store.read_text(object_name)

        target = {
            "id": row["target_agent_id"] or "unknown-agent",
            "version": row["target_agent_version"] or "unversioned",
            "card_url": row["target_agent_card_url"],
            "skills": row["target_agent_skills"] or [],
            "ontology_profile": row["target_agent_profile"] or {},
            "test_type": row["test_type"],
        }
        return object_name, await asyncio.to_thread(download), target

    async def _persist(
        self,
        task_id: str,
        code: str,
        result: dict[str, Any],
        repaired: bool,
        catalog: dict[str, str] | None,
        finding_disposition: str,
        finding_evidence: str,
    ) -> None:
        object_name = f"artifacts/repairs/{task_id}/test_code.py" if repaired else None
        if object_name:
            payload = code.encode()

            def upload() -> None:
                self.object_store.write_bytes(object_name, payload, "text/x-python")

            await asyncio.to_thread(upload)

        status = "PASSED" if result["successful"] else "FAILED"
        pool = await self._pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE test_runs
                SET status = $1, passed = $2, execution_results = $3::jsonb,
                    summary = $4::jsonb, raw_code = $5,
                    object_path = COALESCE($6, object_path), test_catalog = $7::jsonb,
                    finding_disposition = $8, finding_evidence = $9,
                    timestamp_completed = NOW()
                WHERE task_id = $10
                """,
                status,
                result["successful"],
                json.dumps(result),
                json.dumps(result["summary"]),
                code,
                object_name,
                json.dumps(catalog) if catalog else None,
                finding_disposition,
                finding_evidence or None,
                task_id,
            )

    async def run(
        self,
        task_id: str,
        repair: bool = True,
        finding_disposition: str = "untriaged",
        finding_evidence: str = "",
    ) -> dict[str, Any]:
        _, code, target = await self._load(task_id)
        if not execution_accepts(str(target["test_type"])):
            raise ValueError(
                f"{target['test_type']} test must run in the {target['test_type']} executor, "
                f"not the {TEST_EXECUTION_MODE} executor"
            )
        attempts: list[dict[str, Any]] = []
        repaired = False
        for attempt_number in range(MAX_REPAIR_ATTEMPTS + 1):
            result = await execute_code(code)
            attempts.append({"attempt": attempt_number + 1, **result})
            if result["successful"] or not repair or attempt_number == MAX_REPAIR_ATTEMPTS:
                break
            candidate = await self.repair_client.repair(code, result)
            if not candidate:
                break
            code = candidate
            repaired = True

        final = attempts[-1]
        catalog = None
        catalog_error = None
        outcome = catalog_outcome(final, finding_disposition, finding_evidence)
        if outcome:
            try:
                catalog = await asyncio.to_thread(
                    publish_test,
                    agent_id=str(target["id"]),
                    agent_version=str(target["version"]),
                    task_id=task_id,
                    code=code,
                    metadata={
                        "task_id": task_id,
                        "target_agent": target,
                        "repaired": repaired,
                        "outcome": outcome,
                        "finding_evidence": finding_evidence or None,
                        "summary": final["summary"],
                    },
                    test_type=str(target["test_type"]),
                    outcome=outcome,
                )
            except Exception as exc:
                catalog_error = str(exc)
                logger.exception("Failed to publish validated test to the GitHub catalog")
        response = {
            **final,
            "task_id": task_id,
            "repaired": repaired,
            "attempts": attempts,
            "test_catalog": catalog,
            "test_catalog_error": catalog_error,
            "catalog_outcome": outcome,
            "finding_disposition": finding_disposition,
        }
        await self._persist(
            task_id,
            code,
            response,
            repaired,
            catalog,
            finding_disposition,
            finding_evidence,
        )
        return response


LOGIC = TestExecutionAgentLogic()


class TestExecutionAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.input_args.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            await event_queue.enqueue_event(new_agent_text_message("task_id is required"))
            return
        result = await LOGIC.run(
            task_id,
            bool(context.input_args.get("repair", True)),
            str(context.input_args.get("finding_disposition", "untriaged")),
            str(context.input_args.get("finding_evidence", "")),
        )
        await event_queue.enqueue_event(new_agent_text_message(json.dumps(result)))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await LOGIC.close()


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "agent": "aqe-execution", "test_type": TEST_EXECUTION_MODE})


async def discovery(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "name": "aqe",
            "description": "Compound quality-engineering agent",
            "version": "2.0.0",
            "task_topic": "tasks.aqe",
            "result_topic": "results.aqe",
            "skills": [
                {"id": "qe.validate", "description": f"Run generated {TEST_EXECUTION_MODE} tests in a bounded sandbox"},
                {"id": "qe.repair", "description": "Diagnose, repair, and re-run failing tests"},
            ],
        }
    )


async def run_tests(request: Request) -> JSONResponse:
    body = await request.json()
    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return JSONResponse({"error": "task_id is required"}, status_code=400)
    try:
        result = await LOGIC.run(
            task_id,
            bool(body.get("repair", True)),
            str(body.get("finding_disposition", "untriaged")),
            str(body.get("finding_evidence", "")),
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(result, status_code=200 if result["successful"] else 422)


async def confirm_finding(request: Request) -> JSONResponse:
    """Reproduce and catalog a human/triage-confirmed product defect."""
    body = await request.json()
    evidence = body.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return JSONResponse({"error": "non-empty evidence is required"}, status_code=400)
    try:
        result = await LOGIC.run(
            request.path_params["task_id"],
            repair=False,
            finding_disposition="confirmed_product_defect",
            finding_evidence=evidence,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if result["catalog_outcome"] != "confirmed_product_defect":
        return JSONResponse(
            {"error": "test did not reproduce as assertion-only failures", "result": result},
            status_code=422,
        )
    return JSONResponse(result)


def build_app() -> Any:
    port = int(os.getenv("AGENT_PORT", "8003"))
    card = AgentCard(
        name=f"AQE {TEST_EXECUTION_MODE.title()} Test Execution Agent",
        description=f"Runs and repairs generated {TEST_EXECUTION_MODE} tests in an isolated sandbox",
        url=f"http://test_execution_agent:{port}/",
        version="2.0.0",
        default_input_modes=["args"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="qe.validate",
                name="Validate generated tests",
                description="Execute a persisted test run",
                tags=["qe", "pytest", TEST_EXECUTION_MODE],
                examples=["validate task_id 123"],
            )
        ],
    )
    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=DefaultRequestHandler(
            agent_executor=TestExecutionAgentExecutor(), task_store=InMemoryTaskStore()
        ),
    ).build()
    for route in reversed(
        [
            Route("/health", health),
            Route("/agent_card", discovery),
            Route("/.well-known/agent.json", discovery),
            Route("/run_tests", run_tests, methods=["POST"]),
            Route("/v1/findings/{task_id}/confirm", confirm_finding, methods=["POST"]),
        ]
    ):
        app.routes.insert(0, route)
    cors_app = CORSMiddleware(app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    return PrometheusMiddleware(cors_app, f"{TEST_EXECUTION_MODE}-test-execution")


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.getenv("AGENT_PORT", "8003")))
