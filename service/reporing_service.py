import os
import json
import asyncio
from aiokafka import AIOKafkaConsumer
import psycopg2 
from datetime import datetime
from typing import Dict, Any, Optional
import logging 

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('TestReportingService') 

# Configuration
KAFKA_BROKER = os.environ.get('KAFKA_BROKER', 'kafka:9092')
POSTGRES_DB_URL = os.environ.get("POSTGRES_URL", "postgresql://user:pass@postgres:5432/qe_db")

# Topic Definition
TOPIC_EVENT_TEST_RESULT = 'qe.event.test_result' 

def create_table_if_not_exists(conn: psycopg2.extensions.connection):
    """
    Ensures the test_runs history table exists in PostgreSQL with a unified, 
    robust schema that matches all agents' requirements.
    """
    cursor = conn.cursor()
    try:
        cursor.execute("""
            -- UNIFIED SCHEMA (Matching Test Generation Agent)
            CREATE TABLE IF NOT EXISTS test_runs (
                task_id TEXT PRIMARY KEY,
                
                -- Core fields required by Test Generation Agent for initial persistence
                app_id TEXT NOT NULL,
                status TEXT NOT NULL,
                minio_path TEXT NOT NULL,
                
                generated_by_user_id TEXT, 
                timestamp_created TIMESTAMPTZ DEFAULT NOW(),
                
                -- Fields primarily updated by Reporting Service or Execution Agent
                url TEXT,
                raw_code TEXT,
                execution_results JSONB,
                
                -- Reporting service fields
                timestamp TIMESTAMP WITH TIME ZONE, -- Execution completion time
                passed BOOLEAN DEFAULT FALSE,
                summary JSONB,
                data_artifact_version VARCHAR(50) 
            );
        """)
        conn.commit()
        logger.info("PostgreSQL table 'test_runs' confirmed/created with unified schema.")
    except Exception as e:
        conn.rollback()
        logger.error(f"ERROR creating table: {e}")
    finally:
        cursor.close()

async def consume_reporting_events():
    """
    Consumes test result events from Kafka and performs durable writes to PostgreSQL.
    """
    conn: Optional[psycopg2.extensions.connection] = None
    
    try:
        # Establish PostgreSQL connection
        conn = psycopg2.connect(POSTGRES_DB_URL)
        create_table_if_not_exists(conn)
    except Exception as e:
        logger.critical(f"Failed to connect to PostgreSQL at {POSTGRES_DB_URL}. {e}")
        return

    # Initialize AIOKafka Consumer
    consumer = AIOKafkaConsumer(
        TOPIC_EVENT_TEST_RESULT,
        bootstrap_servers=KAFKA_BROKER,
        group_id="reporting-db-group",
        value_deserializer=lambda m: json.loads(m.decode('utf-8'))
    )
    
    await consumer.start()
    logger.info(f"Kafka Consumer started on topic {TOPIC_EVENT_TEST_RESULT}. Ingesting events...")
    
    try:
        async for msg in consumer:
            report: Dict[str, Any] = msg.value
            
            task_id = report.get('task_id')
            if not task_id:
                logger.warning("Skipping message with missing task_id.")
                continue

            # Extract data safely
            url = report.get('url', 'N/A')
            timestamp = report.get('timestamp', datetime.now().isoformat())
            passed = report.get('result', False)
            summary = report.get('summary', {})
            raw_code = report.get('artifacts', {}).get('test_code', '')
            artifact_version = report.get('artifacts', {}).get('rag_version', 'unknown')
            
            try:
                # Use a context manager for the cursor for safety
                with conn.cursor() as cursor:
                    # SQL statement now uses task_id for conflict resolution
                    cursor.execute(
                        """
                        INSERT INTO test_runs (task_id, url, timestamp, passed, summary, raw_code, data_artifact_version)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (task_id) DO UPDATE SET 
                            timestamp = EXCLUDED.timestamp, 
                            passed = EXCLUDED.passed, 
                            summary = EXCLUDED.summary,
                            raw_code = EXCLUDED.raw_code,
                            data_artifact_version = EXCLUDED.data_artifact_version;
                        """,
                        (
                            task_id,
                            url,
                            timestamp,
                            passed,
                            json.dumps(summary),
                            raw_code,
                            artifact_version
                        )
                    )
                conn.commit()
                logger.info(f"Successfully recorded test run: {task_id} (Passed: {passed}, RAG V: {artifact_version})")
                
            except Exception as e:
                # If DB write fails, roll back the transaction before logging error
                conn.rollback()
                logger.error(f"DB INGESTION ERROR for task {task_id}: {e}")
                
    finally:
        await consumer.stop()
        if conn:
            conn.close()

if __name__ == "__main__":
    # Ensure this consumer process runs continuously
    asyncio.run(consume_reporting_events())
