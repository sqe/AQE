"""Website-test execution entrypoint using the shared bounded executor core."""

import os

import uvicorn

from agents.test_execution.app import build_app


app = build_app()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("AGENT_PORT", "8013")))
