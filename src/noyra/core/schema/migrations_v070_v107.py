# Historical SQL must retain its exact bytes; inherited from database.py.
# ruff: noqa: E501
"""Migrations v070 v106 definitions; execution remains in core.database."""

from __future__ import annotations

from noyra.core.conversation_schema import MIGRATION_V84, MIGRATION_V85
from noyra.core.evidence_schema import migration_v80, migration_v92
from noyra.core.participant_schema import PARTICIPANT_SCHEMA
from noyra.core.private_erasure_schema import (
    MIGRATION_V87,
    MIGRATION_V88,
    MIGRATION_V89,
    MIGRATION_V90,
)
from noyra.core.private_sources import MIGRATION_V86
from noyra.core.public_archive_schema import MIGRATION_V93
from noyra.core.public_policy import MIGRATION_V82
from noyra.core.public_sitemap_schema import MIGRATION_V94
from noyra.core.relationship_identity import RELATIONSHIP_IDENTITY_SCHEMA
from noyra.core.resource_policy import MIGRATION_V81
from noyra.core.retention_policy import MIGRATION_V83
from noyra.core.usage_schema import MIGRATION_V91

MIGRATIONS_V070_V107: dict[int, str] = {
    70: """
CREATE TABLE IF NOT EXISTS persistent_features (
    feature_id TEXT PRIMARY KEY,
    feature_version INTEGER NOT NULL CHECK (feature_version >= 1),
    ddl_fingerprint TEXT NOT NULL CHECK (
        length(ddl_fingerprint) = 64
        AND ddl_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""",
    71: """
CREATE TABLE IF NOT EXISTS wallet_payment_reconciliation_events (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    order_id TEXT NOT NULL REFERENCES wallet_payment_orders(order_id),
    execution_id TEXT NOT NULL REFERENCES wallet_payment_executions(execution_id),
    previous_status TEXT NOT NULL CHECK(previous_status = 'confirmed'),
    reason TEXT NOT NULL,
    tx_hash TEXT,
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*')
);
CREATE INDEX IF NOT EXISTS idx_wallet_payment_reconciliation_subject
    ON wallet_payment_reconciliation_events(subject_id, created_at DESC, event_id DESC);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_payment_reconciliation_update
BEFORE UPDATE ON wallet_payment_reconciliation_events
BEGIN SELECT RAISE(ABORT,'wallet payment reconciliation events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_payment_reconciliation_delete
BEFORE DELETE ON wallet_payment_reconciliation_events
BEGIN SELECT RAISE(ABORT,'wallet payment reconciliation events cannot be deleted'); END;
DROP TRIGGER IF EXISTS validate_wallet_execution_transition;
CREATE TRIGGER validate_wallet_execution_transition
BEFORE UPDATE ON wallet_payment_executions
WHEN NOT (
    NEW.status IS OLD.status
        AND NEW.tx_hash IS OLD.tx_hash
        AND NEW.error_code IS OLD.error_code
        AND NEW.receipt_status IS OLD.receipt_status
        AND NEW.receipt_block_number IS OLD.receipt_block_number
        AND NEW.attempt_count IS OLD.attempt_count
        AND NEW.state_hash IS OLD.state_hash
        AND NEW.last_audit_id IS OLD.last_audit_id
        AND NEW.updated_at IS OLD.updated_at
    OR OLD.status='signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status='broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status='unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed')
    OR OLD.status='confirmed' AND NEW.status='unknown'
)
BEGIN SELECT RAISE(ABORT,'wallet payment execution transition is invalid'); END;
DROP TRIGGER IF EXISTS validate_wallet_execution_attempt_transition;
CREATE TRIGGER validate_wallet_execution_attempt_transition
BEFORE UPDATE ON wallet_payment_execution_attempts
WHEN NOT (
    NEW.status IS OLD.status
        AND NEW.tx_hash IS OLD.tx_hash
        AND NEW.error_code IS OLD.error_code
        AND NEW.completed_at IS OLD.completed_at
        AND NEW.state_hash IS OLD.state_hash
    OR OLD.status='signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status='broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status='unknown' AND NEW.status IN ('broadcast','confirmed','failed')
    OR OLD.status='confirmed' AND NEW.status='unknown'
)
BEGIN SELECT RAISE(ABORT,'wallet payment execution attempt transition is invalid'); END;
DROP TRIGGER IF EXISTS validate_wallet_order_transition;
CREATE TRIGGER validate_wallet_order_transition
BEFORE UPDATE ON wallet_payment_orders
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash AND NEW.updated_at = OLD.updated_at
    OR OLD.status = 'pending_policy' AND NEW.status IN ('awaiting_confirmation','reserved','rejected','cancelled','expired')
    OR OLD.status = 'awaiting_confirmation' AND NEW.status IN ('reserved','rejected','cancelled','expired')
    OR OLD.status = 'reserved' AND NEW.status IN ('cancelled','expired','signing')
    OR OLD.status = 'signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status = 'broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status = 'unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed','refunded')
    OR OLD.status = 'confirmed' AND NEW.status='unknown'
    OR OLD.status = 'failed' AND NEW.status = 'refunded'
)
BEGIN SELECT RAISE(ABORT,'wallet payment order transition is invalid'); END;
""",
    72: """
CREATE TABLE IF NOT EXISTS migration_policies (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    approval_mode TEXT NOT NULL DEFAULT 'disabled' CHECK (approval_mode IN ('disabled', 'manual', 'policy_auto', 'emergency_recovery')),
    emergency_recovery_enabled INTEGER NOT NULL DEFAULT 0 CHECK (emergency_recovery_enabled IN (0, 1)),
    local_wallet_transfer_enabled INTEGER NOT NULL DEFAULT 0 CHECK (local_wallet_transfer_enabled IN (0, 1)),
    wallet_mode TEXT NOT NULL DEFAULT 'external_signer_rebind' CHECK (wallet_mode IN ('external_signer_rebind', 'local_wallet_transfer', 'disabled')),
    allowed_target_ids_json TEXT NOT NULL DEFAULT '[]',
    allowed_regions_json TEXT NOT NULL DEFAULT '[]',
    min_free_bytes INTEGER NOT NULL DEFAULT 0 CHECK (min_free_bytes >= 0),
    max_cost_microusd INTEGER NOT NULL DEFAULT 0 CHECK (max_cost_microusd >= 0),
    max_downtime_seconds INTEGER NOT NULL DEFAULT 3600 CHECK (max_downtime_seconds BETWEEN 0 AND 604800),
    maintenance_window_start_minute INTEGER NOT NULL DEFAULT 0 CHECK (maintenance_window_start_minute BETWEEN 0 AND 1439),
    maintenance_window_duration_minutes INTEGER NOT NULL DEFAULT 1440 CHECK (maintenance_window_duration_minutes BETWEEN 1 AND 1440),
    trust_level INTEGER NOT NULL DEFAULT 3 CHECK (trust_level BETWEEN 1 AND 5),
    rejection_cooldown_seconds INTEGER NOT NULL DEFAULT 604800 CHECK (rejection_cooldown_seconds BETWEEN 0 AND 31536000),
    proposal_expiry_seconds INTEGER NOT NULL DEFAULT 86400 CHECK (proposal_expiry_seconds BETWEEN 300 AND 604800),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*')
);
CREATE TABLE IF NOT EXISTS migration_targets (
    target_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    public_key TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL,
    enrollment_generation INTEGER NOT NULL CHECK (enrollment_generation >= 1),
    endpoint TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    region TEXT,
    provider TEXT,
    release_sha TEXT NOT NULL,
    os_arch TEXT NOT NULL,
    encrypted_volume INTEGER NOT NULL CHECK (encrypted_volume IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked', 'quarantined')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    UNIQUE(subject_id, key_fingerprint, enrollment_generation)
);
CREATE INDEX IF NOT EXISTS idx_migration_targets_subject_status ON migration_targets(subject_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS migration_proposals (
    proposal_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    target_id TEXT NOT NULL REFERENCES migration_targets(target_id),
    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),
    status TEXT NOT NULL CHECK (status IN ('planned', 'awaiting_approval', 'approved', 'rejected', 'expired', 'cancelled', 'executing')),
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    benefit_score REAL NOT NULL CHECK (benefit_score BETWEEN 0 AND 1),
    risk_score REAL NOT NULL CHECK (risk_score BETWEEN 0 AND 1),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision_reason TEXT,
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*')
);
CREATE INDEX IF NOT EXISTS idx_migration_proposals_subject_status ON migration_proposals(subject_id, status, created_at DESC);
CREATE TABLE IF NOT EXISTS migration_tasks (
    task_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES migration_proposals(proposal_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    target_id TEXT NOT NULL REFERENCES migration_targets(target_id),
    idempotency_key TEXT NOT NULL,
    source_epoch TEXT NOT NULL,
    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),
    status TEXT NOT NULL CHECK (status IN ('planned', 'preflight', 'awaiting_approval', 'approved', 'preparing', 'transferring', 'restoring', 'validating', 'cutover', 'committed', 'rolling_back', 'rolled_back', 'cancelled', 'failed')),
    manifest_digest TEXT,
    artifact_id TEXT,
    error_code TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_migration_tasks_subject_status ON migration_tasks(subject_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS migration_epochs (
    epoch_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    target_id TEXT NOT NULL REFERENCES migration_targets(target_id),
    epoch_number INTEGER NOT NULL CHECK (epoch_number >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'completed')),
    acquired_at TEXT NOT NULL,
    revoked_at TEXT,
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    UNIQUE(subject_id, epoch_number)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_migration_epochs_active_subject ON migration_epochs(subject_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS migration_rejections (
    rejection_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    proposal_id TEXT NOT NULL REFERENCES migration_proposals(proposal_id),
    target_id TEXT NOT NULL REFERENCES migration_targets(target_id),
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    cooldown_until TEXT NOT NULL,
    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*')
);
CREATE INDEX IF NOT EXISTS idx_migration_rejections_cooldown ON migration_rejections(subject_id, target_id, reason_code, cooldown_until);
CREATE TABLE IF NOT EXISTS migration_audit_events (
    audit_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*')
);
CREATE INDEX IF NOT EXISTS idx_migration_audit_subject_time ON migration_audit_events(subject_id, occurred_at DESC, audit_id DESC);
CREATE TRIGGER IF NOT EXISTS prevent_migration_audit_update BEFORE UPDATE ON migration_audit_events BEGIN SELECT RAISE(ABORT, 'migration audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_migration_audit_delete BEFORE DELETE ON migration_audit_events BEGIN SELECT RAISE(ABORT, 'migration audit events cannot be deleted'); END;
""",
    73: """
CREATE TABLE IF NOT EXISTS migration_target_challenges (
    nonce TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES migration_targets(target_id),
    source_epoch TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_migration_target_challenges_target
    ON migration_target_challenges(target_id, expires_at);
""",
    74: """
-- Attestation columns are added by the idempotent migration hook.  Keeping
-- this marker replay-safe matters for databases whose schema marker is
-- deliberately rewound during migration verification.
SELECT 1;
""",
    75: """
-- The target epoch column and index are installed by the idempotent migration
-- hook so migration verification can safely replay this marker.
SELECT 1;
""",
    76: """
CREATE TABLE IF NOT EXISTS admin_login_rate_events (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    client_key_hash TEXT NOT NULL CHECK (
        length(client_key_hash) = 64 AND client_key_hash NOT GLOB '*[^0-9a-f]*'
    ),
    occurred_at REAL NOT NULL CHECK (occurred_at >= 0)
);
CREATE INDEX IF NOT EXISTS idx_admin_login_rate_events_subject_client_time
    ON admin_login_rate_events(subject_id, client_key_hash, occurred_at);
CREATE TABLE IF NOT EXISTS admin_sessions (
    session_hash TEXT PRIMARY KEY CHECK (
        length(session_hash) = 64 AND session_hash NOT GLOB '*[^0-9a-f]*'
    ),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    role TEXT NOT NULL CHECK (role IN ('operator', 'admin', 'break_glass')),
    actor TEXT NOT NULL,
    expires_at REAL NOT NULL CHECK (expires_at >= 0),
    created_at REAL NOT NULL CHECK (created_at >= 0),
    revoked_at REAL,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_subject_active
    ON admin_sessions(subject_id, revoked_at, expires_at, created_at);
""",
    77: """
CREATE TABLE IF NOT EXISTS search_provider_routing (
    config_id TEXT PRIMARY KEY REFERENCES search_provider_configs(config_id),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 1000),
    weight INTEGER NOT NULL CHECK(weight BETWEEN 1 AND 1000),
    updated_at TEXT NOT NULL,
    state_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_provider_routing_order
    ON search_provider_routing(priority, config_id);
""",
    78: """
CREATE TABLE IF NOT EXISTS migration_backup_registry (
    backup_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    backup_path TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(byte_size > 0),
    schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
    genesis_hash TEXT NOT NULL CHECK(length(genesis_hash) = 64 AND genesis_hash NOT GLOB '*[^0-9a-f]*'),
    key_id TEXT NOT NULL,
    keyring_generation INTEGER NOT NULL CHECK(keyring_generation >= 1),
    status TEXT NOT NULL CHECK(status IN ('registered', 'verified', 'revoked', 'expired')),
    verified_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK(length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*')
);
CREATE INDEX IF NOT EXISTS idx_migration_backup_registry_subject_status
    ON migration_backup_registry(subject_id, status, verified_at DESC);
""",
    79: """
-- Recipient X25519 keys are installed by the idempotent migration hook.  The
-- marker remains replay-safe for databases created before recipient-encrypted
-- migration bundles were introduced.
SELECT 1;
""",
    80: migration_v80(),
    81: MIGRATION_V81,
    82: MIGRATION_V82,
    83: MIGRATION_V83,
    84: MIGRATION_V84,
    85: MIGRATION_V85,
    86: MIGRATION_V86,
    87: MIGRATION_V87,
    88: MIGRATION_V88,
    89: MIGRATION_V89,
    90: MIGRATION_V90,
    91: MIGRATION_V91,
    92: migration_v92(),
    93: MIGRATION_V93,
    94: MIGRATION_V94,
    95: """
ALTER TABLE cognitive_resource_groups
    ADD COLUMN reasoning_effort TEXT CHECK (
        reasoning_effort IS NULL OR reasoning_effort IN ('low', 'medium', 'high', 'xhigh')
    );
""",
    96: """
CREATE TABLE IF NOT EXISTS migration_policy_review_modes (
    subject_id TEXT PRIMARY KEY REFERENCES migration_policies(subject_id),
    mode TEXT NOT NULL CHECK (mode IN ('self_attested', 'dual_review')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_proposal_approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES migration_proposals(proposal_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE (proposal_id, actor)
);
CREATE INDEX IF NOT EXISTS idx_migration_proposal_approvals_subject
    ON migration_proposal_approvals(subject_id, proposal_id, created_at);
CREATE TRIGGER IF NOT EXISTS prevent_migration_proposal_approval_update
    BEFORE UPDATE ON migration_proposal_approvals
    BEGIN SELECT RAISE(ABORT, 'migration proposal approvals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_migration_proposal_approval_delete
    BEFORE DELETE ON migration_proposal_approvals
    BEGIN SELECT RAISE(ABORT, 'migration proposal approvals cannot be deleted'); END;
CREATE TABLE IF NOT EXISTS migration_self_attestations (
    task_id TEXT PRIMARY KEY REFERENCES migration_tasks(task_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    target_id TEXT NOT NULL REFERENCES migration_targets(target_id),
    target_identity_digest TEXT NOT NULL CHECK (
        length(target_identity_digest) = 64 AND target_identity_digest NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_digest TEXT NOT NULL CHECK (
        length(manifest_digest) = 64 AND manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    gates_json TEXT NOT NULL,
    gates_digest TEXT NOT NULL CHECK (
        length(gates_digest) = 64 AND gates_digest NOT GLOB '*[^0-9a-f]*'
    ),
    completed_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    )
);
CREATE INDEX IF NOT EXISTS idx_migration_self_attestations_subject_time
    ON migration_self_attestations(subject_id, completed_at DESC);
CREATE TRIGGER IF NOT EXISTS prevent_migration_self_attestation_update
    BEFORE UPDATE ON migration_self_attestations
    BEGIN SELECT RAISE(ABORT, 'migration self-attestations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_migration_self_attestation_delete
    BEFORE DELETE ON migration_self_attestations
    BEGIN SELECT RAISE(ABORT, 'migration self-attestations cannot be deleted'); END;
""",
    97: """
DROP TRIGGER IF EXISTS prevent_migration_proposal_approval_update;
DROP TRIGGER IF EXISTS prevent_migration_proposal_approval_delete;
DROP INDEX IF EXISTS idx_migration_proposal_approvals_subject;
DROP INDEX IF EXISTS idx_migration_proposal_approvals_proposal_binding;
ALTER TABLE migration_proposal_approvals RENAME TO migration_proposal_approvals_v96;
CREATE TABLE migration_proposal_approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES migration_proposals(proposal_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    identity_binding TEXT CHECK (
        identity_binding IS NULL OR (
            length(identity_binding) = 64 AND identity_binding NOT GLOB '*[^0-9a-f]*'
        )
    ),
    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    )
);
INSERT INTO migration_proposal_approvals(
    approval_id, proposal_id, subject_id, actor, identity_binding,
    policy_revision, created_at, state_hash
)
SELECT approval_id, proposal_id, subject_id, actor, NULL,
       policy_revision, created_at, state_hash
FROM migration_proposal_approvals_v96;
DROP TABLE migration_proposal_approvals_v96;
CREATE INDEX idx_migration_proposal_approvals_subject
    ON migration_proposal_approvals(subject_id, proposal_id, created_at);
CREATE UNIQUE INDEX idx_migration_proposal_approvals_proposal_binding
    ON migration_proposal_approvals(proposal_id, identity_binding)
    WHERE identity_binding IS NOT NULL;
CREATE TRIGGER prevent_migration_proposal_approval_update
    BEFORE UPDATE ON migration_proposal_approvals
    BEGIN SELECT RAISE(ABORT, 'migration proposal approvals are append-only'); END;
CREATE TRIGGER prevent_migration_proposal_approval_delete
    BEFORE DELETE ON migration_proposal_approvals
    BEGIN SELECT RAISE(ABORT, 'migration proposal approvals cannot be deleted'); END;
""",
    98: """
CREATE TABLE IF NOT EXISTS backup_retention_policies (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    settings_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    )
);
CREATE TABLE IF NOT EXISTS backup_retention_runs (
    run_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    plan_id TEXT NOT NULL CHECK (length(plan_id) = 64 AND plan_id NOT GLOB '*[^0-9a-f]*'),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('planned', 'applied', 'failed')),
    cursor_json TEXT NOT NULL,
    deleted_count INTEGER NOT NULL DEFAULT 0 CHECK (deleted_count >= 0),
    failure_code TEXT,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    )
);
CREATE INDEX IF NOT EXISTS idx_backup_retention_runs_subject_time
    ON backup_retention_runs(subject_id, started_at DESC);
""",
    99: """
CREATE TABLE IF NOT EXISTS model_capability_profiles (
    profile_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    provider_type TEXT NOT NULL,
    endpoint_hash TEXT NOT NULL CHECK (
        length(endpoint_hash) = 64 AND endpoint_hash NOT GLOB '*[^0-9a-f]*'
    ),
    model TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    source TEXT NOT NULL CHECK (source IN ('probe', 'operator', 'server_profile')),
    status TEXT NOT NULL CHECK (status IN ('known', 'unknown', 'stale', 'failed')),
    observed_at TEXT NOT NULL,
    expires_at TEXT,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(subject_id, provider_type, endpoint_hash, model)
);
CREATE INDEX IF NOT EXISTS idx_model_capability_profiles_subject
    ON model_capability_profiles(subject_id, provider_type, model);
""",
    100: """
CREATE TABLE IF NOT EXISTS memory_embedding_inputs (
    memory_id TEXT PRIMARY KEY REFERENCES memory_embeddings(memory_id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'
    )
);
CREATE INDEX IF NOT EXISTS idx_waiting_cognitive_task_revisions_task
    ON waiting_cognitive_task_revisions(task_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_subject_purpose_pool
    ON model_calls(subject_id, purpose, resource_pool);
""",
    101: """
CREATE INDEX IF NOT EXISTS idx_audit_subject_action_call
    ON audit_records(subject_id, action, json_extract(payload_json, '$.call_id'))
    WHERE json_valid(payload_json);
""",
    102: """
-- embedding resource configuration fingerprint is added idempotently below.
""",
    103: """
-- observed embedding dimensions are added idempotently below.
""",
    104: PARTICIPANT_SCHEMA,
    105: """
CREATE INDEX IF NOT EXISTS idx_participant_messages_timeline
 ON participant_message_links(subject_id,entity_id,interaction_created_at DESC,interaction_id DESC);
CREATE INDEX IF NOT EXISTS idx_interactions_correspondent_timeline
 ON interactions(subject_id,counterparty,created_at DESC,interaction_id DESC);
CREATE INDEX IF NOT EXISTS idx_relationships_social_priority
 ON relationships(subject_id,familiarity DESC,affinity DESC,updated_at DESC,relationship_id DESC);
CREATE TRIGGER IF NOT EXISTS validate_participant_message_time
BEFORE INSERT ON participant_message_links
WHEN EXISTS(SELECT 1 FROM interactions i WHERE i.interaction_id=NEW.interaction_id
 AND i.subject_id=NEW.subject_id AND i.created_at<>NEW.interaction_created_at)
BEGIN SELECT RAISE(ABORT,'participant message time mismatch'); END;
""",
    107: RELATIONSHIP_IDENTITY_SCHEMA,
    106: """
CREATE TRIGGER IF NOT EXISTS prevent_goal_origin_update
BEFORE UPDATE OF goal_id,subject_id,origin,created_at,origin_evidence,origin_evidence_hash ON goals
WHEN NEW.goal_id IS NOT OLD.goal_id OR NEW.subject_id IS NOT OLD.subject_id
 OR NEW.origin IS NOT OLD.origin OR NEW.created_at IS NOT OLD.created_at
 OR NEW.origin_evidence IS NOT OLD.origin_evidence
 OR NEW.origin_evidence_hash IS NOT OLD.origin_evidence_hash
BEGIN SELECT RAISE(ABORT,'goal origin evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_goal_origin_evidence_insert
BEFORE INSERT ON goals WHEN NEW.origin_evidence<>'created'
BEGIN SELECT RAISE(ABORT,'legacy goal origin evidence is migration-only'); END;
""",
}
