FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/github_analysis/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/github_analysis /app/agents/github_analysis
COPY observability /app/observability
USER 65532:65532
EXPOSE 8010
CMD ["python", "-m", "agents.github_analysis.app"]
