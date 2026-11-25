"""Direct event-source coverage for memory-block revisions."""

from __future__ import annotations

MEMORY_BLOCK_EVENT_LINEAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_block_revision_event_coverage (
    revision_id TEXT PRIMARY KEY REFERENCES memory_block_revision_lineage(revision_id),
    block_id TEXT NOT NULL REFERENCES memory_blocks(block_id),
    source_hash TEXT NOT NULL CHECK (
        length(source_hash) = 64 AND source_hash NOT GLOB '*[^0-9a-f]*'
    )
);
CREATE INDEX IF NOT EXISTS idx_memory_block_revision_event_coverage_block
    ON memory_block_revision_event_coverage(block_id, revision_id);
CREATE TRIGGER IF NOT EXISTS validate_memory_block_revision_event_coverage_insert
BEFORE INSERT ON memory_block_revision_event_coverage
WHEN NOT EXISTS (
    SELECT 1 FROM memory_block_revision_lineage l
    JOIN memory_block_revisions r ON r.revision_id = l.revision_id
    WHERE l.revision_id = NEW.revision_id
      AND l.block_id = NEW.block_id
      AND r.block_id = NEW.block_id
)
BEGIN
    SELECT RAISE(ABORT, 'memory block event coverage identity mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_event_coverage_update
BEFORE UPDATE ON memory_block_revision_event_coverage
BEGIN
    SELECT RAISE(ABORT, 'memory block event coverage is append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_event_coverage_delete
BEFORE DELETE ON memory_block_revision_event_coverage
BEGIN
    SELECT RAISE(ABORT, 'memory block event coverage is append-only');
END;

CREATE TABLE IF NOT EXISTS memory_block_revision_event_sources (
    revision_id TEXT NOT NULL REFERENCES memory_block_revision_event_coverage(revision_id),
    block_id TEXT NOT NULL REFERENCES memory_blocks(block_id),
    source_event_id TEXT NOT NULL REFERENCES events(event_id),
    source_sequence_number INTEGER NOT NULL CHECK (source_sequence_number > 0),
    source_root_hash TEXT NOT NULL CHECK (
        length(source_root_hash) = 64 AND source_root_hash NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY(revision_id, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_block_revision_event_sources_event
    ON memory_block_revision_event_sources(source_event_id, revision_id);
CREATE TRIGGER IF NOT EXISTS validate_memory_block_revision_event_source_insert
BEFORE INSERT ON memory_block_revision_event_sources
WHEN NOT EXISTS (
    SELECT 1 FROM memory_block_revision_event_coverage c
    JOIN memory_blocks b ON b.block_id = c.block_id
    JOIN events e ON e.event_id = NEW.source_event_id
    JOIN event_chain_roots r ON r.event_id = e.event_id
    WHERE c.revision_id = NEW.revision_id
      AND c.block_id = NEW.block_id
      AND b.subject_id = e.subject_id
      AND r.subject_id = e.subject_id
      AND r.sequence_number = NEW.source_sequence_number
      AND r.root_hash = NEW.source_root_hash
)
BEGIN
    SELECT RAISE(ABORT, 'memory block event source mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_event_source_update
BEFORE UPDATE ON memory_block_revision_event_sources
BEGIN
    SELECT RAISE(ABORT, 'memory block event sources are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_event_source_delete
BEFORE DELETE ON memory_block_revision_event_sources
BEGIN
    SELECT RAISE(ABORT, 'memory block event sources are append-only');
END;
"""
