-- PostgreSQL Artifact Metadata Setup Script

-- Table: active_artifacts
-- Purpose: Source of truth for tracking the currently active version of critical, large artifacts
-- (e.g., RAG knowledge base, website state snapshots). This table contains the metadata
-- needed to locate the actual artifact data stored in RustFS.
CREATE TABLE IF NOT EXISTS active_artifacts (
-- Defines the type of artifact being tracked (e.g., 'RAG_KNOWLEDGE_BASE', 'WEBSITE_STATE_CAPTURE').
artifact_type VARCHAR(50) PRIMARY KEY,

-- The unique, immutable version identifier generated at upload time (e.g., v-20250930103000-abcd12).
current_version_id VARCHAR(50) NOT NULL,

-- The object key within the configured RustFS bucket.
object_path TEXT NOT NULL,

-- Timestamp of the last time this artifact type was updated/promoted to active.
updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP

);

-- Indexing for fast lookups if querying by update time or path becomes necessary
CREATE INDEX IF NOT EXISTS idx_active_artifacts_updated_at ON active_artifacts (updated_at DESC);

-- Durable source of truth for generated, executed, and repaired test runs.
-- Application services update this table; they must never drop or recreate it.
CREATE TABLE IF NOT EXISTS test_runs (
    task_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    status TEXT NOT NULL,
    generated_by_user_id TEXT,
    timestamp_created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    timestamp_completed TIMESTAMPTZ,
    object_path TEXT NOT NULL,
    url TEXT,
    passed BOOLEAN NOT NULL DEFAULT FALSE,
    summary JSONB,
    execution_results JSONB,
    raw_code TEXT,
    data_artifact_version VARCHAR(50),
    target_agent_id TEXT,
    target_agent_version TEXT,
    target_agent_card_url TEXT,
    target_agent_skills JSONB,
    target_agent_profile JSONB,
    test_catalog JSONB,
    test_type TEXT NOT NULL DEFAULT 'agent',
    finding_disposition TEXT NOT NULL DEFAULT 'untriaged',
    finding_evidence TEXT
);

ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_id TEXT;
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_version TEXT;
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_card_url TEXT;
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_skills JSONB;
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS target_agent_profile JSONB;
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS test_catalog JSONB;
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS test_type TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS finding_disposition TEXT NOT NULL DEFAULT 'untriaged';
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS finding_evidence TEXT;

CREATE INDEX IF NOT EXISTS idx_test_runs_created ON test_runs (timestamp_created DESC);
CREATE INDEX IF NOT EXISTS idx_test_runs_status ON test_runs (status);
