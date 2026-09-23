import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import (
    analyze_source,
    discover_agent,
    execute_and_repair,
    generate_tests,
    record_workflow_stage,
    review_generated_tests,
)
from .workflow import FleetQualityEngineeringWorkflow, QualityEngineeringWorkflow


async def main() -> None:
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE") or "aqe-workflows",
        workflows=[QualityEngineeringWorkflow, FleetQualityEngineeringWorkflow],
        activities=[
            analyze_source,
            discover_agent,
            generate_tests,
            review_generated_tests,
            execute_and_repair,
            record_workflow_stage,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
