"""Migrations v043 v052 definitions; execution remains in core.database."""

from __future__ import annotations

MIGRATIONS_V043_V052: dict[int, str] = {
    43: """
CREATE INDEX IF NOT EXISTS idx_archive_transfer_lease
    ON archive_transfer_queue(subject_id, status, lease_expires_at);

DROP TRIGGER IF EXISTS validate_archive_transfer_claim_insert;
CREATE TRIGGER validate_archive_transfer_claim_insert
BEFORE INSERT ON archive_transfer_queue
WHEN (NEW.status = 'uploading' AND (
        NEW.claim_token IS NULL OR NEW.lease_owner IS NULL OR NEW.lease_expires_at IS NULL
    )) OR (NEW.status != 'uploading' AND (
        NEW.claim_token IS NOT NULL OR NEW.lease_owner IS NOT NULL
        OR NEW.lease_expires_at IS NOT NULL
    ))
BEGIN
    SELECT RAISE(ABORT, 'archive transfer claim does not match status');
END;

DROP TRIGGER IF EXISTS validate_archive_transfer_claim_update;
CREATE TRIGGER validate_archive_transfer_claim_update
BEFORE UPDATE ON archive_transfer_queue
WHEN (NEW.status = 'uploading' AND (
        NEW.claim_token IS NULL OR NEW.lease_owner IS NULL OR NEW.lease_expires_at IS NULL
    )) OR (NEW.status != 'uploading' AND (
        NEW.claim_token IS NOT NULL OR NEW.lease_owner IS NOT NULL
        OR NEW.lease_expires_at IS NOT NULL
    ))
BEGIN
    SELECT RAISE(ABORT, 'archive transfer claim does not match status');
END;

DROP TRIGGER IF EXISTS validate_archive_transfer_identity_immutable;
CREATE TRIGGER validate_archive_transfer_identity_immutable
BEFORE UPDATE OF transfer_id, subject_id, storage_class, object_key, payload_path,
                 byte_size, content_hash, created_at ON archive_transfer_queue
WHEN NEW.transfer_id IS NOT OLD.transfer_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.storage_class IS NOT OLD.storage_class
  OR NEW.object_key IS NOT OLD.object_key
  OR NEW.payload_path IS NOT OLD.payload_path
  OR NEW.byte_size IS NOT OLD.byte_size
  OR NEW.content_hash IS NOT OLD.content_hash
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'archive transfer identity is immutable');
END;

DROP TRIGGER IF EXISTS validate_archive_transfer_transition;
CREATE TRIGGER validate_archive_transfer_transition
BEFORE UPDATE OF status ON archive_transfer_queue
WHEN NOT (
    NEW.status = OLD.status
    OR (OLD.status = 'queued' AND NEW.status IN ('uploading', 'dead'))
    OR (OLD.status = 'failed' AND NEW.status IN ('queued', 'uploading', 'dead'))
    OR (OLD.status = 'uploading' AND NEW.status IN ('uploaded', 'failed', 'dead'))
    OR (OLD.status IN ('uploaded', 'dead') AND NEW.status = 'queued')
)
BEGIN
    SELECT RAISE(ABORT, 'archive transfer status transition is invalid');
END;

CREATE TABLE IF NOT EXISTS archive_staging_manifests (
    manifest_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    archive_kind TEXT NOT NULL CHECK (
        archive_kind IN ('event_payload', 'observation_content')
    ),
    segment_id TEXT NOT NULL UNIQUE,
    object_key TEXT NOT NULL UNIQUE,
    source_state_json TEXT NOT NULL,
    source_state_hash TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    first_item_at TEXT NOT NULL,
    last_item_at TEXT NOT NULL,
    plaintext_hash TEXT NOT NULL,
    archive_format TEXT NOT NULL,
    encryption_key_id TEXT NOT NULL,
    encryption_key_fingerprint TEXT NOT NULL,
    stored_byte_size INTEGER CHECK (stored_byte_size IS NULL OR stored_byte_size >= 0),
    stored_hash TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'stored', 'committed', 'abandoned', 'removed')
    ),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_archive_staging_subject_status
    ON archive_staging_manifests(subject_id, archive_kind, status, updated_at, manifest_id);

DROP TRIGGER IF EXISTS validate_archive_staging_identity_immutable;
CREATE TRIGGER validate_archive_staging_identity_immutable
BEFORE UPDATE OF manifest_id, subject_id, archive_kind, segment_id, object_key,
                 source_state_json, source_state_hash, item_count, first_item_at,
                 last_item_at, plaintext_hash, archive_format, encryption_key_id,
                 encryption_key_fingerprint, created_at ON archive_staging_manifests
WHEN NEW.manifest_id IS NOT OLD.manifest_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.archive_kind IS NOT OLD.archive_kind
  OR NEW.segment_id IS NOT OLD.segment_id
  OR NEW.object_key IS NOT OLD.object_key
  OR NEW.source_state_json IS NOT OLD.source_state_json
  OR NEW.source_state_hash IS NOT OLD.source_state_hash
  OR NEW.item_count IS NOT OLD.item_count
  OR NEW.first_item_at IS NOT OLD.first_item_at
  OR NEW.last_item_at IS NOT OLD.last_item_at
  OR NEW.plaintext_hash IS NOT OLD.plaintext_hash
  OR NEW.archive_format IS NOT OLD.archive_format
  OR NEW.encryption_key_id IS NOT OLD.encryption_key_id
  OR NEW.encryption_key_fingerprint IS NOT OLD.encryption_key_fingerprint
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'archive staging manifest identity is immutable');
END;

DROP TRIGGER IF EXISTS validate_archive_staging_transition;
CREATE TRIGGER validate_archive_staging_transition
BEFORE UPDATE OF status ON archive_staging_manifests
WHEN NOT (
    NEW.status = OLD.status
    OR (OLD.status = 'prepared' AND NEW.status IN ('stored', 'abandoned'))
    OR (OLD.status = 'stored' AND NEW.status IN ('committed', 'abandoned'))
    OR (OLD.status = 'abandoned' AND NEW.status = 'removed')
)
BEGIN
    SELECT RAISE(ABORT, 'archive staging manifest transition is invalid');
END;

DROP TRIGGER IF EXISTS validate_archive_staging_storage_state;
CREATE TRIGGER validate_archive_staging_storage_state
BEFORE UPDATE ON archive_staging_manifests
WHEN (NEW.status IN ('stored', 'committed') AND (
        NEW.stored_byte_size IS NULL OR NEW.stored_hash IS NULL
    )) OR (NEW.status = 'removed' AND NEW.finalized_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'archive staging storage metadata is incomplete');
END;

DROP TRIGGER IF EXISTS prevent_archive_staging_delete;
CREATE TRIGGER prevent_archive_staging_delete
BEFORE DELETE ON archive_staging_manifests
BEGIN
    SELECT RAISE(ABORT, 'archive staging manifests are append-only');
END;

DROP TRIGGER IF EXISTS validate_training_record_event_subject;
CREATE TRIGGER validate_training_record_event_subject
BEFORE INSERT ON training_records
WHEN NOT EXISTS (
    SELECT 1 FROM events e
    WHERE e.event_id = NEW.event_id AND e.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'training record event subject mismatch');
END;
""",
    44: """
CREATE TABLE IF NOT EXISTS public_posts (
    post_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    kind TEXT NOT NULL CHECK (kind IN ('post', 'help_request')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    author_label TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('draft', 'pending_review', 'published', 'rejected', 'archived')
    ),
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_public_posts_subject_status_time
    ON public_posts(subject_id, status, published_at DESC, post_id DESC);
CREATE TRIGGER IF NOT EXISTS prevent_public_post_identity_update
BEFORE UPDATE OF post_id, subject_id, kind, title, content, content_hash,
                 author_label, idempotency_key, created_at ON public_posts BEGIN
    SELECT RAISE(ABORT, 'public post identity is immutable');
END;
""",
    45: """
CREATE TABLE IF NOT EXISTS interaction_bindings (
    binding_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    transport_id TEXT NOT NULL REFERENCES interaction_transports(transport_id),
    channel TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    external_sender_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('creator', 'participant')),
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'revoked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, transport_id, external_account_id, external_sender_id)
);
CREATE INDEX IF NOT EXISTS idx_interaction_bindings_subject_status
    ON interaction_bindings(subject_id, status, channel);

CREATE TABLE IF NOT EXISTS interaction_inbound_events (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    transport_id TEXT NOT NULL REFERENCES interaction_transports(transport_id),
    channel TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    external_sender_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    scheduling_priority INTEGER NOT NULL DEFAULT 0 CHECK (
        scheduling_priority >= 0 AND scheduling_priority <= 100
    ),
    status TEXT NOT NULL CHECK (status IN ('received', 'processed', 'rejected')),
    received_at TEXT NOT NULL,
    processed_at TEXT,
    interaction_id TEXT REFERENCES interactions(interaction_id),
    UNIQUE(subject_id, transport_id, provider_event_id)
);
CREATE INDEX IF NOT EXISTS idx_interaction_inbound_subject_status
    ON interaction_inbound_events(subject_id, status, received_at);

CREATE TABLE IF NOT EXISTS interaction_threads (
    thread_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    transport_id TEXT NOT NULL REFERENCES interaction_transports(transport_id),
    channel TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    external_thread_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, transport_id, conversation_id, external_thread_id)
);
CREATE INDEX IF NOT EXISTS idx_interaction_threads_subject_conversation
    ON interaction_threads(subject_id, transport_id, conversation_id, updated_at);
CREATE TRIGGER IF NOT EXISTS validate_interaction_binding_transport_insert
BEFORE INSERT ON interaction_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.transport_id = NEW.transport_id
      AND t.subject_id = NEW.subject_id
      AND t.channel = NEW.channel
)
BEGIN
    SELECT RAISE(ABORT, 'inbound binding transport subject mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_interaction_binding_transport_update
BEFORE UPDATE OF subject_id, transport_id, channel ON interaction_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.transport_id = NEW.transport_id
      AND t.subject_id = NEW.subject_id
      AND t.channel = NEW.channel
)
BEGIN
    SELECT RAISE(ABORT, 'inbound binding transport subject mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_interaction_inbound_transport_insert
BEFORE INSERT ON interaction_inbound_events
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.transport_id = NEW.transport_id
      AND t.subject_id = NEW.subject_id
      AND t.channel = NEW.channel
)
BEGIN
    SELECT RAISE(ABORT, 'inbound event transport subject mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_interaction_inbound_interaction_insert
BEFORE INSERT ON interaction_inbound_events
WHEN NEW.interaction_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM interactions i
    WHERE i.interaction_id = NEW.interaction_id AND i.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'inbound event interaction subject mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_interaction_thread_transport_insert
BEFORE INSERT ON interaction_threads
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.transport_id = NEW.transport_id
      AND t.subject_id = NEW.subject_id
      AND t.channel = NEW.channel
)
BEGIN
    SELECT RAISE(ABORT, 'interaction thread transport subject mismatch');
END;
""",
    46: """
-- The first interaction schema keyed provider events and threads only by
-- transport.  A transport may represent multiple external bot/app accounts;
-- rebuild both tables so account identity participates in deduplication.
DROP TRIGGER IF EXISTS validate_interaction_inbound_transport_insert;
DROP TRIGGER IF EXISTS validate_interaction_inbound_interaction_insert;
DROP TRIGGER IF EXISTS validate_interaction_thread_transport_insert;

CREATE TABLE interaction_inbound_events_v46 (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    transport_id TEXT NOT NULL REFERENCES interaction_transports(transport_id),
    channel TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    external_sender_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    scheduling_priority INTEGER NOT NULL DEFAULT 0 CHECK (
        scheduling_priority >= 0 AND scheduling_priority <= 100
    ),
    status TEXT NOT NULL CHECK (status IN ('received', 'processed', 'rejected')),
    received_at TEXT NOT NULL,
    processed_at TEXT,
    interaction_id TEXT REFERENCES interactions(interaction_id),
    UNIQUE(subject_id, transport_id, external_account_id, provider_event_id)
);
INSERT INTO interaction_inbound_events_v46(
    event_id, subject_id, transport_id, channel, provider_event_id,
    external_account_id, external_sender_id, conversation_id, content_hash,
    scheduling_priority, status, received_at, processed_at, interaction_id
)
SELECT e.event_id, e.subject_id, e.transport_id, e.channel, e.provider_event_id,
       COALESCE((
           SELECT b.external_account_id FROM interaction_bindings b
           WHERE b.subject_id = e.subject_id AND b.transport_id = e.transport_id
             AND b.external_sender_id = e.external_sender_id
           ORDER BY b.status = 'active' DESC, b.binding_id LIMIT 1
       ), 'legacy'),
       e.external_sender_id, e.conversation_id, e.content_hash,
       e.scheduling_priority, e.status, e.received_at, e.processed_at, e.interaction_id
FROM interaction_inbound_events e;
DROP TABLE interaction_inbound_events;
ALTER TABLE interaction_inbound_events_v46 RENAME TO interaction_inbound_events;
CREATE INDEX idx_interaction_inbound_subject_status
    ON interaction_inbound_events(subject_id, status, received_at);

CREATE TABLE interaction_threads_v46 (
    thread_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    transport_id TEXT NOT NULL REFERENCES interaction_transports(transport_id),
    channel TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    external_thread_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, transport_id, external_account_id, conversation_id, external_thread_id)
);
INSERT INTO interaction_threads_v46(
    thread_id, subject_id, transport_id, channel, external_account_id,
    conversation_id, external_thread_id, created_at, updated_at
)
SELECT thread_id, subject_id, transport_id, channel, external_account_id,
       conversation_id, external_thread_id, created_at, updated_at
FROM interaction_threads;
DROP TABLE interaction_threads;
ALTER TABLE interaction_threads_v46 RENAME TO interaction_threads;
CREATE INDEX idx_interaction_threads_subject_conversation
    ON interaction_threads(subject_id, transport_id, external_account_id,
                           conversation_id, updated_at);
""",
    47: """
CREATE TABLE IF NOT EXISTS public_post_rate_events (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    client_ip TEXT NOT NULL,
    occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_public_post_rate_events_lookup
    ON public_post_rate_events(subject_id, client_ip, occurred_at);

CREATE TABLE IF NOT EXISTS public_post_captcha_challenges (
    challenge_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    client_ip TEXT NOT NULL,
    answer_hash TEXT NOT NULL,
    answer_salt TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_public_post_captcha_subject_expiry
    ON public_post_captcha_challenges(subject_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_public_post_captcha_ip_expiry
    ON public_post_captcha_challenges(client_ip, expires_at);
""",
    48: """
CREATE TABLE IF NOT EXISTS public_post_controls (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    rate_limit_per_hour INTEGER NOT NULL CHECK (
        rate_limit_per_hour >= 1 AND rate_limit_per_hour <= 100000
    ),
    queue_cap INTEGER NOT NULL CHECK (queue_cap >= 1 AND queue_cap <= 1000000),
    captcha_ttl_seconds INTEGER NOT NULL CHECK (
        captcha_ttl_seconds >= 30 AND captcha_ttl_seconds <= 3600
    ),
    captcha_max_attempts INTEGER NOT NULL CHECK (
        captcha_max_attempts >= 1 AND captcha_max_attempts <= 20
    ),
    captcha_mode TEXT NOT NULL CHECK (captcha_mode IN ('letters', 'digits', 'alphanumeric')),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    state_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS public_post_moderation_events (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    post_id TEXT NOT NULL REFERENCES public_posts(post_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_public_post_moderation_post_time
    ON public_post_moderation_events(subject_id, post_id, created_at, event_id);

DROP TRIGGER IF EXISTS prevent_public_post_delete;
CREATE TRIGGER prevent_public_post_delete
BEFORE DELETE ON public_posts
BEGIN
    SELECT RAISE(ABORT, 'public posts cannot be deleted');
END;

DROP TRIGGER IF EXISTS validate_public_post_status_update;
CREATE TRIGGER validate_public_post_status_update
BEFORE UPDATE OF status, published_at ON public_posts
WHEN NOT (
    (OLD.status = 'draft' AND NEW.status = 'pending_review' AND NEW.published_at IS NULL)
    OR (OLD.status = 'pending_review' AND NEW.status IN ('published', 'rejected'))
    OR (OLD.status IN ('published', 'rejected') AND NEW.status = 'archived')
    OR (OLD.status = NEW.status AND NEW.published_at IS OLD.published_at)
)
BEGIN
    SELECT RAISE(ABORT, 'public post status transition is invalid');
END;

DROP TRIGGER IF EXISTS validate_public_post_published_at;
CREATE TRIGGER validate_public_post_published_at
BEFORE UPDATE OF status, published_at ON public_posts
WHEN (NEW.status = 'published' AND NEW.published_at IS NULL)
  OR (NEW.status != 'published' AND NEW.published_at IS NOT NULL
      AND OLD.published_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'public post publication timestamp is invalid');
END;

DROP TRIGGER IF EXISTS prevent_public_post_moderation_update;
CREATE TRIGGER prevent_public_post_moderation_update
BEFORE UPDATE ON public_post_moderation_events
BEGIN
    SELECT RAISE(ABORT, 'public post moderation history is append-only');
END;
DROP TRIGGER IF EXISTS prevent_public_post_moderation_delete;
CREATE TRIGGER prevent_public_post_moderation_delete
BEFORE DELETE ON public_post_moderation_events
BEGIN
    SELECT RAISE(ABORT, 'public post moderation history cannot be deleted');
END;
DROP TRIGGER IF EXISTS validate_public_post_moderation_subject;
CREATE TRIGGER validate_public_post_moderation_subject
BEFORE INSERT ON public_post_moderation_events
WHEN NOT EXISTS (
    SELECT 1 FROM public_posts p
    WHERE p.post_id = NEW.post_id AND p.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'public post moderation subject mismatch');
END;
""",
    49: """
CREATE TABLE IF NOT EXISTS public_post_captcha_issue_events (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    client_ip TEXT NOT NULL,
    issued_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_public_post_captcha_issue_lookup
    ON public_post_captcha_issue_events(subject_id, client_ip, issued_at);
""",
    50: """
-- Releases through schema 49 persisted only the origin in SQLite while the
-- private transport file retained the complete endpoint.  Mark those rows so
-- TransportStore can perform a one-time, integrity-checked endpoint upgrade
-- without confusing legacy state with a newly configured full endpoint.
-- The columns are added idempotently by Database._ensure_transport_endpoint_columns.
""",
    51: """
-- Preserve the exact provider reply route beside the authenticated inbound
-- event.  Conversation identifiers alone are not sufficient: QQ uses
-- different REST paths for C2C, group, guild-channel and guild-DM replies,
-- while Feishu inbound conversations must be addressed as chat_id.
-- The columns are added idempotently by Database._ensure_inbound_reply_columns.
CREATE UNIQUE INDEX idx_interaction_inbound_interaction
    ON interaction_inbound_events(subject_id, interaction_id)
    WHERE interaction_id IS NOT NULL;
""",
    52: """
-- Public-post moderation is an evidence chain.  Wall-clock timestamps and
-- random event identifiers cannot express causal order when two moderation
-- actions land in the same millisecond, so persist an explicit revision and
-- predecessor link.  Existing rows are upgraded by the Python migration hook
-- before append-only guards are restored.
DROP TRIGGER IF EXISTS prevent_public_post_moderation_update;

-- Preserve the immutable post envelope independently from the mutable
-- moderation projection and make unverified public authors explicit.
-- These columns are added idempotently by Database._ensure_public_post_evidence_columns.

-- The row-count backstop remains, but byte and CAPTCHA issuance budgets are
-- operator-visible controls with host-enforced maxima.

-- Cleanup is by subject and time, not by client, so give those bounded GC
-- queries matching indexes.  Ephemeral pre-v52 rows contained raw client IPs;
-- discard them during the one-time migration rather than carrying that data
-- into backups and runtime exports.
CREATE INDEX IF NOT EXISTS idx_public_post_rate_events_subject_time
    ON public_post_rate_events(subject_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_public_post_captcha_issue_subject_time
    ON public_post_captcha_issue_events(subject_id, issued_at);
DELETE FROM public_post_captcha_challenges;
DELETE FROM public_post_captcha_issue_events;
DELETE FROM public_post_rate_events;
""",
}
