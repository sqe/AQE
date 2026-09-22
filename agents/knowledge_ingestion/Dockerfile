FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/knowledge_ingestion/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/knowledge_ingestion /app/agents/knowledge_ingestion
COPY observability /app/observability
COPY utils /app/utils
COPY ontology /app/ontology
USER 65532:65532
EXPOSE 8004
CMD ["python", "-m", "agents.knowledge_ingestion.app"]
