import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import analyze_source, discover_agent, execute_and_repair, generate_tests
from .workflow import QualityEngineeringWorkflow


async def main() -> None:
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "aqe-workflows"),
        workflows=[QualityEngineeringWorkflow],
        activities=[analyze_source, discover_agent, generate_tests, execute_and_repair],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
