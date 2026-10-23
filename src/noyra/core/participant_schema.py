"""Additive participant identity, account and collaboration storage contract."""
# ruff: noqa: E501

PARTICIPANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS participant_entities (
 entity_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 entity_type TEXT NOT NULL CHECK(entity_type IN ('human','agent','noyra','unknown')),
 display_name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','disabled')),
 created_at TEXT NOT NULL, UNIQUE(subject_id,entity_id)
);
CREATE TABLE IF NOT EXISTS participant_sources (
 source_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, entity_id TEXT NOT NULL,
 channel TEXT NOT NULL, namespace TEXT NOT NULL, sender_id TEXT NOT NULL,
 authentication TEXT NOT NULL CHECK(authentication IN ('channel','account','credential','legacy')),
 created_at TEXT NOT NULL, UNIQUE(subject_id,channel,namespace,sender_id),
 FOREIGN KEY(subject_id,entity_id) REFERENCES participant_entities(subject_id,entity_id)
);
CREATE INDEX IF NOT EXISTS idx_participant_sources_entity ON participant_sources(subject_id,entity_id);
CREATE TABLE IF NOT EXISTS participant_message_links (
 interaction_id TEXT PRIMARY KEY REFERENCES interactions(interaction_id), subject_id TEXT NOT NULL,
 entity_id TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES participant_sources(source_id),
 conversation_key TEXT NOT NULL, visibility TEXT NOT NULL CHECK(visibility IN ('channel','private')),
 created_at TEXT NOT NULL,
 FOREIGN KEY(subject_id,entity_id) REFERENCES participant_entities(subject_id,entity_id)
);
CREATE INDEX IF NOT EXISTS idx_participant_messages_owner ON participant_message_links(subject_id,entity_id,conversation_key,created_at);
CREATE TABLE IF NOT EXISTS participant_accounts (
 account_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, entity_id TEXT NOT NULL,
 username TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(subject_id,username), UNIQUE(subject_id,account_id),
 FOREIGN KEY(subject_id,entity_id) REFERENCES participant_entities(subject_id,entity_id)
);
CREATE TABLE IF NOT EXISTS participant_credentials (
 account_id TEXT PRIMARY KEY REFERENCES participant_accounts(account_id), subject_id TEXT NOT NULL,
 password_digest TEXT NOT NULL, recovery_digest TEXT NOT NULL, changed_at TEXT NOT NULL,
 FOREIGN KEY(subject_id,account_id) REFERENCES participant_accounts(subject_id,account_id)
);
CREATE TABLE IF NOT EXISTS participant_sessions (
 session_digest TEXT PRIMARY KEY, subject_id TEXT NOT NULL, account_id TEXT NOT NULL,
 csrf_digest TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
 FOREIGN KEY(subject_id,account_id) REFERENCES participant_accounts(subject_id,account_id)
);
CREATE INDEX IF NOT EXISTS idx_participant_sessions_account ON participant_sessions(subject_id,account_id);
CREATE TABLE IF NOT EXISTS participant_api_credentials (
 credential_digest TEXT PRIMARY KEY, subject_id TEXT NOT NULL, account_id TEXT NOT NULL,
 label TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(subject_id,account_id) REFERENCES participant_accounts(subject_id,account_id)
);
CREATE TABLE IF NOT EXISTS participant_auth_limits (
 subject_id TEXT NOT NULL, bucket TEXT NOT NULL, window_start REAL NOT NULL, attempts INTEGER NOT NULL,
 PRIMARY KEY(subject_id,bucket)
);
CREATE TABLE IF NOT EXISTS participant_post_links (
 post_id TEXT PRIMARY KEY REFERENCES public_posts(post_id), subject_id TEXT NOT NULL,
 entity_id TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(subject_id,entity_id) REFERENCES participant_entities(subject_id,entity_id)
);
CREATE INDEX IF NOT EXISTS idx_participant_posts_owner ON participant_post_links(subject_id,entity_id,created_at);
CREATE TABLE IF NOT EXISTS participant_submissions (
 submission_id TEXT PRIMARY KEY REFERENCES wallet_bounty_submissions(submission_id),
 subject_id TEXT NOT NULL, entity_id TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(subject_id,entity_id) REFERENCES participant_entities(subject_id,entity_id)
);
CREATE INDEX IF NOT EXISTS idx_participant_submissions_owner ON participant_submissions(subject_id,entity_id,created_at);
CREATE TABLE IF NOT EXISTS participant_applications (
 application_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, entity_id TEXT NOT NULL,
 bounty_id TEXT NOT NULL REFERENCES wallet_bounties(bounty_id), content TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('submitted','withdrawn')), idempotency_key TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(subject_id,entity_id,idempotency_key),
 FOREIGN KEY(subject_id,entity_id) REFERENCES participant_entities(subject_id,entity_id)
);
CREATE INDEX IF NOT EXISTS idx_participant_applications_owner ON participant_applications(subject_id,entity_id,created_at);
CREATE TABLE IF NOT EXISTS participant_notification_reads (
 subject_id TEXT NOT NULL, entity_id TEXT NOT NULL, notification_id TEXT NOT NULL, read_at TEXT NOT NULL,
 PRIMARY KEY(subject_id,entity_id,notification_id),
 FOREIGN KEY(subject_id,entity_id) REFERENCES participant_entities(subject_id,entity_id)
);
"""

# Provenance is append-only. Credential rotation and application withdrawal are
# the explicit mutable operations; no source can be reassigned behind a session.
for _table in (
    "participant_sources",
    "participant_message_links",
    "participant_post_links",
    "participant_submissions",
):
    PARTICIPANT_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS prevent_{_table}_update BEFORE UPDATE ON {_table}
BEGIN SELECT RAISE(ABORT,'participant attribution is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_{_table}_delete BEFORE DELETE ON {_table}
BEGIN SELECT RAISE(ABORT,'participant attribution is immutable'); END;
"""
PARTICIPANT_SCHEMA += """
CREATE TRIGGER IF NOT EXISTS prevent_participant_account_identity_update
BEFORE UPDATE ON participant_accounts
BEGIN SELECT RAISE(ABORT,'participant account identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_participant_entity_identity_update
BEFORE UPDATE OF entity_id,subject_id,entity_type,display_name,created_at ON participant_entities
BEGIN SELECT RAISE(ABORT,'participant entity identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_participant_message_owner
BEFORE INSERT ON participant_message_links
WHEN NOT EXISTS (
 SELECT 1 FROM interactions i JOIN participant_sources s ON s.source_id=NEW.source_id
 WHERE i.interaction_id=NEW.interaction_id AND i.subject_id=NEW.subject_id
 AND s.subject_id=NEW.subject_id AND s.entity_id=NEW.entity_id)
BEGIN SELECT RAISE(ABORT,'participant message ownership mismatch'); END;
CREATE TRIGGER IF NOT EXISTS validate_participant_post_owner
BEFORE INSERT ON participant_post_links
WHEN NOT EXISTS (SELECT 1 FROM public_posts p
 WHERE p.post_id=NEW.post_id AND p.subject_id=NEW.subject_id)
BEGIN SELECT RAISE(ABORT,'participant post ownership mismatch'); END;
CREATE TRIGGER IF NOT EXISTS validate_participant_submission_owner
BEFORE INSERT ON participant_submissions
WHEN NOT EXISTS (SELECT 1 FROM wallet_bounty_submissions s
 WHERE s.submission_id=NEW.submission_id AND s.subject_id=NEW.subject_id
 AND s.counterparty=NEW.entity_id)
BEGIN SELECT RAISE(ABORT,'participant submission ownership mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_participant_application_identity_update
BEFORE UPDATE OF application_id,subject_id,entity_id,bounty_id,content,idempotency_key,created_at
 ON participant_applications
BEGIN SELECT RAISE(ABORT,'participant application identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_participant_application_owner
BEFORE INSERT ON participant_applications
WHEN NOT EXISTS (SELECT 1 FROM wallet_bounties b
 WHERE b.bounty_id=NEW.bounty_id AND b.subject_id=NEW.subject_id)
BEGIN SELECT RAISE(ABORT,'participant application ownership mismatch'); END;
"""
