import uvicorn
import os
import asyncio
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import uuid
import io

# Starlette/CORS Imports
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.requests import Request

# ML/DB Dependencies
import asyncpg
import minio
from qdrant_client import QdrantClient, models

# Core A2A Framework Imports
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from a2a.utils import new_agent_text_message

# 0. Logging Configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('LLMFineTuningAgent') 

# --- Configuration ---
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = "agentic-qe-artifacts"
POSTGRES_DB_URL = os.environ.get("POSTGRES_URL", "postgresql://user:pass@postgres:5432/qe_db")

QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
COLLECTION_NAME = "product_knowledge"
EMBEDDING_DIMENSION = 384 # Must match the dimension used in TestGenerationAgent

# Mock function for text embedding (Actual model usage would be here)
def embed_text(text: str) -> List[float]:
    """Mock embedding function to simulate text-to-vector conversion (384-dim)."""
    # In a real system, this calls a centralized embedding service (e.g., Gemini API)
    return [0.1] * EMBEDDING_DIMENSION

# --- Internal Artifact Management Logic (Replicated from Artifact Management Agent) ---
class ArtifactManager:
    """Simplified, internal client for MinIO and PostgreSQL metadata management."""
    def __init__(self):
        # MinIO client is created here, but database pool is lazily initialized
        self.minio_client = minio.Minio(
            MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False
        )
        self.db_pool: Optional[asyncpg.Pool] = None

    async def init_db_pool(self):
        if not self.db_pool:
            logger.info("Initializing PostgreSQL connection pool.")
            self.db_pool = await asyncpg.create_pool(POSTGRES_DB_URL)
            
            # --- Ensure the necessary table exists by creating it if absent ---
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
            # --------------------------------------------------------------------------

    async def register_new_artifact_version(self, data_content: str, artifact_type: str) -> str:
        """Uploads metadata to MinIO and registers the new version in PostgreSQL."""
        await self.init_db_pool()
        
        # 1. Generate unique ID and path
        version_id = f"v-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        object_name = f"{artifact_type}/{version_id}/metadata.json"
        
        # 2. Upload metadata to MinIO (using asyncio.to_thread for blocking I/O)
        data_bytes = data_content.encode('utf-8')
        data_stream = io.BytesIO(data_bytes)
        
        def blocking_upload():
            if not self.minio_client.bucket_exists(MINIO_BUCKET):
                 self.minio_client.make_bucket(MINIO_BUCKET)
            self.minio_client.put_object(MINIO_BUCKET, object_name, data_stream, len(data_bytes), content_type="application/json")

        await asyncio.to_thread(blocking_upload)

        # 3. Update PostgreSQL
        async with self.db_pool.acquire() as conn:
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
        return version_id


# --- 1. Agent Logic (Pure Business Logic) ---

class LLMFineTuningAgentLogic:
    """
    Core logic for ingesting product knowledge into Qdrant and registering the version.
    """
    def __init__(self):
        self.qdrant_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.artifact_manager = ArtifactManager()
        logger.info("LLMFineTuningAgent Logic initialized with Qdrant and Artifact Manager.")

    async def ingest_and_register_knowledge(self, product_specs: str) -> Dict[str, str]:
        """
        Embeds product specs into Qdrant and records the new knowledge version.
        """
        if not product_specs:
            return {"status": "FAILED", "error": "Product specifications are missing."}
            
        # 1. Chunk the specs (simple splitting for demo)
        chunks = [chunk.strip() for chunk in product_specs.split('.') if chunk.strip()]
        
        # 2. Qdrant Operations (using asyncio.to_thread for blocking client)
        def blocking_qdrant_ops():
            # Ensure the collection exists with the correct dimension
            self.qdrant_client.recreate_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=models.VectorParams(size=EMBEDDING_DIMENSION, distance=models.Distance.COSINE),
            )
            
            # Embed and upload chunks
            points = []
            for i, chunk in enumerate(chunks):
                # Ensure the mock vector generation is robust
                vector = embed_text(chunk)
                if len(vector) != EMBEDDING_DIMENSION:
                     logger.error("Mock embedding dimension mismatch.")
                     raise ValueError("Embedding dimension mismatch")

                points.append(
                    models.PointStruct(
                        id=i, 
                        vector=vector, 
                        payload={"text_chunk": chunk, "source": "product_knowledge"}
                    )
                )

            self.qdrant_client.upsert(
                collection_name=COLLECTION_NAME,
                points=points,
                wait=True
            )
            return len(chunks)

        try:
            num_chunks = await asyncio.to_thread(blocking_qdrant_ops)
            logger.info(f"Successfully ingested {num_chunks} knowledge chunks into Qdrant.")
        except Exception as e:
            logger.error(f"Qdrant ingestion failed: {e}")
            return {"status": "FAILED", "error": f"Qdrant ingestion error: {e}"}

        # 3. Register the new RAG knowledge version (for auditability by TestGenerationAgent)
        try:
            metadata_content = json.dumps({"description": f"Knowledge base updated with {num_chunks} chunks.", "timestamp": datetime.now().isoformat()})
            new_version_id = await self.artifact_manager.register_new_artifact_version(
                data_content=metadata_content,
                artifact_type="RAG_KNOWLEDGE_BASE"
            )
            logger.info(f"New RAG Version registered: {new_version_id}")
            
            return {
                "status": "SUCCESS", 
                "message": f"Ingested {num_chunks} chunks. New RAG knowledge base version registered: {new_version_id}",
                "rag_version_id": new_version_id
            }
        except Exception as e:
            logger.error(f"Artifact registration failed: {e}")
            return {"status": "FAILED", "error": f"Artifact registration error: {e}"}


# --- 2. Agent Executor (A2A Protocol Implementation - Only used for native A2A requests) ---

class LLMFineTuningAgentExecutor(AgentExecutor):
    """
    This is kept for A2A compliance but is typically bypassed by the custom /ingest_knowledge route.
    """
    def __init__(self):
        self.agent = LLMFineTuningAgentLogic()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        product_specs = context.input_text
        result = await self.agent.ingest_and_register_knowledge(product_specs)
        await event_queue.enqueue_event(new_agent_text_message(json.dumps(result, indent=2)))

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        logger.warning(f'Agent execution cancelled for task {context.task_id}. Ingestion cannot be safely interrupted.')


# --- 3. Custom API Handlers (Fixes the 404 and CORS issues) ---

# Global instance of the agent logic to be used by the custom handlers
AGENT_LOGIC = LLMFineTuningAgentLogic()

# --- NEW: Health Check Endpoint Handler (Fixes the 404 for /agent_card) ---

async def agent_card_endpoint(request: Request):
    """
    Handles the GET request to /agent_card for health check.
    This is the required route for the AQE orchestrator frontend.
    """
    agent_id = os.environ.get("AGENT_ID", "LLMKnowledgeIngestionAgent")
    return JSONResponse(
        {"status": "UP", "agent_id": agent_id, "message": "Agent is healthy."}, 
        status_code=200
    )

async def ingest_knowledge_handler(request: Request):
    """Handles the custom HTTP POST request from the frontend."""
    try:
        body = await request.json()
        product_specs = body.get('input_text')
        
        if not product_specs:
            return JSONResponse({"status": "FAILED", "error": "Missing 'input_text' in request body."}, status_code=400)

        logger.info("Custom API call received for knowledge ingestion.")
        
        # Delegate directly to the core logic
        result = await AGENT_LOGIC.ingest_and_register_knowledge(product_specs)
        
        if result.get("status") == "FAILED":
            # Return the detailed error message in the JSON response
            return JSONResponse(result, status_code=500)

        return JSONResponse(result, status_code=200)

    except Exception as e:
        logger.error(f"Error processing ingest_knowledge request: {e}")
        return JSONResponse({"status": "FAILED", "error": f"Internal server error: {e}"}, status_code=500)


# --- 4. Server Startup (The Executor that makes the agent runnable) ---

if __name__ == '__main__':
    AGENT_PORT = int(os.environ.get("AGENT_PORT", 8004))
    AGENT_ID = os.environ.get("AGENT_ID", "LLMKnowledgeIngestionAgent")

    # 1. Define the Agent's capabilities (AgentCard)
    skill = AgentSkill(
        id='ingest_knowledge',
        name='Ingest Product Knowledge for RAG',
        description='Embeds product specifications and stores them in Qdrant, registering a new RAG knowledge version.',
        tags=['llm', 'rag', 'mlops', 'qdrant'],
        examples=['ingest knowledge: "The main button must be blue and labeled "Submit".'],
    )

    agent_card = AgentCard(
        name=AGENT_ID,
        description='Manages the Qdrant vector store and knowledge versioning for the Test Generation Agent.',
        url=f'http://0.0.0.0:{AGENT_PORT}/',
        version='1.0.0',
        default_input_modes=['text'],
        default_output_modes=['text'],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill], 
    )

    # 2. Instantiate the Executor
    executor = LLMFineTuningAgentExecutor()
    
    # 3. Create the A2A Request Handler
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    # 4. Create the A2A Starlette Application
    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build() # Get the underlying Starlette app

    # 5. Define the custom route for the frontend
    #    Add the new /agent_card health check route here
    routes = [
        Route("/agent_card", endpoint=agent_card_endpoint, methods=["GET"]), # NEW ROUTE ADDED
        Route("/ingest_knowledge", endpoint=ingest_knowledge_handler, methods=["POST"]),
    ]

    # 6. Create the main Starlette application and mount the A2A app
    main_app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"], # Allow all origins for local development simplicity
                allow_methods=["*"],
                allow_headers=["*"],
            )
        ]
    )
    # Mount the A2A application under the default root path
    main_app.mount("/", a2a_app)

    logger.info(f"Starting A2A Server for {AGENT_ID} with CORS enabled on port {AGENT_PORT}...")
    
    # 7. Run the server using Uvicorn
    uvicorn.run(main_app, host='0.0.0.0', port=AGENT_PORT)
