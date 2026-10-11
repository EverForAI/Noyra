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

MIGRATION_V88 = """
CREATE TRIGGER IF NOT EXISTS validate_private_erasure_job_owner
BEFORE INSERT ON private_erasure_jobs
WHEN NOT EXISTS(SELECT 1 FROM private_conversations c
 WHERE c.conversation_id=NEW.conversation_id AND c.subject_id=NEW.subject_id)
BEGIN SELECT RAISE(ABORT,'privacy deletion subject mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_private_erasure_job_identity_update
BEFORE UPDATE ON private_erasure_jobs
WHEN NEW.job_id!=OLD.job_id OR NEW.subject_id!=OLD.subject_id
 OR NEW.conversation_id!=OLD.conversation_id OR NEW.created_at!=OLD.created_at
 OR (OLD.status='completed' AND (NEW.status!='completed'
 OR NEW.stage!='verified' OR NEW.counts_json!=OLD.counts_json OR NEW.error_code IS NOT NULL))
BEGIN SELECT RAISE(ABORT,'privacy deletion identity and completion are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_private_erasure_job_delete
BEFORE DELETE ON private_erasure_jobs
BEGIN SELECT RAISE(ABORT,'privacy deletion intent cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS validate_private_erasure_receipt_job
BEFORE INSERT ON private_erasure_receipts
WHEN NOT EXISTS(SELECT 1 FROM private_erasure_jobs j
 WHERE j.job_id=NEW.job_id AND j.status='running')
BEGIN SELECT RAISE(ABORT,'privacy erasure receipt requires running intent'); END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_revision_source_erasure_change
BEFORE UPDATE ON memory_revisions
WHEN NEW.source_event_ids_json!=OLD.source_event_ids_json
BEGIN SELECT RAISE(ABORT,'memory erasure cannot change provenance'); END;
"""

MIGRATION_V89 = """
CREATE TABLE IF NOT EXISTS inbound_identity_challenges (
 challenge_id TEXT PRIMARY KEY,
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 transport_id TEXT NOT NULL REFERENCES interaction_transports(transport_id),
 code_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','observed','confirmed','expired','revoked')),
 external_account_id TEXT,
 external_sender_id TEXT,
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
 expires_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 observed_at TEXT,
 confirmed_at TEXT,
 observed_event_hash TEXT,
 binding_id TEXT REFERENCES interaction_bindings(binding_id),
 UNIQUE(subject_id, transport_id, code_hash)
);
CREATE INDEX IF NOT EXISTS idx_inbound_identity_challenges_lookup
 ON inbound_identity_challenges(subject_id, transport_id, status, expires_at);
CREATE TRIGGER IF NOT EXISTS validate_inbound_identity_challenge_owner
 BEFORE INSERT ON inbound_identity_challenges WHEN NOT EXISTS(
 SELECT 1 FROM interaction_transports t WHERE t.transport_id=NEW.transport_id
 AND t.subject_id=NEW.subject_id AND t.channel='qq' AND t.status='active')
 BEGIN SELECT RAISE(ABORT,'identity challenge transport mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_inbound_identity_challenge_update
 BEFORE UPDATE ON inbound_identity_challenges
 WHEN NEW.challenge_id!=OLD.challenge_id OR NEW.subject_id!=OLD.subject_id
 OR NEW.transport_id!=OLD.transport_id OR NEW.code_hash!=OLD.code_hash
 OR NEW.expires_at!=OLD.expires_at OR NEW.created_at!=OLD.created_at
 OR NEW.attempts<OLD.attempts OR NEW.attempts>5
 OR (OLD.status IN ('confirmed','revoked','expired') AND NEW.status!=OLD.status)
 OR (OLD.status='observed' AND NEW.status='pending')
 OR (OLD.status='pending' AND NEW.status='confirmed')
 OR (OLD.status='confirmed' AND (NEW.binding_id IS NOT OLD.binding_id
 OR NEW.confirmed_at IS NOT OLD.confirmed_at))
 OR (OLD.status!='pending' AND (NEW.external_account_id IS NOT OLD.external_account_id
 OR NEW.external_sender_id IS NOT OLD.external_sender_id
 OR NEW.observed_event_hash IS NOT OLD.observed_event_hash))
BEGIN SELECT RAISE(ABORT,'terminal identity challenge cannot change'); END;
CREATE TRIGGER IF NOT EXISTS validate_inbound_identity_challenge_confirmation
 BEFORE UPDATE ON inbound_identity_challenges WHEN NEW.status='confirmed'
 AND NOT EXISTS(SELECT 1 FROM interaction_bindings b WHERE b.binding_id=NEW.binding_id
 AND b.subject_id=NEW.subject_id AND b.transport_id=NEW.transport_id
 AND b.external_account_id=NEW.external_account_id
 AND b.external_sender_id=NEW.external_sender_id)
 BEGIN SELECT RAISE(ABORT,'identity confirmation binding mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_inbound_identity_challenge_delete
 BEFORE DELETE ON inbound_identity_challenges BEGIN
 SELECT RAISE(ABORT,'identity challenges are audit evidence'); END;
CREATE TABLE IF NOT EXISTS private_channel_routes (
 binding_id TEXT PRIMARY KEY REFERENCES interaction_bindings(binding_id),
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 conversation_id TEXT NOT NULL REFERENCES private_conversations(conversation_id),
 share_web_history INTEGER NOT NULL CHECK(share_web_history IN (0,1)),
 updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS validate_private_channel_route_insert
 BEFORE INSERT ON private_channel_routes
 WHEN NOT EXISTS(SELECT 1 FROM interaction_bindings b JOIN private_conversations c
 ON c.subject_id=b.subject_id WHERE b.binding_id=NEW.binding_id
 AND b.subject_id=NEW.subject_id AND c.conversation_id=NEW.conversation_id
 AND c.status='active')
 BEGIN SELECT RAISE(ABORT,'private route subject mismatch'); END;
CREATE TRIGGER IF NOT EXISTS validate_private_channel_route_update
 BEFORE UPDATE ON private_channel_routes
 WHEN NEW.binding_id!=OLD.binding_id OR NEW.subject_id!=OLD.subject_id
 OR NOT EXISTS(SELECT 1 FROM private_conversations c WHERE c.subject_id=NEW.subject_id
 AND c.conversation_id=NEW.conversation_id AND c.status='active')
 BEGIN SELECT RAISE(ABORT,'private route subject mismatch'); END;
"""

PRIVATE_ROUTE_PUSH_COLUMNS = {
    "push_web_replies": "INTEGER NOT NULL DEFAULT 0 CHECK(push_web_replies IN (0,1))",
    "revision": "INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)",
    "push_since": "TEXT",
    "push_after_sequence": "INTEGER NOT NULL DEFAULT 0 CHECK(push_after_sequence>=0)",
}

MIGRATION_V90 = """
CREATE TABLE IF NOT EXISTS private_message_sequences (
 sequence INTEGER PRIMARY KEY,
 interaction_id TEXT NOT NULL UNIQUE REFERENCES interactions(interaction_id)
);
INSERT INTO private_message_sequences(interaction_id)
 SELECT i.interaction_id FROM interactions i
 WHERE NOT EXISTS(SELECT 1 FROM private_message_sequences s WHERE s.interaction_id=i.interaction_id)
 ORDER BY i.created_at,i.interaction_id;
CREATE TRIGGER IF NOT EXISTS allocate_private_message_sequence AFTER INSERT ON interactions
 BEGIN INSERT INTO private_message_sequences(interaction_id) VALUES(NEW.interaction_id); END;
CREATE TRIGGER IF NOT EXISTS prevent_private_message_sequence_update
 BEFORE UPDATE ON private_message_sequences
 BEGIN SELECT RAISE(ABORT,'private sequence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_private_message_sequence_delete
 BEFORE DELETE ON private_message_sequences
 BEGIN SELECT RAISE(ABORT,'private sequence is immutable'); END;
DROP TRIGGER IF EXISTS validate_private_channel_route_update;
CREATE TRIGGER validate_private_channel_route_update BEFORE UPDATE ON private_channel_routes
 WHEN NEW.binding_id!=OLD.binding_id OR NEW.subject_id!=OLD.subject_id
 OR NEW.revision<=OLD.revision
 OR NOT EXISTS(SELECT 1 FROM private_conversations c WHERE c.subject_id=NEW.subject_id
 AND c.conversation_id=NEW.conversation_id AND (c.status='active'
 OR (NEW.push_web_replies=0 AND NEW.share_web_history=0)))
 BEGIN SELECT RAISE(ABORT,'private route subject or generation mismatch'); END;
CREATE UNIQUE INDEX IF NOT EXISTS idx_private_route_push_window
 ON private_channel_routes(subject_id,conversation_id)
 WHERE push_web_replies=1;
CREATE TABLE IF NOT EXISTS private_web_deliveries (
 delivery_id TEXT PRIMARY KEY REFERENCES interaction_deliveries(delivery_id),
 binding_id TEXT NOT NULL REFERENCES interaction_bindings(binding_id),
 conversation_id TEXT NOT NULL REFERENCES private_conversations(conversation_id),
 route_revision INTEGER NOT NULL CHECK(route_revision >= 1)
);
CREATE TRIGGER IF NOT EXISTS validate_private_web_delivery_insert
 BEFORE INSERT ON private_web_deliveries
 WHEN NOT EXISTS(SELECT 1 FROM interaction_deliveries d
 JOIN interactions i ON i.interaction_id=d.interaction_id
 JOIN private_conversation_messages m ON m.interaction_id=i.interaction_id
 JOIN private_channel_routes r ON r.binding_id=NEW.binding_id
 JOIN interaction_bindings b ON b.binding_id=r.binding_id
 JOIN private_conversations c ON c.conversation_id=m.conversation_id
 WHERE d.delivery_id=NEW.delivery_id AND d.subject_id=i.subject_id
 AND r.subject_id=d.subject_id AND b.subject_id=d.subject_id AND c.subject_id=d.subject_id
 AND m.subject_id=d.subject_id AND b.transport_id=d.transport_id AND b.status='active'
 AND r.conversation_id=c.conversation_id AND c.conversation_id=NEW.conversation_id
 AND r.revision=NEW.route_revision AND r.push_web_replies=1 AND r.share_web_history=1
 AND c.status='active' AND m.author_verified=1 AND i.channel='web'
 AND EXISTS(SELECT 1 FROM inbound_identity_challenges x WHERE x.binding_id=b.binding_id
 AND x.status='confirmed' AND x.subject_id=d.subject_id)
 AND i.direction='outgoing' AND EXISTS(SELECT 1 FROM private_message_sequences s
 WHERE s.interaction_id=i.interaction_id AND s.sequence>r.push_after_sequence))
 BEGIN SELECT RAISE(ABORT,'private push authorization mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_private_web_delivery_update
 BEFORE UPDATE ON private_web_deliveries
 BEGIN SELECT RAISE(ABORT,'private push consent is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_private_web_delivery_delete
 BEFORE DELETE ON private_web_deliveries
 BEGIN SELECT RAISE(ABORT,'private push consent is audit evidence'); END;
"""
