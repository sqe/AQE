FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/agent_builder/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/agent_builder /app/agents/agent_builder
COPY agents/test_execution/test_quality.py /app/agents/test_execution/test_quality.py
COPY observability /app/observability
COPY utils /app/utils
USER 65532:65532
EXPOSE 8015
CMD ["uvicorn", "agents.agent_builder.app:app", "--host", "0.0.0.0", "--port", "8015"]
