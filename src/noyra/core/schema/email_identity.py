"""Additive per-event email evidence; legacy identities are never backfilled as verified."""

EMAIL_IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_ingress_evidence (
    event_id TEXT PRIMARY KEY REFERENCES interaction_inbound_events(event_id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    message_id TEXT CHECK(message_id IS NULL OR length(message_id) BETWEEN 1 AND 512),
    gateway_event_id TEXT
        CHECK(gateway_event_id IS NULL OR length(gateway_event_id) BETWEEN 1 AND 512),
    verification TEXT NOT NULL CHECK(verification IN (
        'unverified','mailbox_challenge','authenticated_submission','provider_mailbox')),
    raw_message_hash TEXT NOT NULL CHECK(length(raw_message_hash)=64),
    source_namespace TEXT NOT NULL CHECK(length(source_namespace)=64),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64)
);
CREATE INDEX IF NOT EXISTS idx_email_ingress_subject ON email_ingress_evidence(subject_id,event_id);
CREATE TRIGGER IF NOT EXISTS validate_email_ingress_subject
BEFORE INSERT ON email_ingress_evidence
WHEN NOT EXISTS(SELECT 1 FROM interaction_inbound_events e WHERE e.event_id=NEW.event_id
    AND e.subject_id=NEW.subject_id AND e.channel='email'
    AND e.reply_context_version=1 AND e.email_identity_version=1)
BEGIN SELECT RAISE(ABORT,'email identity evidence owner mismatch'); END;
CREATE TRIGGER IF NOT EXISTS preserve_email_identity_version
BEFORE UPDATE OF email_identity_version ON interaction_inbound_events
WHEN NEW.email_identity_version IS NOT OLD.email_identity_version
BEGIN SELECT RAISE(ABORT,'email identity version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_email_ingress_evidence_update
BEFORE UPDATE ON email_ingress_evidence
BEGIN SELECT RAISE(ABORT,'email identity evidence is immutable'); END;
"""
