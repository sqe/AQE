# # Use a slim Python image as a base for faster builds
# FROM python:3.11-slim

# # Set environment variables
# ENV DEBIAN_FRONTEND noninteractive
# ENV PYTHONUNBUFFERED 1

# # --- Install System Dependencies ---
# RUN apt-get update \
#     && apt-get install -y \
#     bash \
#     build-essential \
#     libnss3 \
#     libgbm-dev \
#     libpq-dev \
#     fonts-liberation \
#     libappindicator3-1 \
#     libasound2 \
#     libatk-bridge2.0-0 \
#     libcurl4 \
#     libfontconfig1 \
#     libgtk-3-0 \
#     libpangocairo-1.0-0 \
#     libxkbcommon0 \
#     libxtst6 \
#     --no-install-recommends \
#     && apt-get clean \
#     && rm -rf /var/lib/apt/lists/*

# # Set the working directory
# WORKDIR /app

# # 1. Install Dependencies
# # Copy the root requirements.txt (Requires: context: . in docker-compose.yml)
# COPY requirements.txt /app/
# RUN pip install --no-cache-dir -r requirements.txt

# # 2. Install Playwright's Browser Drivers
# RUN playwright install --with-deps

# # 3. CRITICAL FIX: Copy Source Code for Runtime Imports
# # This ensures that any tests or utility imports (like from 'agents' or 'utils')
# # can be found by the test runner when it executes the temporary test file.
# COPY agents/ /app/agents/
# COPY utils/ /app/utils/
# COPY service/ /app/service/

# # Copy the agent's specific requirements.txt (if it has unique, small dependencies)
# COPY test_execution_agent/requirements.txt /app/test_execution_agent_requirements.txt 
# RUN pip install --no-cache-dir -r /app/test_execution_agent_requirements.txt || true

# # Copy the main execution file to the root of the Docker environment's app directory
# COPY test_execution_agent/test_execution_agent.py /app/test_execution_agent.py

# # Expose the agent port
# EXPOSE 8003

# # Command to run the agent
# CMD ["python", "test_execution_agent.py"]


FROM python:3.11-slim

# Set environment variables
ENV DEBIAN_FRONTEND noninteractive
ENV PYTHONUNBUFFERED 1
# CRITICAL: Set the DISPLAY variable for Xvfb. This allows non-headless browser execution.
ENV DISPLAY :99

# --- Install System Dependencies ---
RUN apt-get update \
    && apt-get install -y \
    # WEB TESTING REQUIREMENTS (Including Xvfb)
    xvfb \
    libnss3 \
    libgbm-dev \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libfontconfig1 \
    libgtk-3-0 \
    libpangocairo-1.0-0 \
    libxkbcommon0 \
    libxtst6 \
    # General Agent Dependencies
    bash \
    build-essential \
    libpq-dev \
    libappindicator3-1 \
    libcurl4 \
    --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# 1. Install Dependencies
# Ensure 'pytest-playwright' is in this root requirements.txt file!
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 2. Install Playwright's Browser Drivers
# --with-deps should now successfully pull system deps from the installed list
RUN playwright install --with-deps

# 3. Copy Source Code for Runtime Imports
COPY agents/ /app/agents/
COPY utils/ /app/utils/
COPY service/ /app/service/

# Copy the agent's specific requirements.txt (if it has unique, small dependencies)
COPY test_execution_agent/requirements.txt /app/test_execution_agent_requirements.txt 
RUN pip install --no-cache-dir -r /app/test_execution_agent_requirements.txt || true

# Copy the main execution file to the root of the Docker environment's app directory
COPY test_execution_agent/test_execution_agent.py /app/test_execution_agent.py

# Copy the startup script (Source path now includes the directory prefix)
COPY test_execution_agent/start.sh /app/start.sh
# Make the script executable
RUN chmod +x /app/start.sh
# ------------------------------------

# Expose the agent port
EXPOSE 8003

# Command to run the agent - NOW USES THE WRAPPER SCRIPT
CMD ["/app/start.sh"]
