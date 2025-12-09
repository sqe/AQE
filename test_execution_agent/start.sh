#!/bin/bash
#
# Wrapper script to start Xvfb (Virtual Frame Buffer) 
# before executing the main Python agent.
# This is necessary for non-headless web testing (Playwright).

echo "Starting Xvfb on display $DISPLAY..."
# Run Xvfb in the background. :99 is the display set in the Dockerfile.
Xvfb $DISPLAY -screen 0 1280x1024x24 -ac +extension GLX +render -noreset &
XVFBPID=$!
echo "Xvfb started with PID: $XVFBPID"

# Wait a moment for Xvfb to initialize
sleep 1

# Execute the main agent script
echo "Starting test execution agent..."
python /app/test_execution_agent.py

# Capture the exit code of the Python script
AGENT_EXIT_CODE=$?

# Shut down Xvfb when the Python script exits
echo "Agent finished. Shutting down Xvfb (PID: $XVFBPID)..."
kill $XVFBPID

# Exit the wrapper script with the agent's original exit code
exit $AGENT_EXIT_CODE
