# Use a Python base image
FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# 1. Install ALL necessary system dependencies (Xvfb + Playwright Browsers).
# Installs the full set of dependencies required for Chromium, Firefox, and WebKit on modern Debian.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        xvfb \
        libglib2.0-0 libnss3 libfontconfig1 libasound2 \
        libgtk-3-0 libatk1.0-0 libatk-bridge2.0-0 libgdk-pixbuf-2.0-0 \
        libatspi2.0-0 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libcups2 \
        libdrm2 libgbm1 libxkbcommon0 libwayland-client0 libwayland-egl1 \
        libwayland-server0 libjpeg62-turbo libwebp-dev \
        libwoff1 libharfbuzz-icu0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
        fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Install Playwright browsers (Chromium, Firefox, WebKit)
RUN playwright install

# 4. Copy the test files and the runner script
COPY test.py .
COPY run_tests.sh .

# Make the runner script executable
RUN chmod +x run_tests.sh

# Set the entry point to the runner script. 
ENTRYPOINT ["/app/run_tests.sh"]
