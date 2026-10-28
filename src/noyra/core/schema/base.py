"""Base definitions; execution remains in core.database."""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subject_identity (
    subject_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    genesis_hash TEXT NOT NULL,
    personal_name TEXT,
    identity_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    model_name TEXT,
    last_checkpoint TEXT REFERENCES state_snapshots(snapshot_id),
    origin_subject_id TEXT,
    branch_reason TEXT
);

CREATE TABLE IF NOT EXISTS runtime_state (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    state TEXT NOT NULL CHECK (
        state IN (
            'booting', 'orienting', 'active', 'paused', 'winding_down',
            'reflective_sleep', 'deep_sleep', 'waking', 'stopped', 'resetting'
        )
    ),
    reason TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    privacy_level TEXT NOT NULL,
    causal_parent_ids_json TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'recorded'
        CHECK (processing_status IN ('recorded', 'processed', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_events_subject_time ON events(subject_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_subject_export_time
    ON events(subject_id, occurred_at, event_id);
CREATE INDEX IF NOT EXISTS idx_events_processing ON events(subject_id, processing_status);

CREATE TABLE IF NOT EXISTS state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_subject_version
    ON state_snapshots(subject_id, state_version DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_snapshots_subject_version
    ON state_snapshots(subject_id, state_version);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    goal_id TEXT,
    strategy_id TEXT,
    action_type TEXT NOT NULL,
    tool TEXT NOT NULL,
    target TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    expected_outcome TEXT NOT NULL,
    side_effect INTEGER NOT NULL CHECK (side_effect IN (0, 1)),
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'executing', 'succeeded', 'failed', 'unknown', 'cancelled')
    ),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    resource_cost_json TEXT NOT NULL,
    result_json TEXT,
    prepared_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_subject_status ON actions(subject_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_actions_subject_idempotency
    ON actions(subject_id, idempotency_key);

CREATE TABLE IF NOT EXISTS behavior_logs (
    log_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    occurred_at TEXT NOT NULL,
    action_type TEXT NOT NULL,
    public_goal_reference TEXT,
    tool TEXT NOT NULL,
    public_target TEXT NOT NULL,
    result_status TEXT NOT NULL CHECK (
        result_status IN ('succeeded', 'failed', 'unknown', 'cancelled')
    ),
    side_effect_summary TEXT NOT NULL,
    resource_summary TEXT NOT NULL,
    public_explanation TEXT NOT NULL,
    redaction_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_behavior_subject_time ON behavior_logs(subject_id, occurred_at);

CREATE TABLE IF NOT EXISTS audit_records (
    audit_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_subject_time ON audit_records(subject_id, occurred_at);
CREATE TRIGGER IF NOT EXISTS prevent_audit_record_update
BEFORE UPDATE ON audit_records BEGIN
    SELECT RAISE(ABORT, 'audit records are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_audit_record_delete
BEFORE DELETE ON audit_records BEGIN
    SELECT RAISE(ABORT, 'audit records cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS training_policies (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    record_enabled INTEGER NOT NULL DEFAULT 1 CHECK (record_enabled IN (0, 1)),
    export_enabled INTEGER NOT NULL DEFAULT 1 CHECK (export_enabled IN (0, 1)),
    include_private_psychology INTEGER NOT NULL DEFAULT 0
        CHECK (include_private_psychology IN (0, 1)),
    include_conversations INTEGER NOT NULL DEFAULT 0 CHECK (include_conversations IN (0, 1)),
    include_model_io INTEGER NOT NULL DEFAULT 0 CHECK (include_model_io IN (0, 1)),
    include_external_actions INTEGER NOT NULL DEFAULT 1 CHECK (include_external_actions IN (0, 1)),
    include_workspace INTEGER NOT NULL DEFAULT 0 CHECK (include_workspace IN (0, 1)),
    policy_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_version > 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS training_records (
    record_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    record_kind TEXT NOT NULL,
    privacy_level TEXT NOT NULL,
    eligibility TEXT NOT NULL CHECK (eligibility IN ('eligible', 'restricted', 'excluded')),
    source_hash TEXT NOT NULL,
    redaction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (redaction_status IN ('pending', 'redacted', 'clear', 'excluded')),
    consent_version INTEGER NOT NULL DEFAULT 1 CHECK (consent_version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_training_records_subject_time
    ON training_records(subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_training_records_subject_export_time
    ON training_records(subject_id, created_at, record_id);
CREATE INDEX IF NOT EXISTS idx_training_records_eligibility
    ON training_records(subject_id, eligibility, redaction_status);
CREATE TABLE IF NOT EXISTS storage_archives (
    archive_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    storage_class TEXT NOT NULL CHECK (storage_class IN ('warm', 'cold', 'cloud')),
    object_key TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'uploaded', 'verified', 'failed', 'restored')
    ),
    created_at TEXT NOT NULL,
    verified_at TEXT,
    UNIQUE(subject_id, object_key)
);
CREATE INDEX IF NOT EXISTS idx_storage_archives_subject_status
    ON storage_archives(subject_id, status, created_at DESC);
CREATE TABLE IF NOT EXISTS training_exports (
    export_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    format TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    consent_version INTEGER NOT NULL CHECK (consent_version > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_training_exports_subject_time
    ON training_exports(subject_id, created_at DESC);
CREATE TRIGGER IF NOT EXISTS prevent_training_record_update
BEFORE UPDATE OF event_id, subject_id, source_hash ON training_records BEGIN
    SELECT RAISE(ABORT, 'training record provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_training_record_delete
BEFORE DELETE ON training_records BEGIN
    SELECT RAISE(ABORT, 'training records cannot be deleted');
END;

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1');
"""


CURRENT_SCHEMA_VERSION = 109
