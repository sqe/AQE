-- PostgreSQL Artifact Metadata Setup Script

-- Table: active_artifacts
-- Purpose: Source of truth for tracking the currently active version of critical, large artifacts
-- (e.g., RAG knowledge base, website state snapshots). This table contains the metadata
-- needed to locate the actual artifact data stored in MinIO (Object Storage).
CREATE TABLE IF NOT EXISTS active_artifacts (
-- Defines the type of artifact being tracked (e.g., 'RAG_KNOWLEDGE_BASE', 'WEBSITE_STATE_CAPTURE').
artifact_type VARCHAR(50) PRIMARY KEY,

-- The unique, immutable version identifier generated at upload time (e.g., v-20250930103000-abcd12).
current_version_id VARCHAR(50) NOT NULL,

-- The full path to the artifact file in MinIO (e.g., 'agentic-qe-artifacts/RAG_KB/v-123/data.json').
minio_path TEXT NOT NULL,

-- Timestamp of the last time this artifact type was updated/promoted to active.
updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP

);

-- Indexing for fast lookups if querying by update time or path becomes necessary
CREATE INDEX IF NOT EXISTS idx_active_artifacts_updated_at ON active_artifacts (updated_at DESC);