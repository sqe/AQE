FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TEST_EXECUTION_MODE=agent
WORKDIR /app
COPY agents/test_execution/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/test_execution /app/agents/test_execution
COPY observability /app/observability
COPY utils /app/utils
USER 65532:65532
EXPOSE 8003
CMD ["python", "-m", "agents.test_execution.app"]
