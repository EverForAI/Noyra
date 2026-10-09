"""Explicit privacy operations, separate from routine retention."""

MIGRATION_V87 = """
CREATE TABLE IF NOT EXISTS private_erasure_jobs (
 job_id TEXT PRIMARY KEY,
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 conversation_id TEXT NOT NULL REFERENCES private_conversations(conversation_id),
 status TEXT NOT NULL CHECK(status IN ('pending','running','failed','completed')),
 stage TEXT NOT NULL, error_code TEXT, counts_json TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(subject_id,conversation_id)
);
CREATE TABLE IF NOT EXISTS private_erasure_receipts (
 job_id TEXT NOT NULL REFERENCES private_erasure_jobs(job_id),
 record_table TEXT NOT NULL, record_id TEXT NOT NULL,
 original_hash TEXT NOT NULL, erased_hash TEXT NOT NULL,
 PRIMARY KEY(record_table,record_id)
);
CREATE TRIGGER IF NOT EXISTS prevent_private_erasure_receipt_update
 BEFORE UPDATE ON private_erasure_receipts BEGIN
 SELECT RAISE(ABORT,'privacy erasure receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_private_erasure_receipt_delete
 BEFORE DELETE ON private_erasure_receipts BEGIN
 SELECT RAISE(ABORT,'privacy erasure receipts cannot be deleted'); END;
DROP TRIGGER IF EXISTS prevent_interaction_decision_update;
CREATE TRIGGER prevent_interaction_decision_update BEFORE UPDATE ON interaction_decisions
WHEN NOT EXISTS(SELECT 1 FROM private_erasure_receipts r JOIN private_erasure_jobs j
 ON j.job_id=r.job_id WHERE r.record_table='interaction_decisions'
 AND r.record_id=OLD.decision_id AND j.status='running' AND NEW.rationale='[erased]'
 AND r.original_hash=OLD.state_hash AND EXISTS(SELECT 1 FROM interactions i
 WHERE i.interaction_id=OLD.interaction_id AND i.subject_id=j.subject_id)
 AND NEW.state_hash=r.erased_hash AND NEW.decision_id=OLD.decision_id
 AND NEW.interaction_id=OLD.interaction_id AND NEW.from_status=OLD.from_status
 AND NEW.to_status=OLD.to_status AND NEW.actor=OLD.actor AND NEW.created_at=OLD.created_at)
BEGIN SELECT RAISE(ABORT,'interaction decisions require explicit content erasure'); END;
DROP TRIGGER IF EXISTS prevent_memory_revision_update;
CREATE TRIGGER prevent_memory_revision_update BEFORE UPDATE ON memory_revisions
WHEN NOT EXISTS(SELECT 1 FROM private_erasure_receipts r JOIN private_erasure_jobs j
 ON j.job_id=r.job_id WHERE r.record_table='memory_revisions'
 AND r.record_id=OLD.revision_id AND j.status='running' AND NEW.content='[erased]'
 AND r.original_hash=OLD.content_hash AND EXISTS(SELECT 1 FROM memories m
 WHERE m.memory_id=OLD.memory_id AND m.subject_id=j.subject_id)
 AND NEW.reason='privacy erasure' AND NEW.state_hash=r.erased_hash
 AND NEW.revision_id=OLD.revision_id AND NEW.memory_id=OLD.memory_id
 AND NEW.revision_number=OLD.revision_number AND NEW.salience=OLD.salience
 AND NEW.confidence=OLD.confidence AND NEW.status=OLD.status AND NEW.created_at=OLD.created_at)
BEGIN SELECT RAISE(ABORT,'memory revisions require explicit content erasure'); END;
DROP TRIGGER IF EXISTS prevent_memory_embedding_delete;
CREATE TRIGGER prevent_memory_embedding_delete BEFORE DELETE ON memory_embeddings
WHEN NOT EXISTS(SELECT 1 FROM private_erasure_receipts r JOIN private_erasure_jobs j
 ON j.job_id=r.job_id WHERE r.record_table='memory_embeddings'
 AND r.record_id=OLD.memory_id AND j.status='running' AND j.subject_id=OLD.subject_id)
BEGIN SELECT RAISE(ABORT,'memory embeddings require explicit erasure'); END;
CREATE TRIGGER IF NOT EXISTS prevent_erased_model_payload_update BEFORE UPDATE ON model_calls
WHEN EXISTS(SELECT 1 FROM private_erasure_receipts r WHERE r.record_table='model_calls'
 AND r.record_id=OLD.call_id) AND
 (NEW.request_json IS NOT NULL OR NEW.response_json IS NOT '{"privacy_erased":true}')
BEGIN SELECT RAISE(ABORT,'erased model content cannot be restored'); END;
CREATE TRIGGER IF NOT EXISTS prevent_erased_interaction_update BEFORE UPDATE ON interactions
WHEN EXISTS(SELECT 1 FROM private_erasure_receipts r WHERE r.record_table='interactions'
 AND r.record_id=OLD.interaction_id) AND
 (NEW.content!='[erased]' OR NEW.rationale IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'erased interaction content cannot be restored'); END;
CREATE TRIGGER IF NOT EXISTS prevent_erased_memory_update BEFORE UPDATE ON memories
WHEN EXISTS(SELECT 1 FROM private_erasure_receipts r WHERE r.record_table='memories'
 AND r.record_id=OLD.memory_id) AND
 (NEW.content!='[erased]' OR NEW.status!='archived')
BEGIN SELECT RAISE(ABORT,'erased memory content cannot be restored'); END;
CREATE TRIGGER IF NOT EXISTS prevent_erased_memory_revision_insert
BEFORE INSERT ON memory_revisions
WHEN EXISTS(SELECT 1 FROM private_erasure_receipts r WHERE r.record_table='memories'
 AND r.record_id=NEW.memory_id) AND
 (NEW.content!='[erased]' OR NEW.status!='archived')
BEGIN SELECT RAISE(ABORT,'erased memory revisions cannot be restored'); END;
CREATE TRIGGER IF NOT EXISTS prevent_erased_memory_embedding_insert
BEFORE INSERT ON memory_embeddings
WHEN EXISTS(SELECT 1 FROM private_erasure_receipts r WHERE r.record_table='memories'
 AND r.record_id=NEW.memory_id)
BEGIN SELECT RAISE(ABORT,'erased memory cannot be indexed'); END;
CREATE TRIGGER IF NOT EXISTS prevent_deleted_conversation_restore
BEFORE UPDATE ON private_conversations
WHEN OLD.status='deleted' AND
 (NEW.status!='deleted' OR NEW.title!='[erased]' OR NEW.pinned!=0)
BEGIN SELECT RAISE(ABORT,'deleted conversation cannot be restored'); END;
CREATE TRIGGER IF NOT EXISTS prevent_deleted_window_message_insert
BEFORE INSERT ON private_conversation_messages
WHEN EXISTS(SELECT 1 FROM private_conversations c WHERE c.conversation_id=NEW.conversation_id
 AND c.status IN ('deleting','deleted'))
BEGIN SELECT RAISE(ABORT,'conversation is unavailable'); END;
"""
