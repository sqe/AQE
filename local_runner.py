import sys
import importlib.util
import inspect
import asyncio
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('LocalRunner')

# --- Mock A2A Server/Agent Classes (Needed to run the server stub) ---
try:
    # Attempt to import real A2A components for the server runner
    from a2a.server import Server as A2AServerRunner
    logger.info("Using real A2A Server Runner.")
except ImportError:
    # Fallback to a mock runner if the SDK is not fully installed or configured
    class A2AServerRunner:
        def __init__(self, agent_class, *args, **kwargs):
            self.agent_class = agent_class
            logger.warning("Using MOCK A2A Server Runner.")
        async def run(self):
            logger.info(f"--- MOCK SERVER STARTED ---")
            logger.info(f"Mock Agent: {self.agent_class.__name__}")
            logger.info(f"Tools available: {', '.join(m for m in dir(self.agent_class) if not m.startswith('_'))}")
            logger.info("Mock server is running. Press Ctrl+C to stop.")
            # Simple async sleep loop to keep the mock server alive
            while True:
                await asyncio.sleep(1)

# --- Execution Logic ---

def find_main_agent_class(module, agent_name: str):
    """Dynamically finds the class meant to be the main agent in the loaded module."""
    
    # 1. Look for a class named exactly like the agent file (e.g., ChangeDetectionAgent)
    expected_class_name = "".join(word.capitalize() for word in agent_name.replace('_agent', '').split('_')) + "Agent"
    if hasattr(module, expected_class_name):
        return getattr(module, expected_class_name)

    # 2. Fallback: Find the first class in the module that contains the required tool method
    for name, obj in inspect.getmembers(module, inspect.isclass):
        # We assume the main agent class has the required tool method (detect_and_update, generate_tests, etc.)
        # and ignore the mocked base classes we imported.
        if hasattr(obj, 'detect_and_update') or hasattr(obj, 'generate_tests') or hasattr(obj, 'execute_tests'):
             # Ensure we don't pick up the mocked classes used in the agent file itself
            if name in ['Agent', 'Environment', 'Client']:
                continue
            return obj
            
    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: python local_runner.py <module.path.to.agent>")
        sys.exit(1)

    module_path = sys.argv[1].replace('.py', '') # e.g., agents.change_detection_agent
    
    try:
        # Load the module dynamically
        spec = importlib.util.find_spec(module_path)
        if spec is None:
            raise ImportError(f"Module not found: {module_path}")
            
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_path] = module
        spec.loader.exec_module(module)

        logger.info(f"--- Starting Agent: {module_path} ---")

        agent_name = module_path.split('.')[-1]
        AgentClass = find_main_agent_class(module, agent_name)

        if AgentClass:
            logger.info(f"Found Agent Class: {AgentClass.__name__}")
            
            # Use environment variables for runtime configuration
            target_url = os.environ.get("TARGET_URL", "http://mock-website.com")
            product_specs = os.environ.get("PRODUCT_SPECS", "Basic product spec for mock run.")
            
            # NOTE: We pass the AgentClass and its args to the server runner.
            # The A2AServerRunner is responsible for creating the Agent instance.
            runner = A2AServerRunner(
                agent_class=AgentClass,
                target_url=target_url,
                product_specs=product_specs
            )
            
            # Start the asynchronous server loop
            asyncio.run(runner.run())

        else:
            logger.error(f"Error: Could not find a runnable agent class in {module_path}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Critical error executing agent module {module_path}: {e}", exc_info=True)
        sys.exit(1)
