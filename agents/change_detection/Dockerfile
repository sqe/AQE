FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/change_detection/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/change_detection /app/agents/change_detection
COPY observability /app/observability
USER 65532:65532
EXPOSE 8000
CMD ["python", "-m", "agents.change_detection.app"]
