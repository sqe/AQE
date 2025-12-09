#!/bin/bash
# Execute the Python module corresponding to the AGENT_MODULE environment variable.
# This script is the universal entrypoint for all A2A agent containers (which are Uvicorn servers).

set -e

if [ -z "$AGENT_MODULE" ]; then
    echo "Error: AGENT_MODULE environment variable must be set (e.g., agents.change_detection_agent)."
    exit 1
fi

echo "Starting A2A agent: $AGENT_MODULE"
# Executes the Python module, which contains the 'if __name__ == "__main__"' server startup logic
exec python -m $AGENT_MODULE