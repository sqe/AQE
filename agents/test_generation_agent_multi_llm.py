import uvicorn
import logging
import os
import asyncio
import asyncpg
import minio
import json
import datetime
import uuid
from typing import Dict, Any, Tuple, Optional, List
from qdrant_client import QdrantClient
import httpx 

# Starlette/CORS Imports
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.requests import Request

# Core A2A Framework Imports (Must be available in Docker environment)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from a2a.utils import new_agent_text_message

# Configure logging for production tracing
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestGenerationAgent")

# --- Infrastructure Configuration ---
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = "agentic-qe-artifacts"
POSTGRES_DB_URL = os.environ.get("POSTGRES_URL", "postgresql://user:pass@postgres:5432/qe_db")

# Placeholder for environment-provided application ID and User ID
APP_ID = os.environ.get("APP_ID", "default-agent-app")
USER_ID = os.environ.get("USER_ID", "default-user") 

# --- Qdrant and RAG Configuration ---
QDRANT_CLIENT = QdrantClient(host=os.environ.get("QDRANT_HOST", "qdrant"), port=6333) 
COLLECTION_NAME = "product_knowledge"

# --- LLM Service Configuration (Dynamic Mode) ---
# Default mode is 'SELF_HOSTED', fallback for Gemini.
LLM_PROVIDER_MODE = os.environ.get("LLM_PROVIDER_MODE", "SELF_HOSTED").upper()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Self-Hosted/LM Studio Endpoints (Only used if LLM_PROVIDER_MODE is 'SELF_HOSTED')
LLM_EMBEDDING_ENDPOINT = os.environ.get("LLM_EMBEDDING_ENDPOINT", "http://192.168.1.3:1234/v1/embeddings")
LLM_GENERATION_ENDPOINT = os.environ.get("LLM_GENERATION_ENDPOINT", "http://192.168.1.3:1234/v1/completions")
EMBEDDING_DIMENSION = 384 

# Gemini API Constants 
GEMINI_EMBEDDING_MODEL = "text-embedding-004"
GEMINI_GENERATION_MODEL = "gemini-2.5-flash-preview-05-20"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class LLMServiceClient:
    """Handles all asynchronous communication with the LLM service, supporting Gemini and Self-Hosted modes."""
    def __init__(self, mode: str, api_key: str):
        self.mode = mode
        self.api_key = api_key 
        self.client = httpx.AsyncClient(timeout=60.0, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))
        logger.info(f"LLM Service Client initialized in mode: {mode}")

    async def close(self):
        """Closes the underlying httpx client connection pool."""
        await self.client.aclose()

    async def get_embedding(self, text: str) -> List[float]:
        """Calls the configured embedding model asynchronously."""
        if self.mode == 'GEMINI':
            url = f"{GEMINI_API_BASE_URL}/models/{GEMINI_EMBEDDING_MODEL}:embedContent?key={self.api_key}"
            payload = {"model": GEMINI_EMBEDDING_MODEL, "content": {"parts": [{"text": text}]}}
            
            try:
                response = await self.client.post(url, json=payload)
                response.raise_for_status()
                return response.json()['embedding']['values']
            except Exception as e:
                logger.error(f"Gemini Embedding error: {e}")
                raise
        
        else: # SELF_HOSTED
            try:
                response = await self.client.post(LLM_EMBEDDING_ENDPOINT, json={"input": text})
                response.raise_for_status()
                result_json = response.json()
                if 'data' in result_json and result_json['data']:
                    return result_json['data'][0].get("embedding", [0.0] * EMBEDDING_DIMENSION)
                
                return result_json.get("vector", [0.0] * EMBEDDING_DIMENSION)
            except Exception as e:
                logger.error(f"Self-Hosted Embedding error: {e}")
                raise

    async def generate_code(self, prompt: str) -> str:
        """Calls the configured generation model asynchronously."""
        if self.mode == 'GEMINI':
            url = f"{GEMINI_API_BASE_URL}/models/{GEMINI_GENERATION_MODEL}:generateContent?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.1}
            }
            
            try:
                response = await self.client.post(url, json=payload)
                response.raise_for_status()
                candidate = response.json().get('candidates', [{}])[0]
                return candidate.get('content', {}).get('parts', [{}])[0].get('text', "# Gemini generation failed.")
            except Exception as e:
                logger.error(f"Gemini Generation error: {e}")
                raise

        else: # SELF_HOSTED
            try:
                # Common V1 OpenAI style API payload for completions (used by LM Studio)
                payload = {
                    "prompt": prompt, 
                    "max_tokens": 2048, 
                    "temperature": 0.1,
                }
                
                response = await self.client.post(LLM_GENERATION_ENDPOINT, json=payload)
                response.raise_for_status()
                result_json = response.json()
                # Assuming the self-hosted model returns 'choices[0].text'
                text = result_json.get("choices", [{}])[0].get("text", "# LLM generation failed or returned empty.")
                return text
            
            except Exception as e:
                logger.error(f"Self-Hosted Generation error: {e}")
                raise


# --- 1. Agent Logic (Pure Business Logic) ---

class TestGenerationAgentLogic:
    """
    Handles all infrastructure connections (MinIO, Postgres, Qdrant) and 
    orchestrates the RAG and LLM test generation pipeline, now including 
    persistence of the test code artifact.
    """
    def __init__(self):
        self.minio_client: minio.Minio = minio.Minio(
            MINIO_ENDPOINT, 
            access_key=MINIO_ACCESS_KEY, 
            secret_key=MINIO_SECRET_KEY, 
            secure=False
        )
        self.db_pool: Optional[asyncpg.Pool] = None
        # Initialize the LLM Service Client based on the configured mode
        self.llm_service = LLMServiceClient(LLM_PROVIDER_MODE, GEMINI_API_KEY)
        logger.info("Agent Logic initialized. MinIO and dynamic LLM service clients created.")

    async def init_db_pool(self):
        """
        Initializes the asynchronous PostgreSQL connection pool.
        """
        if not self.db_pool:
            try:
                self.db_pool = await asyncpg.create_pool(POSTGRES_DB_URL)
                
                async with self.db_pool.acquire() as conn:
                    
                    # 1. Force the creation of the 'test_runs' table with the latest schema
                    await conn.execute("DROP TABLE IF EXISTS test_runs;")
                    await conn.execute("""
                        CREATE TABLE test_runs (
                            -- Use task_id for consistency with Reporting Service, using UUID as PRIMARY KEY
                            task_id TEXT PRIMARY KEY, 
                            
                            -- Agent persistence columns (for initial save and execution update)
                            app_id TEXT NOT NULL,
                            status TEXT NOT NULL,
                            generated_by_user_id TEXT,
                            timestamp_created TIMESTAMPTZ DEFAULT NOW(),
                            minio_path TEXT NOT NULL,
                            execution_results JSONB DEFAULT NULL,

                            -- Reporting service columns (added for robustness if Reporting Service runs first)
                            url TEXT,
                            passed BOOLEAN DEFAULT FALSE,
                            summary JSONB,
                            raw_code TEXT,
                            data_artifact_version VARCHAR(50) 
                        );
                    """)
                    
                    # 2. Also ensure 'active_artifacts' table exists (if we use it)
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS active_artifacts (
                            artifact_type VARCHAR(50) PRIMARY KEY,
                            current_version_id VARCHAR(50) NOT NULL,
                            minio_path TEXT NOT NULL,
                            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    
                logger.info("PostgreSQL connection pool established and schemas verified (test_runs forced updated).")
            except Exception as e:
                logger.error(f"Failed to connect to PostgreSQL or initialize schema: {e}")
                raise

    async def tear_down(self):
        """Gracefully close resources upon agent shutdown."""
        if self.db_pool:
            await self.db_pool.close()
            logger.info("PostgreSQL connection pool closed.")
        await self.llm_service.close() 
        logger.info("LLM Service client closed.")
            
    async def _get_artifact_metadata(self, artifact_type: str) -> Tuple[str, str]:
        """Queries PostgreSQL for the active artifact version ID and MinIO path."""
        if not self.db_pool: await self.init_db_pool()
        async with self.db_pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "SELECT current_version_id, minio_path FROM active_artifacts WHERE artifact_type = $1",
                    artifact_type
                )
                if not row:
                    raise FileNotFoundError(f"No active artifact found for type: {artifact_type}")
                return row['current_version_id'], row['minio_path']
            except Exception as e:
                logger.error(f"Error querying artifact metadata: {e}")
                raise
    
    async def _fetch_artifact_from_minio(self, minio_path: str) -> Dict[str, Any]:
        """Fetches the large artifact file from MinIO using asyncio.to_thread for non-blocking I/O."""
        logger.info(f"Fetching artifact from MinIO path: {minio_path}")
        def blocking_download():
            response = self.minio_client.get_object(MINIO_BUCKET, minio_path)
            data = response.read()
            response.close()
            response.release_conn()
            return data
        try:
            data_bytes = await asyncio.to_thread(blocking_download)
            return json.loads(data_bytes.decode('utf-8'))
        except Exception as e:
            logger.error(f"MinIO download failed for {minio_path}: {e}")
            raise

    async def retrieve_knowledge(self, query: str) -> List[str]:
        """Queries Qdrant for knowledge semantically similar to the current task."""
        # 1. Embed the query
        query_vector = await self.llm_service.get_embedding(query)

        # 2. Search Qdrant
        def blocking_search():
            return QDRANT_CLIENT.search(
                collection_name=COLLECTION_NAME,
                query_vector=query_vector,
                limit=3
            )

        search_result = await asyncio.to_thread(blocking_search)
        return [hit.payload['text_chunk'] for hit in search_result if hit.payload and 'text_chunk' in hit.payload]

    async def _store_test_artifact_in_minio(self, task_id: str, test_code: str) -> str:
        """
        Stores the generated test code as a file in MinIO.
        Returns the MinIO object path.
        """
        # Define a consistent path structure for test code artifacts
        minio_path = f"artifacts/{APP_ID}/tests/{task_id}/test_code.py"
        data_bytes = test_code.encode('utf-8')
        data_size = len(data_bytes)
        
        logger.info(f"Uploading {data_size} bytes to MinIO path: {minio_path}")

        def blocking_upload():
            from io import BytesIO
            data_stream = BytesIO(data_bytes)
            self.minio_client.put_object(
                bucket_name=MINIO_BUCKET,
                object_name=minio_path,
                data=data_stream,
                length=data_size,
                content_type='text/x-python'
            )

        try:
            await asyncio.to_thread(blocking_upload)
            return minio_path
        except Exception as e:
            logger.error(f"MinIO upload failed for {minio_path}: {e}")
            raise

    async def _create_test_run_metadata_in_postgres(self, task_id: str, minio_path: str, test_spec: str, url: str) -> None:
        """
        Creates the initial PENDING metadata entry in PostgreSQL.
        Uses task_id for consistency with reporting service.
        """
        if not self.db_pool: await self.init_db_pool()

        async with self.db_pool.acquire() as conn:
            # We use the public data collection path structure as a convention
            app_specific_path = f"/artifacts/{APP_ID}/public/data/test_runs/{task_id}"
            try:
                await conn.execute("""
                    INSERT INTO test_runs (task_id, app_id, status, generated_by_user_id, minio_path, url, raw_code)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (task_id) DO UPDATE SET 
                        status = EXCLUDED.status, 
                        minio_path = EXCLUDED.minio_path,
                        url = EXCLUDED.url,
                        raw_code = EXCLUDED.raw_code;
                """,
                    task_id,
                    APP_ID,
                    "PENDING", # Initial status
                    USER_ID,   # User ID of the agent that generated the run
                    minio_path,
                    url,
                    "# Code will be loaded from MinIO, but this is a placeholder."
                )
                logger.info(f"PostgreSQL metadata created for task_id: {task_id}")
            except Exception as e:
                logger.error(f"PostgreSQL insert failed for {task_id}: {e}")
                raise


    async def generate_tests_and_persist(self, captured_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main orchestration method: generates code, persists it, and returns the ID.
        """
        # 1. Generate Test Code (using existing RAG/LLM logic)
        generation_result = await self.generate_tests(captured_state)
        test_code = generation_result.get("test_code")
        version_id = generation_result.get("artifact_version_used")
        test_spec = captured_state.get('spec', 'General Test')
        target_url = captured_state.get('url', 'N/A')

        # Check if the generation failed before attempting persistence
        if test_code.startswith("# Error:"):
            return {"status": "FAILED", "error": test_code}

        # 2. Persistence Layer
        try:
            # Generate a unique ID for this execution run, using the consistent name task_id
            task_id = str(uuid.uuid4())
            
            # Save the generated code to MinIO
            minio_path = await self._store_test_artifact_in_minio(task_id, test_code)
            
            # Save the metadata (including the MinIO path) to Postgres
            await self._create_test_run_metadata_in_postgres(task_id, minio_path, test_spec, target_url)
            
            # Return the ID and metadata needed by the client/Execution Agent
            return {
                "status": "SUCCESS",
                "task_id": task_id, # Return task_id instead of test_run_id
                "rag_version_id": version_id,
                "minio_path": minio_path
            }
        except Exception as e:
            error_message = f"# Error during persistence (MinIO/Postgres): {str(e)}"
            logger.error(error_message, exc_info=True)
            return {"status": "FAILED", "error": error_message}
            

    async def generate_tests(self, captured_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generates production-ready Playwright Python test code.
        The prompt is updated here to enforce strict mode compliance.
        """
        rag_artifact_type = "RAG_KNOWLEDGE_BASE" 
        version_id = "ERROR" 

        try:
            version_id, minio_path = await self._get_artifact_metadata(rag_artifact_type)
            rag_data = await self._fetch_artifact_from_minio(minio_path)

            target_url = captured_state.get('url', 'N/A')
            test_spec = captured_state.get('spec', 'Run general tests.')
            kb_context = captured_state.get('kb', 'No extra knowledge provided.')
            
            query_context = (
                f"Generate test for URL {target_url} based on spec: '{test_spec}'. "
                f"Use this additional context: '{kb_context}'"
            )
            
            product_context_chunks = await self.retrieve_knowledge(query_context)
            product_context = "\n".join([f"- {c}" for c in product_context_chunks])
            
            rag_context_section = f"""
        ***
        PRODUCT CONTEXT (from Qdrant RAG V{version_id}):
        {product_context if product_context else "No specific product knowledge found. Rely on general web automation best practices."}
        ***"""
            
            # --- FIX: Constraint updated to enforce unique locators and avoid strict mode violations ---
            llm_constraint = """
            ***
            TEST GENERATION CONSTRAINTS (CRITICAL for Execution Stability):
            1. **Explicit Waiting (Mandatory):** Always use explicit waiting functions (e.g., `locator.wait_for(state='visible')` or `page.wait_for_selector`) instead of fixed timeouts (`page.wait_for_timeout`).
            2. **Strict Mode Compliance (CRITICAL):** Playwright requires locators to resolve to a SINGLE element. **AVOID** generic locators (like `page.get_by_text("Link Text")`) if the text appears multiple times (e.g., in the header and footer).
            3. **Unique Locators (Mandatory):** For unique and critical elements, use highly specific methods:
                - **Primary Method:** `page.get_by_role("role_name", name="Accessible Name/Text")` (e.g., `page.get_by_role("link", name="Enrich Finance")`).
                - **Secondary Method:** If a general text is the only option, use `page.get_by_text("Text fragment", exact=True).first` or combine it with a unique container, like `page.locator("header").get_by_text("Enrich Finance")`.
            4. **Search Engine Target:** If the test objective involves a search engine for a generic test, **use DuckDuckGo (https://duckduckgo.com/)** instead of Google, as Playwright often gets blocked.
            ***"""
            # --- END FIX ---

            full_prompt = f"""
            You are an expert Playwright Python Test Automation Engineer.
            Your task is to generate a complete, executable Python file using Playwright.
            
            Target URL: {target_url}
            Test Objective/Specification: {test_spec}
            Additional User Context: {kb_context}
            
            {llm_constraint}

            {rag_context_section}

            Generate the test code that verifies critical user flows and UI compliance based on the context.
            The response must be *only* the Python code block.
            """

            test_code = await self.llm_service.generate_code(full_prompt)
            
            # Simple check to strip any surrounding markdown, common in LLM responses
            if test_code.strip().startswith("```python"):
                test_code = test_code.strip().replace("```python", "").replace("```", "").strip()

            return {
                "test_code": test_code,
                "artifact_version_used": version_id
            }

        except FileNotFoundError:
            error_message = f"# Error: RAG artifact {rag_artifact_type} not found in DB. Cannot generate grounded tests."
            return {
                "test_code": error_message,
                "artifact_version_used": "UNAVAILABLE"
            }
        except Exception as e:
            error_message = f"# Error: Failed during generation pipeline (RAG Version: {version_id}). Details: {str(e)}" 
            logger.error(error_message, exc_info=True)
            return {
                "test_code": error_message,
                "artifact_version_used": version_id
            }


# --- 2. Agent Executor (A2A Protocol Implementation) ---

class TestGenerationAgentExecutor(AgentExecutor): 
    """
    Implements the A2A protocol methods (execute, cancel) and delegates 
    to the TestGenerationAgentLogic.
    """

    def __init__(self):
        # Instantiate the Agent Logic class
        self.agent = TestGenerationAgentLogic()
        logger.info("TestGenerationAgentExecutor initialized.")

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        # For A2A protocol, input arguments are used.
        captured_state = context.input_args.get("captured_state")
        
        if not captured_state or not isinstance(captured_state, dict):
            error_message = "Execution failed: Missing or invalid 'captured_state' argument in request."
            logger.error(error_message)
            await event_queue.enqueue_event(new_agent_text_message(error_message))
            return

        # Call the persistence orchestration method
        result = await self.agent.generate_tests_and_persist(captured_state)
        
        # Send the final result back to the user/caller via the EventQueue
        message = json.dumps(result, indent=2)
        await event_queue.enqueue_event(new_agent_text_message(f"Test Generation Complete:\n{message}"))
        logger.info("Test generation complete and result sent.")

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # Gracefully shut down clients if necessary
        await self.agent.tear_down()
        logger.warning('Agent shut down during cancellation.')


# --- 3. Custom Endpoint Handlers ---

# Global instance of the agent logic to be used by the custom handlers
AGENT_LOGIC = TestGenerationAgentLogic()

async def health_endpoint(request: Request):
    logger.debug("/health endpoint accessed.")
    return JSONResponse({"status": "UP"}, status_code=200)

async def agent_card_endpoint(request: Request):
    logger.debug("/agent_card endpoint accessed.")
    agent_id = os.environ.get("AGENT_ID", "TestGenerationAgent")
    return JSONResponse(
        {"status": "UP", "agent_id": agent_id, "message": "Agent is healthy."}, 
        status_code=200
    )


async def generate_tests_handler(request: Request):
    """
    Handles the custom HTTP POST request, generates tests, and persists the run.
    It returns the task_id.
    """
    try:
        body = await request.json()
        logger.info(f"Received JSON body for test generation: {body}")
        
        target_url = body.get('url')
        if not target_url:
            return JSONResponse({"status": "FAILED", "error": "Missing 'url'."}, status_code=400)

        # Call the new orchestration method
        result = await AGENT_LOGIC.generate_tests_and_persist(body)
        
        if result["status"] == "FAILED":
            return JSONResponse({
                "status": "FAILED", 
                "error_details": result["error"]
            }, status_code=500)

        # Success: Return the ID so the client can tell the Execution Agent which artifact to run
        return JSONResponse({
            "status": "SUCCESS", 
            "task_id": result["task_id"], # Return task_id
            "rag_version_id": result["rag_version_id"]
        }, status_code=200)

    except json.JSONDecodeError:
        logger.error("Error decoding JSON request body.")
        return JSONResponse({"status": "FAILED", "error": "Invalid JSON format."}, status_code=400)
    except Exception as e:
        logger.error(f"Error processing generate_tests request: {e}", exc_info=True)
        return JSONResponse({"status": "FAILED", "error": f"Internal server error: {e}"}, status_code=500)


# --- 4. Server Startup (The Executor that makes the agent runnable) ---

if __name__ == '__main__':
    # Configuration is pulled from the Docker environment variables
    AGENT_PORT = int(os.environ.get("AGENT_PORT", 8001))
    AGENT_ID = os.environ.get("AGENT_ID", "TestGenerationAgent")

    # 1. Define the Agent's capabilities (AgentCard)
    skill = AgentSkill(
        id='generate_tests',
        name='Generate Playwright Tests via RAG-LLM Pipeline',
        description='Generates executable Playwright Python tests using a versioned RAG system.',
        tags=['qa', 'llm', 'rag', 'minio', 'postgres'],
        examples=['generate tests for the captured state'],
    )

    agent_card = AgentCard(
        name=AGENT_ID,
        description='Generates high-quality, grounded tests using product specs and LLMs.',
        url=f'[http://0.0.0.0](http://0.0.0.0):{AGENT_PORT}/',
        version='1.0.0',
        default_input_modes=['args'],
        default_output_modes=['text'],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill], 
    )

    # 2. Instantiate the Executor
    executor = TestGenerationAgentExecutor()
    
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
    starlette_app = server_app.build()

    # Define the custom routes required by the client/orchestrator
    custom_routes = [
        Route("/health", endpoint=health_endpoint, methods=["GET", "OPTIONS"]), 
        Route("/agent_card", endpoint=agent_card_endpoint, methods=["GET", "OPTIONS"]),
        Route("/generate_test_plan", endpoint=generate_tests_handler, methods=["POST", "OPTIONS"]),
    ]
    
    for route in reversed(custom_routes): 
        starlette_app.routes.insert(0, route)

    # 5. Apply the CORS Middleware - this must wrap the entire application.
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
