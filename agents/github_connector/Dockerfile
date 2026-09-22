FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/github_connector/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/github_connector /app/agents/github_connector
COPY observability /app/observability
COPY utils /app/utils
USER 65532:65532
EXPOSE 8014
CMD ["uvicorn", "agents.github_connector.app:app", "--host", "0.0.0.0", "--port", "8014"]
