from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


@workflow.defn
class QualityEngineeringWorkflow:
    """Durably generate and execute a test run through existing AQE APIs."""

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        generation_request = request
        if request.get("test_type", "agent") == "agent" and request.get("agent_card_url"):
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
            generation_request = {**generation_request, "agent_discovery": agent_discovery}
        if request.get("source_repository"):
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
        return await workflow.execute_activity(
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
