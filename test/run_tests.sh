#!/bin/bash

# run_tests.sh

# Display number for Xvfb
export DISPLAY=:99

# Function to handle cleanup on exit
cleanup() {
    echo "Stopping Xvfb..."
    # Check if the process ID is set and running before attempting to kill
    if [ ! -z "${XVFB_PID}" ] && kill -0 "${XVFB_PID}" 2>/dev/null; then
        kill "$XVFB_PID"
    fi
    exit
}

# Trap signals (like Ctrl+C or container stop) to ensure cleanup runs
trap cleanup SIGINT SIGTERM EXIT

echo "Starting Xvfb on display $DISPLAY..."
# Start Xvfb (X Virtual Framebuffer) in the background
# -ac allows anyone to connect (no access control)
# -screen 0 1280x1024x24 sets the screen resolution and color depth
Xvfb $DISPLAY -ac -screen 0 1280x1024x24 &

# Store the Process ID of Xvfb
XVFB_PID=$!

echo "Waiting for Xvfb to start..."
# Wait a moment for Xvfb to initialize
sleep 2

# Set Playwright environment variables to force non-headless mode
# This tells Playwright to render the browser to the virtual display set above.
export PLAYWRIGHT_HEADLESS=0

echo "Running pytest with non-headless browser..."
# Execute pytest and capture its exit code
# The browser will render to the virtual screen provided by Xvfb.
/usr/local/bin/pytest test.py
TEST_EXIT_CODE=$?

# The trap signal EXIT will handle the cleanup, 
# but we explicitly exit with the pytest status code.
exit $TEST_EXIT_CODE
