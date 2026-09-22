FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/github_commit/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/github_commit /app/agents/github_commit
COPY observability /app/observability
USER 65532:65532
EXPOSE 8011
CMD ["python", "-m", "agents.github_commit.app"]
