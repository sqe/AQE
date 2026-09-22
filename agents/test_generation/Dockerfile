FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/test_generation/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/test_generation /app/agents/test_generation
COPY observability /app/observability
COPY utils /app/utils
COPY ontology /app/ontology
USER 65532:65532
EXPOSE 8001
CMD ["python", "-m", "agents.test_generation.app"]
