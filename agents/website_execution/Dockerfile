FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TEST_EXECUTION_MODE=website
WORKDIR /app
COPY agents/website_execution/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/test_execution /app/agents/test_execution
COPY agents/website_execution /app/agents/website_execution
COPY observability /app/observability
COPY utils /app/utils
USER 65532:65532
EXPOSE 8013
CMD ["python", "-m", "agents.website_execution.app"]
