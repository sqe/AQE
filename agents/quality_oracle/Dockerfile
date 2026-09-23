FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/quality_oracle/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/quality_oracle /app/agents/quality_oracle
COPY observability /app/observability
COPY utils /app/utils
USER 65532:65532
EXPOSE 8017
CMD ["uvicorn", "agents.quality_oracle.app:app", "--host", "0.0.0.0", "--port", "8017"]
