#!/bin/bash
set -e

# This script is the entrypoint for the test_reporting_service worker container.

echo "Starting Test Reporting Kafka Consumer..."

# Execute the Python file containing the Kafka consumer logic.
# Use 'exec' to ensure the Python process is the main process (PID 1), 
# allowing Docker to handle signals (like stopping or restarting) correctly.
exec python3 service/reporing_service.py
