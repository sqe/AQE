import uvicorn
import logging
import os
import json
import asyncio
import asyncpg
import minio
from datetime import datetime
from typing import Dict, Any, Optional
import io
import uuid

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
logger = logging.getLogger("ArtifactManagementAgent")

# --- Configuration (using environment variables for production) ---
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = "agentic-qe-artifacts"
POSTGRES_DB_URL = os.environ.get("POSTGRES_URL", "postgresql://user:pass@postgres:5432/qe_db")

# --- 1. Agent Logic (Pure Business Logic) ---

class ArtifactManagementAgentLogic:
    """
    Handles versioning, storage (MinIO), and metadata (PostgreSQL) for large artifacts.
    """
    def __init__(self):
        # MinIO Python SDK is blocking. We rely on asyncio.to_thread() for safe I/O.
        self.minio_client: Optional[minio.Minio] = None
        self.db_pool: Optional[asyncpg.Pool] = None
        
        self._init_clients()
        logger.info("ArtifactManagementAgent Logic initialized.")

    def _init_clients(self):
        """Initializes synchronous MinIO client."""
        try:
            self.minio_client = minio.Minio(
                MINIO_ENDPOINT,
                access_key=MINIO_ACCESS_KEY,
                secret_key=MINIO_SECRET_KEY,
                secure=False
            )
            # Verify bucket existence (blocking call, done once at init)
            if not self.minio_client.bucket_exists(MINIO_BUCKET):
                logger.warning(f"MinIO bucket {MINIO_BUCKET} not found. Creating it.")
                self.minio_client.make_bucket(MINIO_BUCKET)
            logger.info("MinIO client initialized and bucket verified.")
        except Exception as e:
            logger.error(f"Failed to initialize MinIO client: {e}")
            self.minio_client = None

    async def init_db_pool(self):
        """Initialize the asynchronous PostgreSQL connection pool and ensure schema."""
        if not self.db_pool:
            try:
                self.db_pool = await asyncpg.create_pool(POSTGRES_DB_URL)
                logger.info("PostgreSQL connection pool created.")

                # Ensure the necessary 'active_artifacts' table exists
                async with self.db_pool.acquire() as conn:
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS active_artifacts (
                            artifact_type VARCHAR(255) PRIMARY KEY,
                            current_version_id VARCHAR(255) NOT NULL,
                            minio_path TEXT NOT NULL,
                            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                logger.info("PostgreSQL 'active_artifacts' table ensured to exist.")

            except Exception as e:
                logger.error(f"Failed to connect to PostgreSQL or create schema: {e}")
                raise

    async def tear_down(self):
        """Gracefully close resources."""
        if self.db_pool:
            await self.db_pool.close()
            logger.info("PostgreSQL connection pool closed.")

    async def upload_and_register_artifact(self, data_content: str, artifact_type: str, file_extension: str = "json") -> str:
        """
        Uploads the artifact data to MinIO and registers its version in PostgreSQL.
        Returns the new version ID.
        """
        # Ensure pool is initialized (and schema is created)
        await self.init_db_pool() 
        
        if not self.minio_client:
            raise ConnectionError("MinIO client is not initialized or failed to connect.")

        # 1. Generate a unique, auditable version ID
        version_id = f"v-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        object_name = f"{artifact_type}/{version_id}/data.{file_extension}"
        
        logger.info(f"Uploading new artifact version: {version_id} for type {artifact_type}")

        # 2. Upload to MinIO using asyncio.to_thread for safe blocking I/O
        data_bytes = data_content.encode('utf-8')
        data_stream = io.BytesIO(data_bytes)
        
        def blocking_upload():
            self.minio_client.put_object(
                MINIO_BUCKET,
                object_name,
                data_stream,
                len(data_bytes),
                content_type=f"application/{file_extension}"
            )

        try:
            await asyncio.to_thread(blocking_upload)
        except Exception as e:
            logger.error(f"MinIO upload failed for {object_name}: {e}")
            raise

        # 3. Update PostgreSQL as the Source of Truth (asyncpg)
        async with self.db_pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO active_artifacts (artifact_type, current_version_id, minio_path)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (artifact_type) DO UPDATE SET 
                        current_version_id = EXCLUDED.current_version_id, 
                        minio_path = EXCLUDED.minio_path,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    artifact_type, version_id, object_name
                )
            except Exception as e:
                logger.error(f"PostgreSQL registration failed for {version_id}: {e}")
                raise
        
        logger.info(f"Artifact {version_id} successfully registered at path: {object_name}")
        return version_id

# --- 2. Agent Executor (A2A Protocol Implementation) ---

class ArtifactManagementAgentExecutor(AgentExecutor):
    """
    Implements the A2A protocol and exposes artifact management logic as a skill.
    """

    def __init__(self):
        self.agent = ArtifactManagementAgentLogic()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """
        The main entry point, executing the upload_and_register_artifact skill.
        """
        args = context.input_args
        
        data_content = args.get("data_content")
        artifact_type = args.get("artifact_type")
        file_extension = args.get("file_extension", "json")
        
        if not data_content or not artifact_type:
            error_message = "Execution failed: Missing required arguments 'data_content' or 'artifact_type'."
            logger.error(error_message)
            await event_queue.enqueue_event(new_agent_text_message(error_message))
            return
            
        await event_queue.enqueue_event(new_agent_text_message(f"Starting upload for artifact type: {artifact_type}..."))

        try:
            new_version_id = await self.agent.upload_and_register_artifact(
                data_content=data_content,
                artifact_type=artifact_type,
                file_extension=file_extension
            )
            
            result = {"new_version_id": new_version_id, "artifact_type": artifact_type}
            
            message = json.dumps(result, indent=2)
            await event_queue.enqueue_event(new_agent_text_message(f"Artifact Management Complete:\n{message}"))
            logger.info(f"Artifact upload complete. Version ID: {new_version_id}")

        except Exception as e:
            error_message = f"Artifact management failed critically: {e}"
            logger.error(error_message, exc_info=True)
            await event_queue.enqueue_event(new_agent_text_message(f"ERROR: {error_message}"))

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        await self.agent.tear_down()
        logger.warning('Artifact Management Agent torn down.')


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
    agent_id = os.environ.get("AGENT_ID", "ArtifactManagementAgent")
    return JSONResponse(
        {"status": "UP", "agent_id": agent_id, "message": "Agent is healthy."}, 
        status_code=200
    )


# --- 3. Server Startup (The Executor that makes the agent runnable) ---

if __name__ == '__main__':
    # Configuration is pulled from the Docker environment variables
    AGENT_PORT = int(os.environ.get("AGENT_PORT", 8007))
    AGENT_ID = os.environ.get("AGENT_ID", "ArtifactManagementAgent")

    # 1. Define the Agent's capabilities (AgentCard)
    skill = AgentSkill(
        id='upload_artifact',
        name='Upload and Version Artifact',
        description='Uploads data to MinIO, generates a version ID, and registers the metadata in PostgreSQL as the active version.',
        tags=['minio', 'postgres', 'versioning', 'storage'],
        parameters={
            "data_content": {"type": "string", "description": "The artifact content (e.g., JSON string) to be stored."},
            "artifact_type": {"type": "string", "description": "The identifier for the artifact (e.g., 'RAG_KNOWLEDGE_BASE')."},
            "file_extension": {"type": "string", "description": "Optional: File extension (default: 'json').", "optional": True}
        },
        returns={"type": "object", "description": "The newly created version ID of the artifact."},
        examples=['upload artifact data content "..." with type "RAG_KNOWLEDGE_BASE"']
    )

    agent_card = AgentCard(
        name=AGENT_ID,
        description='Manages version control and storage for large data artifacts (RAG, state captures).',
        url=f'http://0.0.0.0:{AGENT_PORT}/',
        version='1.0.0',
        default_input_modes=['args'],
        default_output_modes=['text'],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill], 
    )

    # 2. Instantiate the Executor
    executor = ArtifactManagementAgentExecutor()
    
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
        Route("/health", endpoint=health_endpoint, methods=["GET"]), # <-- NEW Health Route
        Route("/agent_card", endpoint=agent_card_endpoint, methods=["GET"]),
    ]
    
    # Insert custom routes at the beginning of the A2A routes list
    for route in reversed(custom_routes): 
        starlette_app.routes.insert(0, route)

    # 5. Apply the CORS Middleware
    cors_app = CORSMiddleware(
        app=starlette_app, # Apply middleware to the app with the new route
        allow_origins=["*"], 
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    logger.info(f"Starting A2A Server for {AGENT_ID} on port {AGENT_PORT}...")
    
    # 6. Run the server using Uvicorn, pointing to the CORS-wrapped app
    uvicorn.run(cors_app, host='0.0.0.0', port=AGENT_PORT)
