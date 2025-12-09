import uvicorn
import os
import json
import asyncio
import hashlib
import logging
import redis.asyncio as redis
from playwright.async_api import async_playwright
from typing import List, Dict, Any, Optional

# Starlette/CORS Imports
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route 

# Core A2A Framework Imports
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from a2a.utils import new_agent_text_message

# Configure logging for production tracing
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WebpageStateCaptureAgent")

# --- Configuration ---
CACHE_EXPIRATION_SECONDS = 300 # Cache state for 5 minutes
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))


# --- 1. Agent Logic (Pure Business Logic) ---

class WebpageStateCaptureAgentLogic:
    """
    Core logic for capturing web page state using Playwright and caching the 
    results in Redis. This class contains no A2A server implementation details.
    """
    def __init__(self):
        # Initialize async Redis client (connection happens on first use)
        self.redis_client: Optional[redis.Redis] = None
        logger.info("Agent Logic initialized.")

    async def _ensure_redis_ready(self):
        """Initializes the Redis client connection asynchronously."""
        if not self.redis_client:
            # Connects to the 'redis' service defined in docker-compose.yml
            self.redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            logger.info("Asynchronous Redis client initialized.")

    async def capture_state(self, url: str) -> Dict[str, Any]:
        """
        Navigates to a URL, extracts interactive elements, and handles caching.
        """
        await self._ensure_redis_ready()

        url_hash = hashlib.sha256(url.encode()).hexdigest()
        cache_key = f"web_state:{url_hash}"
        
        # 1. Redis Check: Attempt to retrieve cached state (ASYNCHRONOUSLY)
        try:
            cached_state = await self.redis_client.get(cache_key)
            if cached_state:
                logger.info(f"Cache Hit for URL: {url}")
                return json.loads(cached_state)
        except Exception as e:
            logger.warning(f"Redis check failed: {e}. Proceeding without cache.")
            
        logger.info(f"Cache Miss for URL: {url}. Starting Playwright capture...")
        
        # 2. Playwright Capture (Runs only on cache miss)
        try:
            # Note: We must launch Playwright inside the async function scope
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                # Use a higher timeout for potential slow loading
                await page.goto(url, wait_until='networkidle', timeout=30000) 

                # --- JavaScript Injection to Extract Elements ---
                elements_data = await page.evaluate('''() => {
                    const selectors = 'button, input, a, [role="button"], [role="link"]';
                    return Array.from(document.querySelectorAll(selectors)).map(el => ({
                        tag: el.tagName.toLowerCase(),
                        id: el.id,
                        name: el.getAttribute('name') || '',
                        text: el.textContent ? el.textContent.trim().substring(0, 50) : 'N/A',
                        type: el.getAttribute('type') || el.getAttribute('role') || 'N/A',
                        selector: el.tagName.toLowerCase() + (el.id ? `#${el.id}` : '')
                    }));
                }''')
                # --- End JavaScript Injection ---
                
                await browser.close()
        except Exception as e:
            logger.error(f"Playwright Capture Failed for {url}: {e}")
            return {"url": url, "elements": [], "status": "PLAYWRIGHT_ERROR", "error_detail": str(e)}


        # 3. Final Result and Redis Set
        state_data = {
            "url": url,
            "elements": elements_data,
            "status": "CAPTURED"
        }
        
        # Set the result back to Redis (ASYNCHRONOUSLY)
        try:
            await self.redis_client.set(cache_key, json.dumps(state_data), ex=CACHE_EXPIRATION_SECONDS)
            logger.info(f"State captured and saved to Redis.")
        except Exception as e:
            logger.warning(f"Failed to save to Redis: {e}")

        return state_data


# --- 2. Agent Executor (A2A Protocol Implementation) ---

class WebpageStateCaptureAgentExecutor(AgentExecutor):
    # ... (A2A logic remains unchanged) ...
    def __init__(self):
        # Instantiate the Agent Logic class
        self.agent_logic = WebpageStateCaptureAgentLogic()
        logger.info("Agent Executor initialized.")

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """
        The main entry point, expecting 'url' in the input arguments.
        """
        
        # Expecting input arguments via A2A Tool call, e.g.,
        # { "url": "http://example.com" }
        url = context.input_args.get("url")
        
        if not url or not isinstance(url, str):
            error_message = "Execution failed: Missing or invalid 'url' argument in request."
            logger.error(error_message)
            await event_queue.enqueue_event(new_agent_text_message(error_message))
            return

        result = await self.agent_logic.capture_state(url)
        
        # Send the final result back to the user/caller via the EventQueue
        message = json.dumps(result, indent=2)
        await event_queue.enqueue_event(new_agent_text_message(message))
        logger.info("State capture complete and result sent.")

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # Currently no heavy I/O to close in the logic, but this method is required.
        logger.warning(f'Agent execution cancelled for task {context.task_id}.')

# --- NEW: Health Check Endpoint Handler (Fixes the 404 for /agent_card) ---

async def agent_card_endpoint(request):
    """
    Handles the GET request to /agent_card for health check.
    This is the required route for the AQE orchestrator frontend.
    """
    agent_id = os.environ.get("AGENT_ID", "WebpageStateCaptureAgent")
    return JSONResponse(
        {"status": "UP", "agent_id": agent_id, "message": "Agent is healthy."}, 
        status_code=200
    )


# --- Existing Custom Endpoint Handler (/capture) ---

async def capture_endpoint(request):
    """
    Handles the direct POST request to /capture from the frontend.
    This bypasses the full A2A execute protocol for a simple synchronous API call.
    """
    try:
        # 1. Get JSON data from the request body
        data = await request.json()
        url = data.get("url")
        
        if not url:
            return JSONResponse({"error": "Missing 'url' in payload."}, status_code=400)
        
        # 2. Execute the core logic directly
        # Note: A new logic instance is created here, which is fine since it's lightweight
        logic = WebpageStateCaptureAgentLogic() 
        result = await logic.capture_state(url)
        
        # 3. Return the result as a standard JSON response
        return JSONResponse(result)
        
    except Exception as e:
        logger.error(f"Error in /capture endpoint: {e}")
        # Return a 500 error if something went wrong internally (e.g., JSON parsing fails)
        return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)


# --- 3. Server Startup (The Executor that makes the agent runnable) ---

if __name__ == '__main__':
    # Configuration is pulled from the Docker environment variables
    AGENT_PORT = int(os.environ.get("AGENT_PORT", 8002))
    AGENT_ID = os.environ.get("AGENT_ID", "WebpageStateCaptureAgent")

    # 1. Define the Agent's capabilities (AgentCard)
    skill = AgentSkill(
        id='capture_state',
        name='Capture Interactive Web Element State',
        description='Navigates to a URL, extracts interactive elements (buttons, inputs, links) using Playwright, and caches the result.',
        tags=['web', 'playwright', 'caching', 'state'],
        examples=['capture_state url="https://example.com"'],
    )

    agent_card = AgentCard(
        name=AGENT_ID,
        description='Provides real-time state snapshots of web pages for analysis and test generation.',
        url=f'http://0.0.0.0:{AGENT_PORT}/',
        version='1.0.0',
        default_input_modes=['args'],
        default_output_modes=['text'],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill], 
    )

    # 2. Instantiate the Executor
    executor = WebpageStateCaptureAgentExecutor()
    
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

    # --- Custom ROUTING ---
    
    # Add the required /agent_card health check route 
    agent_card_route = Route("/agent_card", endpoint=agent_card_endpoint, methods=["GET"])
    starlette_app.routes.insert(0, agent_card_route) # Insert as the first route
    
    # Add the custom /capture route that the frontend is expecting
    custom_route = Route("/capture", endpoint=capture_endpoint, methods=["POST"])
    
    # Insert the custom POST route after the GET health check route
    starlette_app.routes.insert(1, custom_route)

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
