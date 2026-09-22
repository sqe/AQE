FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/webpage_state_capture/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/webpage_state_capture /app/agents/webpage_state_capture
COPY observability /app/observability
USER 65532:65532
EXPOSE 8002
CMD ["python", "-m", "agents.webpage_state_capture.app"]
