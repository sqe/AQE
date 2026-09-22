FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/artifact_management/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/artifact_management /app/agents/artifact_management
COPY observability /app/observability
COPY utils /app/utils
USER 65532:65532
EXPOSE 8007
CMD ["python", "-m", "agents.artifact_management.app"]
