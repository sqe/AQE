import uvicorn
import logging
import os
import asyncio
import json
from typing import Dict, Any, Optional
from aiokafka import AIOKafkaConsumer
from github import Github, InputGitAuthor 
from github.GithubException import UnknownObjectException

# Starlette/CORS Imports
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.requests import Request

# Core A2A Framework Imports
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from a2a.utils import new_agent_text_message

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GitHubCommitAgent")

# --- Configuration ---
KAFKA_BROKER = os.environ.get('KAFKA_BROKER', 'kafka:9092')
TOPIC_TASK_COMMIT = 'qe.task.commit' 
GITHUB_TOKEN = os.environ.get("GITHUB_PAT")
TEST_FILE_PATH = "generated_tests/qe_test.py" # Standard location in the repository


# --- 1. Agent Logic (Pure Business Logic) ---

class GitHubCommitAgentLogic:
    """
    Core logic for the GitHub Commit Agent. Initiates a Kafka consumer 
    to listen for commit tasks and executes synchronous GitHub operations 
    using asyncio.to_thread.
    """
    def __init__(self):
        self.github_client: Optional[Github] = self._init_github_client()
        self.consumer: Optional[AIOKafkaConsumer] = None
        logger.info("GitHubCommitAgent Logic initialized.")
        
    def _init_github_client(self) -> Optional[Github]:
        """Initializes the synchronous PyGitHub client."""
        if not GITHUB_TOKEN:
            logger.error("GITHUB_PAT environment variable not set. GitHub operations are disabled.")
            return None
        return Github(GITHUB_TOKEN)

    async def _handle_commit_task(self, task: Dict[str, Any]):
        """Executes the synchronous GitHub logic for a single task."""
        if not self.github_client:
            logger.error(f"Cannot process task {task.get('task_id', 'N/A')}: GitHub client not initialized.")
            return

        task_id = task.get('task_id', 'N/A')
        repo_owner = task['repo_owner']
        repo_name = task['repo_name']
        test_content = task['test_content']
        commit_message = task['commit_message']
        
        try:
            # All PyGitHub calls must be wrapped in asyncio.to_thread
            repo = await asyncio.to_thread(self.github_client.get_user(repo_owner).get_repo, repo_name)
            
            # 1. Check if file exists
            try:
                # Attempt to get file contents (will raise UnknownObjectException if not found)
                contents = await asyncio.to_thread(repo.get_contents, TEST_FILE_PATH)
                
                # File exists, update it
                await asyncio.to_thread(
                    repo.update_file,
                    path=TEST_FILE_PATH,
                    message=commit_message,
                    content=test_content,
                    sha=contents.sha
                )
                logger.info(f"Task {task_id}: Successfully UPDATED file: {TEST_FILE_PATH}")
                
            except UnknownObjectException:
                # File does not exist, create it
                await asyncio.to_thread(
                    repo.create_file,
                    path=TEST_FILE_PATH,
                    message=commit_message,
                    content=test_content
                )
                logger.info(f"Task {task_id}: Successfully CREATED file: {TEST_FILE_PATH}")
            
        except Exception as e:
            # Log the failure but continue consuming other messages
            logger.error(f"FATAL COMMIT ERROR for task {task_id}: {e}", exc_info=True)


    async def start_consumer_loop(self):
        """Initializes and runs the infinite Kafka consumption loop."""
        self.consumer = AIOKafkaConsumer(
            TOPIC_TASK_COMMIT,
            bootstrap_servers=[KAFKA_BROKER],
            group_id="github-commit-group", 
            value_deserializer=lambda m: json.loads(m.decode('utf-8'))
        )
        
        try:
            await self.consumer.start()
            logger.info("Kafka Consumer started. Waiting for commit tasks...")
            
            # Run indefinitely, processing messages as they arrive
            async for msg in self.consumer:
                task = msg.value
                logger.info(f"Received new commit task: {task.get('task_id', 'N/A')}")
                # Process the commit task, handling potential failures internally
                await self._handle_commit_task(task)

        except asyncio.CancelledError:
            logger.info("Kafka consumer loop cancelled (graceful shutdown).")
        except Exception as e:
            logger.error(f"Kafka Consumer encountered a critical error: {e}", exc_info=True)
        finally:
            if self.consumer:
                await self.consumer.stop()
                logger.info("Kafka Consumer stopped.")


# --- 2. Agent Executor (A2A Protocol Implementation) ---

class GitHubCommitAgentExecutor(AgentExecutor):
    """
    Implements the A2A protocol. The execute method initiates the Kafka consumer 
    as a background task.
    """

    def __init__(self):
        self.agent = GitHubCommitAgentLogic()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """
        Starts the long-running Kafka consumer loop in the background.
        """
        if not self.agent.github_client:
             await event_queue.enqueue_event(new_agent_text_message("ERROR: GitHub PAT not set. Commit agent running but cannot execute commits."))
             return
             
        # Create the consumer task without awaiting it. This allows the A2A server 
        # to remain responsive while the consumer runs continuously.
        asyncio.create_task(self.agent.start_consumer_loop())
        
        message = "GitHubCommitAgent consumer started successfully and is running in the background, listening to Kafka topic 'qe.task.commit'."
        await event_queue.enqueue_event(new_agent_text_message(message))
        logger.info(message)


    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # Since the consumer loop is running in a background task, the process 
        # termination (handled by the environment) will be the primary shutdown mechanism.
        logger.warning('Agent is an infinite consumer; graceful shutdown relies on environment termination.')


# --- Custom Endpoint Handlers ---

async def health_endpoint(request: Request):
    """
    Handles the GET request to /health for simple status monitoring.
    """
    return JSONResponse({"status": "UP"}, status_code=200)

async def agent_card_endpoint(request: Request):
    """
    Handles the GET request to /agent_card for health check and metadata.
    """
    agent_id = os.environ.get("AGENT_ID", "GitHubCommitAgent")
    return JSONResponse(
        {"status": "UP", "agent_id": agent_id, "message": "Agent is healthy."}, 
        status_code=200
    )


# --- 3. Server Startup (The Executor that makes the agent runnable) ---

if __name__ == '__main__':
    # Configuration is pulled from the Docker environment variables
    AGENT_PORT = int(os.environ.get("AGENT_PORT", 8006))
    AGENT_ID = os.environ.get("AGENT_ID", "GitHubCommitAgent")

    # 1. Define the Agent's capabilities (AgentCard)
    skill = AgentSkill(
        id='start_commit_consumer',
        name='Start Kafka Commit Listener',
        description='Starts the asynchronous listener for completed, passing test tasks and commits code to GitHub.',
        tags=['kafka', 'github', 'mlops', 'ci/cd'],
        examples=['start commit listener'],
    )

    agent_card = AgentCard(
        name=AGENT_ID,
        description='Asynchronously commits successful test code to a Git repository via Kafka.',
        url=f'http://0.0.0.0:{AGENT_PORT}/',
        version='1.0.0',
        default_input_modes=['text'],
        default_output_modes=['text'],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill], 
    )

    # 2. Instantiate the Executor
    executor = GitHubCommitAgentExecutor()
    
    # 3. Create the Request Handler (A2A server plumbing)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    # 4. Create the A2A Starlette Application and build the ASGI app
    server_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )
    starlette_app = server_app.build() # Capture the built Starlette app

    # Add custom routes needed for monitoring and discovery
    custom_routes = [
        Route("/health", endpoint=health_endpoint, methods=["GET"]), 
        Route("/agent_card", endpoint=agent_card_endpoint, methods=["GET"]),
    ]
    
    # Insert custom routes at the beginning of the A2A routes list
    for route in reversed(custom_routes): 
        starlette_app.routes.insert(0, route)

    # 5. Apply the CORS Middleware
    cors_app = CORSMiddleware(
        app=starlette_app, 
        allow_origins=["*"], 
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    logger.info(f"Starting A2A Server for {AGENT_ID} on port {AGENT_PORT}...")
    
    # 6. Run the server using Uvicorn, pointing to the CORS-wrapped app
    uvicorn.run(cors_app, host='0.0.0.0', port=AGENT_PORT)
