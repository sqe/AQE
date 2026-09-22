"""Ontology- and RAG-grounded test-generation agent entrypoint."""

import uvicorn
import logging
import os
import asyncio
import asyncpg
import json
import datetime
import uuid
from typing import Dict, Any, Tuple, Optional, List
from qdrant_client import QdrantClient
import httpx 
from utils.agent_ontology import select_ontology_context
from utils.object_store import ObjectStore
from utils.test_types import resolve_test_type

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
from observability.metrics import PrometheusMiddleware

# Configure logging for production tracing
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestGenerationAgent")

# --- Infrastructure Configuration ---
POSTGRES_DB_URL = os.environ.get("POSTGRES_URL", "postgresql://user:pass@postgres:5432/qe_db")

# Placeholder for environment-provided application ID and User ID
APP_ID = os.environ.get("APP_ID", "default-agent-app")
USER_ID = os.environ.get("USER_ID", "default-user") 

# --- Qdrant and RAG Configuration ---
QDRANT_CLIENT = QdrantClient(
    host=os.environ.get("QDRANT_HOST", "qdrant"),
    port=int(os.environ.get("QDRANT_PORT", "6333")),
    api_key=os.environ.get("QDRANT_API_KEY") or None,
    https=os.environ.get("QDRANT_HTTPS", "false").lower() == "true",
)
COLLECTION_NAME = "product_knowledge"

# --- LLM Service Configuration (Dynamic Mode) ---
# Default mode is 'SELF_HOSTED', fallback for Gemini.
LLM_PROVIDER_MODE = os.environ.get("LLM_PROVIDER_MODE", "SELF_HOSTED").upper()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Self-Hosted/LM Studio Endpoints (Only used if LLM_PROVIDER_MODE is 'SELF_HOSTED')
LLM_EMBEDDING_ENDPOINT = os.environ.get("LLM_EMBEDDING_ENDPOINT", "http://192.168.1.3:1234/v1/embeddings")
LLM_GENERATION_ENDPOINT = os.environ.get("LLM_GENERATION_ENDPOINT", "http://192.168.1.3:1234/v1/completions")
GRAPH_API_URL = os.environ.get("GRAPH_API_URL", "http://diagnostics_agent:8006/v1/graph/events")
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
    Handles all infrastructure connections (RustFS, Postgres, Qdrant) and
    orchestrates the RAG and LLM test generation pipeline, now including 
    persistence of the test code artifact.
    """
    def __init__(self):
        self.object_store = ObjectStore()
        self.db_pool: Optional[asyncpg.Pool] = None
        # Initialize the LLM Service Client based on the configured mode
        self.llm_service = LLMServiceClient(LLM_PROVIDER_MODE, GEMINI_API_KEY)
        logger.info("Agent Logic initialized. RustFS and dynamic LLM service clients created.")

    async def init_db_pool(self):
        """
        Initializes the asynchronous PostgreSQL connection pool.
        """
        if not self.db_pool:
            try:
                self.db_pool = await asyncpg.create_pool(POSTGRES_DB_URL)
                
                async with self.db_pool.acquire() as conn:
                    
                    # Schema setup is non-destructive. Production migrations own
                    # schema evolution; an agent must never erase test history.
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS test_runs (
                            -- Use task_id for consistency with Reporting Service, using UUID as PRIMARY KEY
                            task_id TEXT PRIMARY KEY, 
                            
                            -- Agent persistence columns (for initial save and execution update)
                            app_id TEXT NOT NULL,
                            status TEXT NOT NULL,
                            generated_by_user_id TEXT,
                            timestamp_created TIMESTAMPTZ DEFAULT NOW(),
                            timestamp_completed TIMESTAMPTZ,
                            object_path TEXT NOT NULL,
                            execution_results JSONB DEFAULT NULL,

                            -- Reporting service columns (added for robustness if Reporting Service runs first)
                            url TEXT,
                            passed BOOLEAN DEFAULT FALSE,
                            summary JSONB,
                            raw_code TEXT,
                            data_artifact_version VARCHAR(50),
                            target_agent_id TEXT,
                            target_agent_version TEXT,
                            target_agent_card_url TEXT,
                            target_agent_skills JSONB,
                            target_agent_profile JSONB,
                            test_catalog JSONB,
                            test_type TEXT NOT NULL DEFAULT 'agent'
                        );
                    """)
                    await conn.execute("""
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_id TEXT;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_version TEXT;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_card_url TEXT;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_skills JSONB;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_profile JSONB;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS test_catalog JSONB;
                        ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS test_type TEXT NOT NULL DEFAULT 'agent';
                    """)
                    
                    # 2. Also ensure 'active_artifacts' table exists (if we use it)
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS active_artifacts (
                            artifact_type VARCHAR(50) PRIMARY KEY,
                            current_version_id VARCHAR(50) NOT NULL,
                            object_path TEXT NOT NULL,
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
        """Queries PostgreSQL for the active artifact version ID and object path."""
        if not self.db_pool: await self.init_db_pool()
        async with self.db_pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "SELECT current_version_id, object_path FROM active_artifacts WHERE artifact_type = $1",
                    artifact_type
                )
                if not row:
                    raise FileNotFoundError(f"No active artifact found for type: {artifact_type}")
                return row['current_version_id'], row['object_path']
            except Exception as e:
                logger.error(f"Error querying artifact metadata: {e}")
                raise
    
    async def _fetch_artifact(self, object_path: str) -> Dict[str, Any]:
        """Fetches an artifact from RustFS without blocking the event loop."""
        logger.info(f"Fetching artifact from RustFS path: {object_path}")
        def blocking_download():
            return self.object_store.read_bytes(object_path)
        try:
            data_bytes = await asyncio.to_thread(blocking_download)
            return json.loads(data_bytes.decode('utf-8'))
        except Exception as e:
            logger.error(f"RustFS download failed for {object_path}: {e}")
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

    async def _store_test_artifact(self, task_id: str, test_code: str) -> str:
        """
        Stores generated test code in RustFS and returns its object path.
        """
        # Define a consistent path structure for test code artifacts
        object_path = f"artifacts/{APP_ID}/tests/{task_id}/test_code.py"
        data_bytes = test_code.encode('utf-8')
        data_size = len(data_bytes)
        
        logger.info(f"Uploading {data_size} bytes to RustFS path: {object_path}")

        def blocking_upload():
            self.object_store.write_bytes(object_path, data_bytes, "text/x-python")

        try:
            await asyncio.to_thread(blocking_upload)
            return object_path
        except Exception as e:
            logger.error(f"RustFS upload failed for {object_path}: {e}")
            raise

    async def _create_test_run_metadata_in_postgres(
        self,
        task_id: str,
        object_path: str,
        test_spec: str,
        url: str,
        captured_state: Dict[str, Any],
    ) -> None:
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
                    INSERT INTO test_runs (
                        task_id, app_id, status, generated_by_user_id, object_path, url, raw_code,
                        target_agent_id, target_agent_version, target_agent_card_url, target_agent_skills,
                        target_agent_profile, test_type
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb, $13)
                    ON CONFLICT (task_id) DO UPDATE SET 
                        status = EXCLUDED.status, 
                        object_path = EXCLUDED.object_path,
                        url = EXCLUDED.url,
                        raw_code = EXCLUDED.raw_code,
                        target_agent_id = EXCLUDED.target_agent_id,
                        target_agent_version = EXCLUDED.target_agent_version,
                        target_agent_card_url = EXCLUDED.target_agent_card_url,
                        target_agent_skills = EXCLUDED.target_agent_skills,
                        target_agent_profile = EXCLUDED.target_agent_profile,
                        test_type = EXCLUDED.test_type;
                """,
                    task_id,
                    APP_ID,
                    "PENDING", # Initial status
                    USER_ID,   # User ID of the agent that generated the run
                    object_path,
                    url,
                    "# Code is stored in RustFS.",
                    str((captured_state.get("target_agent") or {}).get("id") or APP_ID),
                    str((captured_state.get("target_agent") or {}).get("version") or captured_state.get("agent_version") or "unversioned"),
                    captured_state.get("agent_card_url"),
                    json.dumps(captured_state.get("skills", [])),
                    json.dumps(
                        {
                            key: captured_state[key]
                            for key in (
                                "agent_archetype", "autonomy", "data_sensitivity",
                                "impact", "network_scope", "risk_labels"
                            )
                            if key in captured_state
                        }
                    ),
                    resolve_test_type(captured_state),
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
            
            object_path = await self._store_test_artifact(task_id, test_code)
            
            await self._create_test_run_metadata_in_postgres(
                task_id, object_path, test_spec, target_url, captured_state
            )
            try:
                await self.llm_service.client.post(
                    GRAPH_API_URL,
                    json={
                        "kind": "test_generated",
                        "task_id": task_id,
                        "test_type": resolve_test_type(captured_state),
                        "target_agent": captured_state.get("target_agent") or {},
                        "agent_archetype": captured_state.get("agent_archetype"),
                    },
                    timeout=3,
                )
            except httpx.HTTPError:
                logger.warning("Live graph event could not be delivered", exc_info=True)
            
            # Return the ID and metadata needed by the client/Execution Agent
            return {
                "status": "SUCCESS",
                "task_id": task_id, # Return task_id instead of test_run_id
                "rag_version_id": version_id,
                "object_path": object_path,
                "test_type": resolve_test_type(captured_state),
            }
        except Exception as e:
            error_message = f"# Error during persistence (RustFS/Postgres): {str(e)}"
            logger.error(error_message, exc_info=True)
            return {"status": "FAILED", "error": error_message}
            

    async def generate_tests(self, captured_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generates production-ready Python for one explicitly selected test runtime.
        """
        rag_artifact_type = "RAG_KNOWLEDGE_BASE" 
        version_id = "ERROR" 

        try:
            version_id, object_path = await self._get_artifact_metadata(rag_artifact_type)
            rag_data = await self._fetch_artifact(object_path)

            target_url = captured_state.get('url', 'N/A')
            test_spec = captured_state.get('spec', 'Run general tests.')
            kb_context = captured_state.get('kb', 'No extra knowledge provided.')
            target_agent = captured_state.get("target_agent") or {}
            agent_card_url = captured_state.get("agent_card_url", "")
            agent_skills = captured_state.get("skills", target_agent.get("skills", []))
            test_type = resolve_test_type(captured_state)
            source_analysis = captured_state.get("source_analysis") or {}
            
            query_context = (
                f"Generate test for URL {target_url} based on spec: '{test_spec}'. "
                f"Use this additional context: '{kb_context}'"
            )
            
            product_context_chunks = await self.retrieve_knowledge(query_context)
            product_context = "\n".join([f"- {c}" for c in product_context_chunks])
            ontology_context = "\n".join(
                f"- {record}" for record in select_ontology_context(captured_state)
            )
            
            rag_context_section = f"""
        ***
        PRODUCT CONTEXT (from Qdrant RAG V{version_id}):
        {product_context if product_context else "No specific product knowledge found. Rely on general web automation best practices."}

        APPLICABLE AGENT ONTOLOGY (V1.0.0):
        {ontology_context}
        ***"""
            
            llm_constraint = """
            ***
            TEST GENERATION CONSTRAINTS (CRITICAL for Execution Stability):
            1. **Explicit Waiting (Mandatory):** Always use explicit waiting functions (e.g., `locator.wait_for(state='visible')` or `page.wait_for_selector`) instead of fixed timeouts (`page.wait_for_timeout`).
            2. **Strict Mode Compliance (CRITICAL):** Playwright requires locators to resolve to a SINGLE element. **AVOID** generic locators (like `page.get_by_text("Link Text")`) if the text appears multiple times (e.g., in the header and footer).
            3. **Unique Locators (Mandatory):** For unique and critical elements, use highly specific methods:
                - **Primary Method:** `page.get_by_role("role_name", name="Accessible Name/Text")` (e.g., `page.get_by_role("link", name="Enrich Finance")`).
                - **Secondary Method:** If a general text is the only option, use `page.get_by_text("Text fragment", exact=True).first` or combine it with a unique container, like `page.locator("header").get_by_text("Enrich Finance")`.
            4. **Search Engine Target:** If the test objective involves a search engine for a generic test, **use DuckDuckGo (https://duckduckgo.com/)** instead of Google, as Playwright often gets blocked.
            5. **Atomic Tests:** Every `test_` function verifies exactly one observable outcome with exactly one Python `assert` or Playwright `expect(...)`. Split multiple outcomes into separate tests and share setup through fixtures.
            6. **Do Not Hide Failures:** Never weaken expected values, catch assertion failures, use arbitrary sleeps, or conditionally skip an assertion to make a test pass.
            7. **Isolation:** Tests must not depend on execution order or state left by another test. Use fixtures for setup and cleanup.
            ***"""

            full_prompt = f"""
            You are an expert Python end-to-end test automation engineer.
            Generate a complete executable pytest file for TEST TYPE: {test_type}.
            If TEST TYPE is agent, use httpx and utils.target_auth.authenticated_client; do not
            import Playwright or depend on a browser. Validate the Agent Card, A2A/JSON-RPC/API,
            declared skill, and orchestration contracts.
            If TEST TYPE is website, use Playwright and utils.target_auth browser helpers; test
            observable browser behavior. Use login_with_form only when form auth is configured.
            Do not assume a profession or business domain. Derive behavior only from the supplied
            Agent Card, declared skills, scenario, product evidence, and observable outcomes.
            
            Target URL: {target_url}
            Target Agent: {json.dumps(target_agent, default=str)}
            Agent Card URL: {agent_card_url or "Not supplied"}
            Declared Agent Skills: {json.dumps(agent_skills, default=str)}
            GitHub Source Analysis (candidate evidence, not a confirmed defect):
            {json.dumps(source_analysis, default=str)}
            Test Objective/Specification: {test_spec}
            Additional User Context: {kb_context}
            
            {llm_constraint}

            {rag_context_section}

            Generate version-safe E2E tests for the declared behavior. Validate protocol contracts,
            orchestration handoffs, and domain outcomes without replacing real expected values with mocks.
            When source findings are supplied, design observable black-box tests that could reproduce
            them; never assert that a source candidate is a real defect without runtime evidence.
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
        {
            "status": "UP",
            "agent_id": agent_id,
            "version": "1.0.0",
            "skills": [{"id": "generate_tests"}],
            "message": "Agent is healthy.",
        },
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
            "rag_version_id": result["rag_version_id"],
            "test_type": result["test_type"],
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
        name='Generate Agent or Website Tests via RAG-LLM Pipeline',
        description='Generates isolated HTTP agent tests or Playwright website tests using versioned evidence.',
        tags=['qa', 'llm', 'rag', 'rustfs', 'postgres'],
        examples=['generate tests for the captured state'],
    )

    agent_card = AgentCard(
        name=AGENT_ID,
        description='Generates high-quality, grounded tests using product specs and LLMs.',
        url=f'http://0.0.0.0:{AGENT_PORT}/',
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
    uvicorn.run(PrometheusMiddleware(cors_app, "test-generation"), host='0.0.0.0', port=AGENT_PORT)
