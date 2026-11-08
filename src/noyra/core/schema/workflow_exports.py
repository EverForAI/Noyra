# ruff: noqa: E501 -- versioned SQL layout
"""Versioned export acceptance directory; files never authorize their own acceptance."""

WORKFLOW_EXPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_export_publications (
 publication_id TEXT PRIMARY KEY,
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 workflow_id TEXT NOT NULL,
 source_json TEXT NOT NULL CHECK(length(source_json)<=32000 AND json_valid(source_json)),
 source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
 encryption_key_id TEXT NOT NULL,
 encryption_key_fingerprint TEXT NOT NULL CHECK(length(encryption_key_fingerprint)=64),
 key_generation INTEGER NOT NULL CHECK(key_generation>=1),
 status TEXT NOT NULL CHECK(status IN ('prepared','published_durability_unknown','accepted')),
 manifest_json TEXT CHECK(manifest_json IS NULL OR (length(manifest_json)<=8192 AND json_valid(manifest_json))),
 receipt_event_id TEXT REFERENCES events(event_id),
 state_hash TEXT NOT NULL CHECK(length(state_hash)=64),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(subject_id,workflow_id,source_hash),
 CHECK((status='prepared' AND manifest_json IS NULL AND receipt_event_id IS NULL)
    OR (status='published_durability_unknown' AND manifest_json IS NOT NULL AND receipt_event_id IS NULL)
    OR (status='accepted' AND manifest_json IS NOT NULL AND receipt_event_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_workflow_exports_subject_time
 ON workflow_export_publications(subject_id,created_at DESC,publication_id DESC);
CREATE TRIGGER IF NOT EXISTS preserve_workflow_export_source
BEFORE UPDATE OF publication_id,subject_id,workflow_id,source_json,source_hash,
 encryption_key_id,encryption_key_fingerprint,key_generation,created_at ON workflow_export_publications
BEGIN SELECT RAISE(ABORT,'workflow export source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS preserve_accepted_workflow_export
BEFORE UPDATE ON workflow_export_publications WHEN OLD.status='accepted'
BEGIN SELECT RAISE(ABORT,'accepted workflow export is immutable'); END;
CREATE TRIGGER IF NOT EXISTS preserve_workflow_export_transition
BEFORE UPDATE OF status ON workflow_export_publications
WHEN NOT ((OLD.status='prepared' AND NEW.status IN ('accepted','published_durability_unknown'))
 OR (OLD.status='published_durability_unknown' AND NEW.status='accepted'))
BEGIN SELECT RAISE(ABORT,'workflow export transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS preserve_workflow_export_delete
BEFORE DELETE ON workflow_export_publications
BEGIN SELECT RAISE(ABORT,'workflow export publications cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS validate_workflow_export_subject
BEFORE INSERT ON workflow_export_publications
WHEN NOT EXISTS (SELECT 1 FROM workflow_checkpoints WHERE workflow_id=NEW.workflow_id AND subject_id=NEW.subject_id)
BEGIN SELECT RAISE(ABORT,'workflow export source subject mismatch'); END;
"""
