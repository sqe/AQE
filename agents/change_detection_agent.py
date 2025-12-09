import uvicorn
import logging
import os
import asyncio
import hashlib
import json
import datetime
import httpx
from typing import Dict, Any, Optional

# Starlette/CORS Imports
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.requests import Request

# Async infrastructure clients
import redis.asyncio as redis
from aiokafka import AIOKafkaProducer

# Core A2A Framework Imports (Must be available in Docker environment)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from a2a.utils import new_agent_text_message
from a2a.client import A2AClient 

# 0. Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ChangeDetectionAgent') 

# Kafka Topics for Workflow
TOPIC_TASK_COMMIT = 'qe.task.commit'
TOPIC_EVENT_TEST_RESULT = 'qe.event.test_result'

# Global access to ENV vars used for Agent config (used in custom endpoint)
TARGET_URL = os.environ.get("TARGET_URL", "http://default-target.com")
PRODUCT_SPECS = os.environ.get("PRODUCT_SPECS", "No specs provided.")
REPO_OWNER = os.environ.get("REPO_OWNER", "default-owner") 
REPO_NAME = os.environ.get("REPO_NAME", "default-repo")


# --- 1. Agent Logic (Pure Business Logic) ---

class ChangeDetectionAgentLogic:
    """
    Core business logic for orchestration, client management, caching, 
    and inter-agent communication.
    """
    def __init__(self, target_url: str, product_specs: str, repo_owner: str, repo_name: str):
        self.target_url = target_url
        self.product_specs = product_specs
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        

        # A2A Client is used for synchronous RPC calls to other agents
        self.http_client = httpx.Client()

        self.capture_client = A2AClient(
            self.http_client,
            url="http://webpage-state-capture-agent:8002"
        )

        self.generation_client = A2AClient(
            self.http_client,
            url="http://test-gen-agent:8001") # TestGenerationAgent
        
        self.execution_client = A2AClient(
            self.http_client,
            url="http://test-exec-agent:8005") 

        # Initialize async infrastructure clients lazily
        self.redis_client: Optional[redis.Redis] = None
        self.kafka_producer: Optional[AIOKafkaProducer] = None


    async def _ensure_clients_ready(self):
        """Initializes and ensures all asynchronous clients are connected within the event loop."""
        if not self.redis_client:
            # Connects to the 'redis' service defined in docker-compose.yml
            self.redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            logger.info("Asynchronous Redis client initialized.")

        if not self.kafka_producer:
            # Connects to the 'kafka' service defined in docker-compose.yml
            self.kafka_producer = AIOKafkaProducer(
                bootstrap_servers=['kafka:9092'],
                value_serializer=lambda v: json.dumps(v).encode('utf-8')
            )
            logger.info("Asynchronous Kafka producer initialized.")

        # Start the Kafka producer if not already connected
        try:
            if not self.kafka_producer._started:
                logger.info("Starting AIOKafkaProducer...")
                await self.kafka_producer.start()
        except Exception as e:
            # Handle case where self.kafka_producer is None or has no _started attribute
            logger.error(f"Failed to start Kafka Producer: {e}")
            raise RuntimeError("Kafka producer initialization failed.")


    def _get_cache_key(self) -> str:
        """Generates a cache key based on the target URL and current specs."""
        data_to_hash = f"{self.target_url}-{self.product_specs}"
        return hashlib.sha256(data_to_hash.encode()).hexdigest()

    async def detect_and_update(self) -> Dict[str, Any]:
        """The main orchestration workflow for change detection."""
        
        # CRITICAL: Ensure async clients are ready before proceeding
        try:
            await self._ensure_clients_ready()
        except RuntimeError as e:
            return {"status": "FAILURE", "reason": f"Infrastructure Error: {e}"}


        task_id = self._get_cache_key()
        logger.info(f"--- STARTING QE CYCLE for task_id: {task_id} ---")
        
        # 1. Redis Check: Check for a final cached result
        try:
            cached_result = await self.redis_client.get(f"final_result:{task_id}")
            if cached_result:
                logger.info("1. Redis Hit: Returning cached final test result.")
                return json.loads(cached_result)
        except Exception as e:
            logger.warning(f"Redis lookup failed: {e}. Proceeding with full run.")

        # 2. Recapture State (A2A call via client)
        logger.info("2. Requesting state capture via WebpageStateCaptureAgent...")
        try:
            captured_state = await self.capture_client.call(
                agent_id="WebpageStateCaptureAgent",
                tool_name="capture_state", 
                url=self.target_url
            )
            logger.info(f"   -> State Captured successfully. Element count: {len(captured_state.get('elements', []))}")
        except Exception as e:
            logger.error(f"2. State Capture Failed for {self.target_url}: {e}")
            return {"status": "FAILURE", "reason": f"State Capture Failed: {e}"}

        # 3. Regenerate Tests (A2A call)
        logger.info("3. Generating Test Code via TestGenerationAgent...")
        try:
            generation_response = await self.generation_client.call(
                agent_id="TestGenerationAgent",
                tool_name="generate_tests",
                captured_state=captured_state
            )
        except Exception as e:
            logger.error(f"3. Test Generation Failed: {e}")
            return {"status": "FAILURE", "reason": f"Test Generation Failed: {e}"}
        
        test_code = generation_response.get("test_code", "# Test code unavailable")
        artifact_version = generation_response.get("artifact_version_used", "unknown")

        # 4. Execute and Validate (A2A call)
        logger.info("4. Executing Tests via TestExecutionAgent...")
        try:
            execution_results = await self.execution_client.call(
                agent_id="TestExecutionAgent",
                tool_name="execute_tests",
                test_code=test_code
            )
        except Exception as e:
            logger.error(f"4. Test Execution Failed: {e}")
            test_passed = False
            execution_results = {"successful": False, "summary": f"Execution failed due to A2A error: {e}"}

        test_passed = execution_results.get("successful", False)
        
        # --- Reporting and Cleanup Steps ---
        
        # 5. Asynchronous Test Result Reporting (To PostgreSQL via Kafka consumer)
        reporting_payload = {
            "task_id": task_id,
            "url": self.target_url,
            "timestamp": datetime.datetime.now().isoformat(),
            "result": test_passed,
            "summary": execution_results,
            "artifacts": {"test_code": test_code, "rag_version": artifact_version}
        }
        try:
            logger.info(f"5. Publishing test result event to Kafka topic: {TOPIC_EVENT_TEST_RESULT}. Passed: {test_passed}")
            await self.kafka_producer.send_and_wait(TOPIC_EVENT_TEST_RESULT, value=reporting_payload) 
        except Exception as e:
            logger.error(f"Failed to send result to Kafka: {e}")

        # 6. Conditional Asynchronous Commit (Kafka Producer)
        final_result = {"status": "FAILURE", "url": self.target_url, "execution_summary": execution_results}
        
        if test_passed is True:
            logger.info("6a. Tests PASSED. Publishing commit task to Kafka...")
            commit_payload = {
                "task_id": task_id,
                "repo_owner": self.repo_owner, 
                "repo_name": self.repo_name,
                "test_content": test_code, 
                "commit_message": f"Agentic QE: Add/Update tests for {self.target_url}"
            }
            try:
                await self.kafka_producer.send_and_wait(TOPIC_TASK_COMMIT, value=commit_payload) 
                final_result["status"] = "SUCCESS_PENDING_COMMIT" 
                final_result["commit_status"] = "Task published to Kafka for GitHubCommitAgent."
            except Exception as e:
                logger.error(f"Failed to send commit task to Kafka: {e}")
                final_result["status"] = "SUCCESS_BUT_COMMIT_FAILED"
        else:
            logger.warning("6b. Tests FAILED. Skipping GitHub commit.")
            final_result["status"] = "FAILURE"

        # 7. Final Report & Redis Cache Set
        try:
            await self.redis_client.set(f"final_result:{task_id}", json.dumps(final_result), ex=3600)
            logger.info(f"7. Final result saved to Redis. Status: {final_result['status']}")
        except Exception as e:
            logger.warning(f"Failed to save final result to Redis: {e}")
        
        return final_result


# --- 2. Agent Executor (A2A Protocol Implementation) ---

class ChangeDetectionAgentExecutor(AgentExecutor):
    def __init__(self, target_url: str, product_specs: str, repo_owner: str, repo_name: str):
        self.agent = ChangeDetectionAgentLogic(
            target_url=target_url, 
            product_specs=product_specs,
            repo_owner=repo_owner, 
            repo_name=repo_name     
        )

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        result = await self.agent.detect_and_update()
        message = json.dumps(result, indent=2)
        await event_queue.enqueue_event(new_agent_text_message(f"Change Detection Orchestrator Finished:\n{message}"))
        logger.info("Execution complete and result sent.")

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        logger.warning('Agent does not support cancel.')


# --- Custom Endpoint Handlers (Fixes the 404 issue) ---

async def agent_card_endpoint(request: Request):
    """
    Handles the GET request to /agent_card for health check.
    This is the required route for the orchestrator frontend.
    """
    agent_id = os.environ.get("AGENT_ID", "ChangeDetectionAgent")
    return JSONResponse(
        {"status": "UP", "agent_id": agent_id, "message": "Agent is healthy."}, 
        status_code=200
    )


async def detect_changes_endpoint(request: Request):
    """
    Handles the direct POST request to /detect_changes from the frontend.
    This bypasses the full A2A execute protocol for a simple synchronous API call.
    """
    try:
        logger.info("Received request on custom /detect_changes endpoint.")
        
        # Execute the core logic directly, using the ENV vars for context
        # (This matches how the A2A Executor initializes the logic class)
        logic = ChangeDetectionAgentLogic(
            target_url=TARGET_URL,
            product_specs=PRODUCT_SPECS,
            repo_owner=REPO_OWNER,
            repo_name=REPO_NAME
        )
        
        # Run the detection and orchestration workflow
        result = await logic.detect_and_update()
        
        # Return the result as a standard JSON response
        return JSONResponse(result)
        
    except Exception as e:
        logger.error(f"Error in /detect_changes endpoint: {e}")
        # Return a 500 error if something went wrong internally
        return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)


# --- 3. Server Startup (The Executor that makes the agent runnable) ---

if __name__ == '__main__':
    # Configuration is pulled from the Docker environment variables
    AGENT_PORT = int(os.environ.get("AGENT_PORT", 8000))
    AGENT_ID = os.environ.get("AGENT_ID", "ChangeDetectionAgent")

    # 1. Define the Agent's capabilities (AgentCard)
    skill = AgentSkill(
        id='detect_change_and_orchestrate',
        name='Orchestrate Full QA Change Detection Workflow',
        description='Triggers the workflow to detect changes, generate tests, and run validation.',
        tags=['qa', 'orchestrator', 'async'],
        examples=['check for pricing changes', 'run change detection workflow'],
    )

    agent_card = AgentCard(
        name=AGENT_ID,
        description='Orchestrates QA workflows based on product updates, utilizing Kafka and Redis.',
        url=f'http://0.0.0.0:{AGENT_PORT}/',
        version='1.0.0',
        default_input_modes=['text'],
        default_output_modes=['text'],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill], 
    )

    # 2. Instantiate the Executor with its configuration
    executor = ChangeDetectionAgentExecutor(
        target_url=TARGET_URL, 
        product_specs=PRODUCT_SPECS,
        repo_owner=REPO_OWNER,
        repo_name=REPO_NAME
    )
    
    # 3. Create the Request Handler (A2A server plumbing)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    # 4. Create the A2A Starlette Application and build the ASGI app
    a2a_app_instance = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )
    starlette_app = a2a_app_instance.build()

    # Inject custom routes needed for frontend discovery/interaction
    custom_routes = [
        # Required health check route
        Route("/agent_card", endpoint=agent_card_endpoint, methods=["GET"]),
        # Custom logic route
        Route("/detect_changes", endpoint=detect_changes_endpoint, methods=["POST"])
    ]
    
    # Insert custom routes at the beginning of the A2A routes list
    # Inserting in reverse order ensures they appear first in the final list.
    for route in reversed(custom_routes): 
        starlette_app.routes.insert(0, route)

    # 5. Apply the CORS Middleware
    cors_app = CORSMiddleware(
        app=starlette_app, # Apply middleware to the app with the new routes
        allow_origins=["*"], 
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    logger.info(f"Starting A2A Server for {AGENT_ID} on port {AGENT_PORT}...")
    
    # 6. Run the server using Uvicorn, pointing to the CORS-wrapped app
    uvicorn.run(cors_app, host='0.0.0.0', port=AGENT_PORT)
