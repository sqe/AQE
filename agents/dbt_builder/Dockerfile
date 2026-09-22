FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agents/dbt_builder/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY agents/dbt_builder /app/agents/dbt_builder
COPY observability /app/observability
COPY utils /app/utils
USER 65532:65532
EXPOSE 8016
CMD ["uvicorn", "agents.dbt_builder.app:app", "--host", "0.0.0.0", "--port", "8016"]
