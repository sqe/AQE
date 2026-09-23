import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


def error_message(error: BaseException) -> str:
    messages: list[str] = []
    current: BaseException | None = error
    while current is not None:
        message = str(current).strip()
        if message and message not in messages:
            messages.append(message)
        current = current.__cause__
    return ": ".join(reversed(messages)) or type(error).__name__


@workflow.defn
class QualityEngineeringWorkflow:
    """Durably generate and execute a test run through existing AQE APIs."""

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        emit_stages = workflow.patched("workflow-graph-events-v1")
        workflow_id = workflow.info().workflow_id

        async def record_stage(kind: str, **fields: Any) -> None:
            if not emit_stages:
                return
            await workflow.execute_activity(
                "record_workflow_stage",
                {
                    "kind": kind,
                    "workflow_id": workflow_id,
                    "test_type": request.get("test_type", "agent"),
                    "target_agent": request.get("target_agent") or {
                        "id": request.get("agent_name"),
                        "version": request.get("agent_version"),
                    },
                    **fields,
                },
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

        await record_stage("workflow_started")
        generation_request = request
        if request.get("test_type", "agent") == "agent" and request.get("agent_card_url"):
            await record_stage("agent_discovery_started")
            agent_discovery = await workflow.execute_activity(
                "discover_agent",
                {
                    "card_urls": [request["agent_card_url"]],
                    "max_depth": request.get("discovery_depth", 2),
                    "max_latency_ms": request.get("max_latency_ms", 5000),
                    "min_accuracy": request.get("min_accuracy", 0.8),
                },
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            discovered_agents = agent_discovery.get("agents", [])
            discovered_agent = discovered_agents[0] if discovered_agents else {}
            discovered_skills = discovered_agent.get("skills", [])
            generation_request = {
                **generation_request,
                "agent_discovery": agent_discovery,
                "scenarios": generation_request.get("scenarios") or discovered_agent.get("scenarios", []),
                "skills": generation_request.get("skills") or discovered_skills,
                "target_agent": generation_request.get("target_agent") or {
                    "id": discovered_agent.get("identity") or request.get("agent_name"),
                    "version": discovered_agent.get("version") or request.get("agent_version") or "unversioned",
                    "card_url": request["agent_card_url"],
                    "skills": discovered_skills,
                },
            }
            if workflow.patched("discovery-archetype-v1"):
                generation_request["agent_archetype"] = (
                    generation_request.get("agent_archetype") or discovered_agent.get("archetype")
                )
        if request.get("source_repository"):
            await record_stage("source_analysis_started")
            source_analysis = await workflow.execute_activity(
                "analyze_source",
                {
                    "repository": request["source_repository"],
                    "ref": request.get("source_ref") or request.get("agent_version"),
                    "paths": request.get("source_paths", []),
                },
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            generation_request = {**generation_request, "source_analysis": source_analysis}
        await record_stage("test_generation_started")
        generated = await workflow.execute_activity(
            "generate_tests",
            generation_request,
            start_to_close_timeout=timedelta(minutes=10),
            # Generation persists a new artifact and is not safe to replay until
            # the generation endpoint accepts an idempotency key.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        if generated.get("status") != "SUCCESS":
            raise ApplicationError(
                str(generated.get("error") or "Test generation failed without an error message"),
                non_retryable=True,
            )
        oracle_review = {"status": "SKIPPED", "reasoning_summary": "Legacy workflow predates oracle gate."}
        if workflow.patched("quality-oracle-v1"):
            deterministic_candidate = (
                (generated.get("grounding") or {}).get("generation_method")
                == "declared_contract_compiler"
            )
            review_attempts = (
                2
                if workflow.patched("quality-oracle-repair-v1") and not deterministic_candidate
                else 1
            )
            for review_attempt in range(review_attempts):
                object_path = generated.get("object_path")
                if not object_path:
                    raise ApplicationError(
                        "Generated test result has no immutable object_path; regenerate with the current test-generation agent",
                        non_retryable=True,
                    )
                await record_stage("oracle_review_started", task_id=generated["task_id"])
                oracle_review = await workflow.execute_activity(
                    "review_generated_tests",
                    {
                        **generation_request,
                        "task_id": generated["task_id"],
                        "object_path": object_path,
                        "grounding": generated.get("grounding", {}),
                    },
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                if oracle_review.get("status") == "APPROVED":
                    break
                if review_attempt + 1 < review_attempts:
                    repair_request = {
                        **generation_request,
                        "oracle_feedback": {
                            key: oracle_review.get(key)
                            for key in ("reasoning_summary", "issues", "missing_coverage", "evidence_citations")
                        },
                    }
                    await record_stage(
                        "test_generation_started",
                        task_id=generated["task_id"],
                        summary={"reason": "oracle_feedback_repair", "attempt": review_attempt + 2},
                    )
                    generated = await workflow.execute_activity(
                        "generate_tests",
                        repair_request,
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                    if generated.get("status") != "SUCCESS":
                        raise ApplicationError(
                            str(generated.get("error") or "Oracle-feedback regeneration failed"),
                            non_retryable=True,
                        )
            if oracle_review.get("status") != "APPROVED":
                rejection_context = " after repair" if review_attempts > 1 else ""
                raise ApplicationError(
                    f"Quality Oracle rejected generated tests{rejection_context}: "
                    + "; ".join(oracle_review.get("issues") or ["review did not approve execution"]),
                    non_retryable=True,
                )
        await record_stage("test_execution_started", task_id=generated["task_id"])
        execution_result = await workflow.execute_activity(
            "execute_and_repair",
            {
                "task_id": generated["task_id"],
                "test_type": generated["test_type"],
                "repair": request.get("repair", True),
                "finding_disposition": request.get("finding_disposition", "untriaged"),
                "finding_evidence": request.get("finding_evidence", ""),
            },
            start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        await record_stage(
            "workflow_completed",
            task_id=generated["task_id"],
            status="passed" if execution_result.get("successful") else "failed",
        )
        return {**execution_result, "oracle_review": oracle_review}


@workflow.defn
class FleetQualityEngineeringWorkflow:
    """Run a bounded fleet campaign as durable child QE workflows."""

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        runs = request.get("runs", [])
        max_concurrency = min(max(int(request.get("max_concurrency", 1)), 1), 5)
        outcomes: list[dict[str, Any]] = []

        async def execute(index: int, run: dict[str, Any]) -> dict[str, Any]:
            child_id = f"{workflow.info().workflow_id}-agent-{index + 1}"
            try:
                result = await workflow.execute_child_workflow(
                    QualityEngineeringWorkflow.run,
                    run,
                    id=child_id,
                    task_queue=workflow.info().task_queue,
                )
                return {
                    "workflow_id": child_id,
                    "agent": run.get("agent_name") or run.get("agent_card_url"),
                    "successful": bool(result.get("successful")),
                    "result": result,
                }
            except Exception as exc:
                detailed_error = error_message(exc)
                if workflow.patched("fleet-failure-graph-events-v1"):
                    try:
                        await workflow.execute_activity(
                            "record_workflow_stage",
                            {
                                "kind": "workflow_completed",
                                "workflow_id": child_id,
                                "test_type": run.get("test_type", "agent"),
                                "target_agent": run.get("target_agent") or {
                                    "id": run.get("agent_name"),
                                    "version": run.get("agent_version"),
                                },
                                "status": "failed",
                                "summary": {"error": detailed_error},
                            },
                            start_to_close_timeout=timedelta(seconds=10),
                            retry_policy=RetryPolicy(maximum_attempts=1),
                        )
                    except Exception:
                        pass
                return {
                    "workflow_id": child_id,
                    "agent": run.get("agent_name") or run.get("agent_card_url"),
                    "successful": False,
                    "error": detailed_error,
                }

        for offset in range(0, len(runs), max_concurrency):
            chunk = runs[offset : offset + max_concurrency]
            outcomes.extend(
                await asyncio.gather(
                    *(execute(offset + index, run) for index, run in enumerate(chunk))
                )
            )

        passed = sum(1 for outcome in outcomes if outcome["successful"])
        return {
            "successful": passed == len(outcomes),
            "summary": {
                "agents": len(outcomes),
                "passed": passed,
                "failed": len(outcomes) - passed,
            },
            "runs": outcomes,
        }
