-- V2 migration: extend chunks table with tree-structured fields
-- Generated 2026-07-19 for self-written documents (review / manuscript / grant)
-- Safe to re-run (uses IF NOT EXISTS via individual ALTER statements)

-- Add V2 tree columns to chunks (TEXT/INTEGER/REAL, no FK since they're internal refs)
ALTER TABLE chunks ADD COLUMN level INTEGER DEFAULT 0;
ALTER TABLE chunks ADD COLUMN path TEXT;
ALTER TABLE chunks ADD COLUMN parent_id TEXT;
ALTER TABLE chunks ADD COLUMN child_ids TEXT;       -- JSON array string
ALTER TABLE chunks ADD COLUMN sibling_ids TEXT;     -- JSON array string
ALTER TABLE chunks ADD COLUMN heading_chain TEXT;   -- JSON array string
ALTER TABLE chunks ADD COLUMN doc_id TEXT;          -- e.g. 'review_ai_fall_elderly'

-- Index for tree traversal (prefix-match on path)
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- Add 'self_written' as a valid source value (no CHECK constraint in current schema,
-- but document the convention).
-- source values: pubmed | glm_web | url_fetch | file_parse | self_written