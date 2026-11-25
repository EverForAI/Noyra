"""Versioned memory-block source lineage DDL."""

from __future__ import annotations

MEMORY_BLOCK_LINEAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_block_revision_lineage (
    revision_id TEXT PRIMARY KEY REFERENCES memory_block_revisions(revision_id),
    block_id TEXT NOT NULL REFERENCES memory_blocks(block_id),
    lineage_status TEXT NOT NULL CHECK (
        lineage_status IN ('unknown', 'declared_no_memory_sources', 'memory')
    ),
    lineage_hash TEXT NOT NULL CHECK (
        (lineage_status = 'unknown' AND lineage_hash = '')
        OR (lineage_status != 'unknown' AND length(lineage_hash) = 64
            AND lineage_hash NOT GLOB '*[^0-9a-f]*')
    )
);
CREATE INDEX IF NOT EXISTS idx_memory_block_revision_lineage_block
    ON memory_block_revision_lineage(block_id, revision_id);

-- Existing revisions have no source snapshot. Preserve that fact explicitly;
-- do not infer independence from missing historical lineage.
INSERT INTO memory_block_revision_lineage(revision_id, block_id, lineage_status, lineage_hash)
SELECT r.revision_id, r.block_id, 'unknown', ''
FROM memory_block_revisions r
WHERE NOT EXISTS (
    SELECT 1 FROM memory_block_revision_lineage l WHERE l.revision_id = r.revision_id
);

CREATE TRIGGER IF NOT EXISTS validate_memory_block_revision_lineage_insert
BEFORE INSERT ON memory_block_revision_lineage
WHEN NEW.lineage_status = 'unknown'
  OR NOT EXISTS (
    SELECT 1 FROM memory_block_revisions r
    JOIN memory_blocks b ON b.block_id = r.block_id
    WHERE r.revision_id = NEW.revision_id
      AND r.block_id = NEW.block_id
  )
BEGIN
    SELECT RAISE(ABORT, 'new memory block revision lineage must be declared');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_lineage_update
BEFORE UPDATE ON memory_block_revision_lineage
BEGIN
    SELECT RAISE(ABORT, 'memory block revision lineage is append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_lineage_delete
BEFORE DELETE ON memory_block_revision_lineage
BEGIN
    SELECT RAISE(ABORT, 'memory block revision lineage is append-only');
END;

CREATE TABLE IF NOT EXISTS memory_block_revision_sources (
    revision_id TEXT NOT NULL REFERENCES memory_block_revision_lineage(revision_id),
    block_id TEXT NOT NULL REFERENCES memory_blocks(block_id),
    source_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    source_revision_number INTEGER NOT NULL CHECK (source_revision_number > 0),
    source_revision_hash TEXT NOT NULL CHECK (
        length(source_revision_hash) = 64
        AND source_revision_hash NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY(revision_id, source_memory_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_block_revision_sources_memory
    ON memory_block_revision_sources(source_memory_id, source_revision_number, revision_id);
CREATE TRIGGER IF NOT EXISTS validate_memory_block_revision_source_insert
BEFORE INSERT ON memory_block_revision_sources
WHEN NOT EXISTS (
    SELECT 1 FROM memory_block_revision_lineage l
    JOIN memory_blocks b ON b.block_id = l.block_id
    JOIN memories m ON m.memory_id = NEW.source_memory_id
    JOIN memory_revisions mr ON mr.memory_id = m.memory_id
        AND mr.revision_number = NEW.source_revision_number
    WHERE l.revision_id = NEW.revision_id
      AND l.block_id = NEW.block_id
      AND l.lineage_status = 'memory'
      AND b.subject_id = m.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'memory block source lineage mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_source_update
BEFORE UPDATE ON memory_block_revision_sources
BEGIN
    SELECT RAISE(ABORT, 'memory block revision sources are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_source_delete
BEFORE DELETE ON memory_block_revision_sources
BEGIN
    SELECT RAISE(ABORT, 'memory block revision sources are append-only');
END;
"""
