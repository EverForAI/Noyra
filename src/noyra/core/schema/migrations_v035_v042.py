"""Migrations v035 v042 definitions; execution remains in core.database."""

from __future__ import annotations

MIGRATIONS_V035_V042: dict[int, str] = {
    35: """
CREATE TABLE IF NOT EXISTS archive_keyring_revisions (
    revision_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    generation INTEGER NOT NULL CHECK (generation > 0),
    format TEXT NOT NULL CHECK (format = 'noyra-archive-keyring/v1'),
    active_key_id TEXT NOT NULL,
    legacy_key_id TEXT,
    key_metadata_json TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, generation)
);
CREATE INDEX IF NOT EXISTS idx_archive_keyring_subject_generation
    ON archive_keyring_revisions(subject_id, generation DESC);
CREATE TRIGGER IF NOT EXISTS validate_archive_keyring_generation_insert
BEFORE INSERT ON archive_keyring_revisions
WHEN NEW.generation != COALESCE(
    (SELECT MAX(generation) + 1 FROM archive_keyring_revisions
     WHERE subject_id = NEW.subject_id),
    1
)
BEGIN
    SELECT RAISE(ABORT, 'archive keyring generation must be sequential');
END;
CREATE TRIGGER IF NOT EXISTS prevent_archive_keyring_revision_update
BEFORE UPDATE ON archive_keyring_revisions BEGIN
    SELECT RAISE(ABORT, 'archive keyring revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_archive_keyring_revision_delete
BEFORE DELETE ON archive_keyring_revisions BEGIN
    SELECT RAISE(ABORT, 'archive keyring revisions are append-only');
END;

CREATE TABLE IF NOT EXISTS archive_object_replicas (
    replica_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    object_key TEXT NOT NULL,
    object_kind TEXT NOT NULL CHECK (
        object_kind IN ('event_segment', 'observation_segment')
    ),
    replica_type TEXT NOT NULL CHECK (replica_type IN ('local', 'cloud')),
    provider_id TEXT NOT NULL,
    ciphertext_hash TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    state TEXT NOT NULL CHECK (
        state IN (
            'pending', 'uploading', 'present', 'verified', 'gc_pending',
            'absent', 'restoring', 'unavailable', 'missing', 'corrupt'
        )
    ),
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    verified_at TEXT,
    last_accessed_at TEXT,
    removed_at TEXT,
    restored_at TEXT,
    last_error_code TEXT,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, object_key, replica_type, provider_id)
);
CREATE INDEX IF NOT EXISTS idx_archive_replicas_subject_state
    ON archive_object_replicas(subject_id, replica_type, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_archive_replicas_subject_key
    ON archive_object_replicas(subject_id, object_key);
CREATE TRIGGER IF NOT EXISTS prevent_archive_replica_delete
BEFORE DELETE ON archive_object_replicas BEGIN
    SELECT RAISE(ABORT, 'archive replicas cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS archive_object_replica_revisions (
    revision_id TEXT PRIMARY KEY,
    replica_id TEXT NOT NULL REFERENCES archive_object_replicas(replica_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    object_key TEXT NOT NULL,
    object_kind TEXT NOT NULL CHECK (
        object_kind IN ('event_segment', 'observation_segment')
    ),
    replica_type TEXT NOT NULL CHECK (replica_type IN ('local', 'cloud')),
    provider_id TEXT NOT NULL,
    ciphertext_hash TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    state TEXT NOT NULL CHECK (
        state IN (
            'pending', 'uploading', 'present', 'verified', 'gc_pending',
            'absent', 'restoring', 'unavailable', 'missing', 'corrupt'
        )
    ),
    verified_at TEXT,
    last_accessed_at TEXT,
    removed_at TEXT,
    restored_at TEXT,
    last_error_code TEXT,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(replica_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_archive_replica_revisions_parent
    ON archive_object_replica_revisions(replica_id, revision_number);
CREATE TRIGGER IF NOT EXISTS validate_archive_replica_revision_subject_insert
BEFORE INSERT ON archive_object_replica_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM archive_object_replicas r
    WHERE r.replica_id = NEW.replica_id AND r.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'archive replica revision ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_archive_replica_revision_sequence_insert
BEFORE INSERT ON archive_object_replica_revisions
WHEN NEW.revision_number != COALESCE(
    (SELECT MAX(revision_number) + 1 FROM archive_object_replica_revisions
     WHERE replica_id = NEW.replica_id),
    1
)
BEGIN
    SELECT RAISE(ABORT, 'archive replica revision must be sequential');
END;
CREATE TRIGGER IF NOT EXISTS prevent_archive_replica_revision_update
BEFORE UPDATE ON archive_object_replica_revisions BEGIN
    SELECT RAISE(ABORT, 'archive replica revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_archive_replica_revision_delete
BEFORE DELETE ON archive_object_replica_revisions BEGIN
    SELECT RAISE(ABORT, 'archive replica revisions are append-only');
END;

CREATE TABLE IF NOT EXISTS storage_usage_samples (
    sample_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    subject_bytes INTEGER NOT NULL CHECK (subject_bytes >= 0),
    effective_subject_bytes INTEGER NOT NULL CHECK (effective_subject_bytes >= 0),
    database_bytes INTEGER NOT NULL CHECK (database_bytes >= 0),
    database_reclaimable_bytes INTEGER NOT NULL CHECK (database_reclaimable_bytes >= 0),
    wal_bytes INTEGER NOT NULL CHECK (wal_bytes >= 0),
    local_archive_bytes INTEGER NOT NULL CHECK (local_archive_bytes >= 0),
    cloud_staging_bytes INTEGER NOT NULL CHECK (cloud_staging_bytes >= 0),
    exports_bytes INTEGER NOT NULL CHECK (exports_bytes >= 0),
    training_bytes INTEGER NOT NULL CHECK (training_bytes >= 0),
    workspace_bytes INTEGER NOT NULL CHECK (workspace_bytes >= 0),
    free_bytes INTEGER NOT NULL CHECK (free_bytes >= 0),
    subject_quota_bytes INTEGER NOT NULL CHECK (subject_quota_bytes > 0),
    minimum_free_bytes INTEGER NOT NULL CHECK (minimum_free_bytes > 0),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, created_at)
);
CREATE INDEX IF NOT EXISTS idx_storage_usage_samples_subject_time
    ON storage_usage_samples(subject_id, created_at DESC);
CREATE TRIGGER IF NOT EXISTS prevent_storage_usage_sample_update
BEFORE UPDATE ON storage_usage_samples BEGIN
    SELECT RAISE(ABORT, 'storage usage samples are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_storage_usage_sample_delete
BEFORE DELETE ON storage_usage_samples BEGIN
    SELECT RAISE(ABORT, 'storage usage samples are append-only');
END;
""",
    36: """
CREATE TABLE IF NOT EXISTS interaction_delivery_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES interaction_deliveries(delivery_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    source TEXT NOT NULL CHECK (source IN ('provider', 'operator')),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('delivered', 'failed', 'cancelled', 'unknown', 'unsupported', 'error')
    ),
    provider_message_id TEXT,
    provider_status TEXT,
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(delivery_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_delivery_reconciliations_subject_time
    ON interaction_delivery_reconciliations(subject_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_terminal_reconciliation
    ON interaction_delivery_reconciliations(delivery_id)
    WHERE outcome IN ('delivered', 'failed', 'cancelled');
CREATE TRIGGER IF NOT EXISTS validate_delivery_reconciliation_ownership
BEFORE INSERT ON interaction_delivery_reconciliations
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_deliveries d
    WHERE d.delivery_id = NEW.delivery_id AND d.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'delivery reconciliation ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_delivery_reconciliation_unknown
BEFORE INSERT ON interaction_delivery_reconciliations
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_deliveries d
    WHERE d.delivery_id = NEW.delivery_id AND d.status = 'unknown'
)
BEGIN
    SELECT RAISE(ABORT, 'only unknown deliveries can be reconciled');
END;
CREATE TRIGGER IF NOT EXISTS validate_delivery_reconciliation_sequence
BEFORE INSERT ON interaction_delivery_reconciliations
WHEN NEW.sequence != COALESCE(
    (SELECT MAX(r.sequence) + 1 FROM interaction_delivery_reconciliations r
     WHERE r.delivery_id = NEW.delivery_id),
    1
)
BEGIN
    SELECT RAISE(ABORT, 'delivery reconciliation sequence mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_delivery_reconciliation_after_terminal
BEFORE INSERT ON interaction_delivery_reconciliations
WHEN EXISTS (
    SELECT 1 FROM interaction_delivery_reconciliations r
    WHERE r.delivery_id = NEW.delivery_id
      AND r.outcome IN ('delivered', 'failed', 'cancelled')
)
BEGIN
    SELECT RAISE(ABORT, 'delivery reconciliation is already terminal');
END;
CREATE TRIGGER IF NOT EXISTS validate_operator_delivery_reconciliation
BEFORE INSERT ON interaction_delivery_reconciliations
WHEN NEW.source = 'operator' AND NEW.outcome NOT IN ('delivered', 'failed', 'cancelled')
BEGIN
    SELECT RAISE(ABORT, 'operator reconciliation must be terminal');
END;
CREATE TRIGGER IF NOT EXISTS validate_provider_delivery_reconciliation
BEFORE INSERT ON interaction_delivery_reconciliations
WHEN NEW.source = 'provider' AND NEW.outcome = 'cancelled'
BEGIN
    SELECT RAISE(ABORT, 'provider reconciliation cannot cancel delivery');
END;
CREATE TRIGGER IF NOT EXISTS prevent_delivery_reconciliation_update
BEFORE UPDATE ON interaction_delivery_reconciliations BEGIN
    SELECT RAISE(ABORT, 'delivery reconciliations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_delivery_reconciliation_delete
BEFORE DELETE ON interaction_delivery_reconciliations BEGIN
    SELECT RAISE(ABORT, 'delivery reconciliations cannot be deleted');
END;
""",
    37: """
CREATE TABLE IF NOT EXISTS common_knowledge_import_provenance (
    provenance_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL UNIQUE REFERENCES common_knowledge_imports(import_id),
    package_id TEXT NOT NULL REFERENCES common_knowledge_packages(package_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    envelope_json TEXT NOT NULL,
    envelope_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    signer_key_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    UNIQUE(package_id, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_provenance_subject
    ON common_knowledge_import_provenance(subject_id, verified_at DESC);
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_provenance_ownership
BEFORE INSERT ON common_knowledge_import_provenance
WHEN NOT EXISTS (
    SELECT 1 FROM common_knowledge_imports i
    WHERE i.import_id = NEW.import_id
      AND i.package_id = NEW.package_id
      AND i.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge provenance ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_provenance_package
BEFORE INSERT ON common_knowledge_import_provenance
WHEN NOT EXISTS (
    SELECT 1 FROM common_knowledge_packages p
    WHERE p.package_id = NEW.package_id
      AND p.payload_hash = NEW.payload_hash
      AND p.key_id = NEW.signer_key_id
      AND p.signature = NEW.signature
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge provenance package mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_provenance_update
BEFORE UPDATE ON common_knowledge_import_provenance BEGIN
    SELECT RAISE(ABORT, 'common knowledge provenance is append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_provenance_delete
BEFORE DELETE ON common_knowledge_import_provenance BEGIN
    SELECT RAISE(ABORT, 'common knowledge provenance cannot be deleted');
END;
""",
    38: """
CREATE TABLE IF NOT EXISTS autonomous_project_execution_clock_events (
    clock_event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    session_id TEXT NOT NULL,
    execution_kind TEXT NOT NULL CHECK (
        execution_kind IN ('project_review', 'phase_execution')
    ),
    action TEXT NOT NULL CHECK (action IN ('start', 'stop', 'recover')),
    active_delta_seconds REAL NOT NULL CHECK (active_delta_seconds >= 0),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(project_id, sequence),
    UNIQUE(session_id, action)
);
CREATE INDEX IF NOT EXISTS idx_project_execution_clock_subject
    ON autonomous_project_execution_clock_events(subject_id, project_id, sequence);
CREATE TRIGGER IF NOT EXISTS validate_project_execution_clock_ownership
BEFORE INSERT ON autonomous_project_execution_clock_events
WHEN NOT EXISTS (
    SELECT 1 FROM autonomous_projects p
    WHERE p.project_id = NEW.project_id AND p.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'project execution clock ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_project_execution_clock_sequence
BEFORE INSERT ON autonomous_project_execution_clock_events
WHEN NEW.sequence != COALESCE(
    (SELECT MAX(e.sequence) + 1 FROM autonomous_project_execution_clock_events e
     WHERE e.project_id = NEW.project_id),
    1
)
BEGIN
    SELECT RAISE(ABORT, 'project execution clock sequence mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_project_execution_clock_start
BEFORE INSERT ON autonomous_project_execution_clock_events
WHEN NEW.action = 'start' AND EXISTS (
    SELECT 1 FROM autonomous_project_execution_clock_events previous
    WHERE previous.project_id = NEW.project_id
      AND previous.sequence = NEW.sequence - 1
      AND previous.action = 'start'
)
BEGIN
    SELECT RAISE(ABORT, 'project execution clock session is already active');
END;
CREATE TRIGGER IF NOT EXISTS validate_project_execution_clock_finish
BEFORE INSERT ON autonomous_project_execution_clock_events
WHEN NEW.action IN ('stop', 'recover') AND NOT EXISTS (
    SELECT 1 FROM autonomous_project_execution_clock_events previous
    WHERE previous.project_id = NEW.project_id
      AND previous.sequence = NEW.sequence - 1
      AND previous.action = 'start'
      AND previous.session_id = NEW.session_id
      AND previous.execution_kind = NEW.execution_kind
)
BEGIN
    SELECT RAISE(ABORT, 'project execution clock finish has no matching start');
END;
CREATE TRIGGER IF NOT EXISTS validate_project_execution_clock_delta
BEFORE INSERT ON autonomous_project_execution_clock_events
WHEN (NEW.action IN ('start', 'recover') AND NEW.active_delta_seconds != 0)
  OR (NEW.action = 'stop' AND NEW.active_delta_seconds < 0)
BEGIN
    SELECT RAISE(ABORT, 'project execution clock delta is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_project_execution_clock_update
BEFORE UPDATE ON autonomous_project_execution_clock_events BEGIN
    SELECT RAISE(ABORT, 'project execution clock is append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_project_execution_clock_delete
BEFORE DELETE ON autonomous_project_execution_clock_events BEGIN
    SELECT RAISE(ABORT, 'project execution clock cannot be deleted');
END;
""",
    39: """
CREATE INDEX IF NOT EXISTS idx_runtime_logs_events_keyset
    ON events(subject_id, occurred_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_model_calls_keyset
    ON model_calls(subject_id, created_at DESC, call_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_actions_keyset
    ON actions(subject_id, prepared_at DESC, action_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_epistemic_keyset
    ON epistemic_review_runs(subject_id, created_at DESC, review_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_relationship_keyset
    ON relationship_social_runs(subject_id, created_at DESC, social_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_self_models_keyset
    ON self_models(subject_id, created_at DESC, self_model_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_thoughts_keyset
    ON thought_episodes(subject_id, created_at DESC, thought_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_metacognition_keyset
    ON metacognitive_decisions(subject_id, created_at DESC, decision_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_motivation_keyset
    ON motivation_reviews(subject_id, created_at DESC, review_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_consciousness_keyset
    ON consciousness_frames(subject_id, created_at DESC, frame_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_routing_keyset
    ON cognitive_route_decisions(subject_id, created_at DESC, decision_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_projects_keyset
    ON autonomous_project_reviews(subject_id, created_at DESC, review_id DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_logs_audit_keyset
    ON audit_records(subject_id, occurred_at DESC, audit_id DESC);
""",
    40: """
CREATE TABLE IF NOT EXISTS subject_storage_keys (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    storage_key TEXT NOT NULL UNIQUE,
    CHECK (
        length(storage_key) = 40
        AND substr(storage_key, 1, 8) = 'subject_'
        AND substr(storage_key, 9) NOT GLOB '*[^0-9a-f]*'
        AND storage_key != subject_id COLLATE NOCASE
    )
);
INSERT OR IGNORE INTO subject_storage_keys(subject_id, storage_key)
SELECT subject_id, 'subject_' || lower(hex(randomblob(16)))
FROM subject_identity;
CREATE TRIGGER IF NOT EXISTS prevent_subject_storage_key_update
BEFORE UPDATE ON subject_storage_keys BEGIN
    SELECT RAISE(ABORT, 'subject storage keys are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_subject_storage_key_delete
BEFORE DELETE ON subject_storage_keys BEGIN
    SELECT RAISE(ABORT, 'subject storage keys cannot be deleted');
END;
""",
    41: """
CREATE TABLE IF NOT EXISTS common_knowledge_versions (
    package_id TEXT NOT NULL REFERENCES common_knowledge_packages(package_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    publisher_subject_id TEXT NOT NULL,
    series_id TEXT NOT NULL,
    package_version INTEGER NOT NULL CHECK (package_version > 0),
    predecessor_package_id TEXT REFERENCES common_knowledge_packages(package_id),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(package_id, subject_id),
    UNIQUE(subject_id, publisher_subject_id, series_id, package_version)
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_versions_series
    ON common_knowledge_versions(subject_id, publisher_subject_id, series_id, package_version);
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_version_package
BEFORE INSERT ON common_knowledge_versions
WHEN NOT EXISTS (
    SELECT 1 FROM common_knowledge_packages p
    WHERE p.package_id = NEW.package_id
      AND p.publisher_subject_id = NEW.publisher_subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge version package mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_version_predecessor
BEFORE INSERT ON common_knowledge_versions
WHEN NEW.predecessor_package_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM common_knowledge_versions previous
    WHERE previous.package_id = NEW.predecessor_package_id
      AND previous.subject_id = NEW.subject_id
      AND previous.publisher_subject_id = NEW.publisher_subject_id
      AND previous.series_id = NEW.series_id
      AND previous.package_version < NEW.package_version
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge version predecessor mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_version_update
BEFORE UPDATE ON common_knowledge_versions BEGIN
    SELECT RAISE(ABORT, 'common knowledge versions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_version_delete
BEFORE DELETE ON common_knowledge_versions BEGIN
    SELECT RAISE(ABORT, 'common knowledge versions cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS common_knowledge_sync_events (
    event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    publisher_subject_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL CHECK (event_type IN ('published', 'revoked')),
    package_id TEXT NOT NULL REFERENCES common_knowledge_packages(package_id),
    key_id TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(subject_id, sequence),
    UNIQUE(subject_id, package_id, event_type)
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_sync_events_feed
    ON common_knowledge_sync_events(subject_id, sequence, event_id);
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_sync_event_package
BEFORE INSERT ON common_knowledge_sync_events
WHEN NEW.subject_id != NEW.publisher_subject_id OR NOT EXISTS (
    SELECT 1 FROM common_knowledge_packages p
    WHERE p.package_id = NEW.package_id
      AND p.publisher_subject_id = NEW.publisher_subject_id
      AND p.key_id = NEW.key_id
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge sync event package mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_sync_event_sequence
BEFORE INSERT ON common_knowledge_sync_events
WHEN NEW.sequence != COALESCE(
    (SELECT MAX(sequence) + 1 FROM common_knowledge_sync_events
     WHERE subject_id = NEW.subject_id),
    1
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge sync event sequence mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_sync_event_update
BEFORE UPDATE ON common_knowledge_sync_events BEGIN
    SELECT RAISE(ABORT, 'common knowledge sync events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_sync_event_delete
BEFORE DELETE ON common_knowledge_sync_events BEGIN
    SELECT RAISE(ABORT, 'common knowledge sync events cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS common_knowledge_peers (
    peer_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    label TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    publisher_subject_id TEXT NOT NULL,
    key_id TEXT NOT NULL REFERENCES common_knowledge_trusted_keys(key_id),
    public_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    cursor INTEGER NOT NULL CHECK (cursor >= 0),
    remote_etag TEXT,
    sync_interval_seconds INTEGER NOT NULL CHECK (
        sync_interval_seconds BETWEEN 60 AND 86400
    ),
    next_sync_at TEXT NOT NULL,
    last_sync_at TEXT,
    last_error_code TEXT,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, endpoint),
    UNIQUE(subject_id, publisher_subject_id, key_id)
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_peers_due
    ON common_knowledge_peers(subject_id, status, next_sync_at, peer_id);

CREATE TABLE IF NOT EXISTS common_knowledge_remote_events (
    peer_id TEXT NOT NULL REFERENCES common_knowledge_peers(peer_id),
    event_id TEXT NOT NULL,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    publisher_subject_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL CHECK (event_type IN ('published', 'revoked')),
    package_id TEXT NOT NULL REFERENCES common_knowledge_packages(package_id),
    event_json TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(peer_id, event_id),
    UNIQUE(peer_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_remote_events_subject
    ON common_knowledge_remote_events(subject_id, peer_id, sequence);
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_remote_event_peer
BEFORE INSERT ON common_knowledge_remote_events
WHEN NOT EXISTS (
    SELECT 1 FROM common_knowledge_peers peer
    WHERE peer.peer_id = NEW.peer_id
      AND peer.subject_id = NEW.subject_id
      AND peer.publisher_subject_id = NEW.publisher_subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge remote event peer mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_remote_event_update
BEFORE UPDATE ON common_knowledge_remote_events BEGIN
    SELECT RAISE(ABORT, 'common knowledge remote events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_remote_event_delete
BEFORE DELETE ON common_knowledge_remote_events BEGIN
    SELECT RAISE(ABORT, 'common knowledge remote events cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS common_knowledge_sync_runs (
    sync_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL REFERENCES common_knowledge_peers(peer_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'partial', 'unchanged', 'failed')),
    cursor_before INTEGER NOT NULL CHECK (cursor_before >= 0),
    cursor_after INTEGER NOT NULL CHECK (cursor_after >= cursor_before),
    discovered INTEGER NOT NULL CHECK (discovered >= 0),
    imported INTEGER NOT NULL CHECK (imported >= 0),
    revoked INTEGER NOT NULL CHECK (revoked >= 0),
    error_code TEXT,
    state_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_sync_runs_subject
    ON common_knowledge_sync_runs(subject_id, occurred_at DESC, sync_id);
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_sync_run_peer
BEFORE INSERT ON common_knowledge_sync_runs
WHEN NOT EXISTS (
    SELECT 1 FROM common_knowledge_peers peer
    WHERE peer.peer_id = NEW.peer_id AND peer.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge sync run peer mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_sync_run_update
BEFORE UPDATE ON common_knowledge_sync_runs BEGIN
    SELECT RAISE(ABORT, 'common knowledge sync runs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_sync_run_delete
BEFORE DELETE ON common_knowledge_sync_runs BEGIN
    SELECT RAISE(ABORT, 'common knowledge sync runs cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS common_knowledge_evaluation_events (
    evaluation_event_id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL,
    package_id TEXT NOT NULL REFERENCES common_knowledge_packages(package_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    sequence INTEGER NOT NULL CHECK (sequence IN (1, 2)),
    status TEXT NOT NULL CHECK (status IN ('requested', 'accepted', 'rejected')),
    envelope_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(evaluation_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_evaluations_subject
    ON common_knowledge_evaluation_events(subject_id, occurred_at DESC, evaluation_id, sequence);
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_evaluation_import
BEFORE INSERT ON common_knowledge_evaluation_events
WHEN NOT EXISTS (
    SELECT 1 FROM common_knowledge_imports i
    WHERE i.package_id = NEW.package_id AND i.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'common knowledge evaluation import mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_evaluation_sequence
BEFORE INSERT ON common_knowledge_evaluation_events
WHEN (NEW.sequence = 1 AND (
        NEW.status != 'requested'
        OR EXISTS (SELECT 1 FROM common_knowledge_evaluation_events e
                   WHERE e.evaluation_id = NEW.evaluation_id)
    ))
    OR (NEW.sequence = 2 AND (
        NEW.status NOT IN ('accepted', 'rejected')
        OR NOT EXISTS (
            SELECT 1 FROM common_knowledge_evaluation_events requested
            WHERE requested.evaluation_id = NEW.evaluation_id
              AND requested.sequence = 1
              AND requested.status = 'requested'
              AND requested.package_id = NEW.package_id
              AND requested.subject_id = NEW.subject_id
              AND requested.envelope_hash = NEW.envelope_hash
        )
    ))
BEGIN
    SELECT RAISE(ABORT, 'common knowledge evaluation sequence mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_evaluation_update
BEFORE UPDATE ON common_knowledge_evaluation_events BEGIN
    SELECT RAISE(ABORT, 'common knowledge evaluations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_evaluation_delete
BEFORE DELETE ON common_knowledge_evaluation_events BEGIN
    SELECT RAISE(ABORT, 'common knowledge evaluations cannot be deleted');
END;
""",
    42: """
CREATE TABLE IF NOT EXISTS secret_file_intents (
    intent_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    secret_reference TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'delete')),
    fingerprint TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'file_ready', 'committed', 'pending', 'failed', 'removed')
    ),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(resource_type, resource_id, secret_reference, operation)
);
CREATE INDEX IF NOT EXISTS idx_secret_file_intents_subject_state
    ON secret_file_intents(subject_id, resource_type, state, updated_at);
DROP TRIGGER IF EXISTS validate_secret_file_intent_reference_binding;
CREATE TRIGGER validate_secret_file_intent_reference_binding
BEFORE INSERT ON secret_file_intents
WHEN EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.secret_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'transport'
               AND t.transport_id = NEW.resource_id AND t.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM search_provider_configs s
    WHERE s.key_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'search'
               AND s.config_id = NEW.resource_id AND s.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM cognitive_resource_keys c
    WHERE c.key_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'cognitive'
               AND c.key_id = NEW.resource_id AND c.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM embedding_resources e
    WHERE e.config_id || '.key' = NEW.secret_reference
      AND NOT (NEW.resource_type = 'embedding'
               AND e.config_id = NEW.resource_id AND e.subject_id = NEW.subject_id)
)
BEGIN
    SELECT RAISE(ABORT, 'secret file reference is already bound to another resource');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_intent_binding;
CREATE TRIGGER validate_secret_file_intent_intent_binding
BEFORE INSERT ON secret_file_intents
WHEN EXISTS (
    SELECT 1 FROM secret_file_intents i
    WHERE i.secret_reference = NEW.secret_reference
      AND NOT (
          i.subject_id = NEW.subject_id
          AND i.resource_type = NEW.resource_type
          AND i.resource_id = NEW.resource_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'secret file reference is already bound to another intent');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_reference_binding_update;
CREATE TRIGGER validate_secret_file_intent_reference_binding_update
BEFORE UPDATE OF subject_id, resource_type, resource_id, secret_reference ON secret_file_intents
WHEN EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.secret_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'transport'
               AND t.transport_id = NEW.resource_id AND t.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM search_provider_configs s
    WHERE s.key_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'search'
               AND s.config_id = NEW.resource_id AND s.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM cognitive_resource_keys c
    WHERE c.key_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'cognitive'
               AND c.key_id = NEW.resource_id AND c.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM embedding_resources e
    WHERE e.config_id || '.key' = NEW.secret_reference
      AND NOT (NEW.resource_type = 'embedding'
               AND e.config_id = NEW.resource_id AND e.subject_id = NEW.subject_id)
)
BEGIN
    SELECT RAISE(ABORT, 'secret file reference is already bound to another resource');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_identity_immutable;
CREATE TRIGGER validate_secret_file_intent_identity_immutable
BEFORE UPDATE OF intent_id, subject_id, resource_type, resource_id,
                 secret_reference, operation, created_at ON secret_file_intents
WHEN NEW.intent_id IS NOT OLD.intent_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.resource_type IS NOT OLD.resource_type
  OR NEW.resource_id IS NOT OLD.resource_id
  OR NEW.secret_reference IS NOT OLD.secret_reference
  OR NEW.operation IS NOT OLD.operation
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'secret file intent identity is immutable');
END;
DROP TRIGGER IF EXISTS prevent_secret_file_intent_delete;
CREATE TRIGGER prevent_secret_file_intent_delete
BEFORE DELETE ON secret_file_intents
BEGIN
    SELECT RAISE(ABORT, 'secret file intents are append-only');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_transition;
CREATE TRIGGER IF NOT EXISTS validate_secret_file_intent_transition
BEFORE UPDATE OF state ON secret_file_intents
WHEN NOT (
    NEW.state = OLD.state
    OR (OLD.state = 'prepared' AND NEW.state IN ('file_ready', 'committed', 'failed', 'removed'))
    OR (OLD.state = 'file_ready' AND NEW.state IN ('committed', 'failed', 'removed'))
    OR (OLD.state = 'committed' AND NEW.state IN ('failed', 'removed'))
    OR (OLD.state = 'pending' AND NEW.state IN ('failed', 'removed'))
    OR (OLD.state = 'failed' AND NEW.state IN ('pending', 'prepared', 'removed'))
)
BEGIN
    SELECT RAISE(ABORT, 'secret file intent state transition is invalid');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_operation_state;
CREATE TRIGGER IF NOT EXISTS validate_secret_file_intent_operation_state
BEFORE UPDATE OF state ON secret_file_intents
WHEN (NEW.operation = 'create' AND NEW.state = 'pending')
  OR (NEW.operation = 'delete' AND NEW.state IN ('prepared', 'file_ready', 'committed'))
BEGIN
    SELECT RAISE(ABORT, 'secret file intent operation state is invalid');
END;

DROP TRIGGER IF EXISTS validate_archive_keyring_generation_insert;
CREATE TRIGGER validate_archive_keyring_generation_insert
BEFORE INSERT ON archive_keyring_revisions
WHEN (
    EXISTS (
        SELECT 1 FROM archive_keyring_revisions WHERE subject_id = NEW.subject_id
    ) AND NEW.generation <= (
        SELECT MAX(generation) FROM archive_keyring_revisions WHERE subject_id = NEW.subject_id
    )
) OR (
    NOT EXISTS (
        SELECT 1 FROM archive_keyring_revisions WHERE generation = NEW.generation
    )
    AND EXISTS (SELECT 1 FROM archive_keyring_revisions)
    AND NEW.generation != (
        SELECT MAX(generation) + 1 FROM archive_keyring_revisions
    )
) OR (
    EXISTS (
        SELECT 1 FROM archive_keyring_revisions WHERE generation = NEW.generation
    )
    AND EXISTS (SELECT 1 FROM archive_keyring_revisions)
    AND NEW.generation != (
        SELECT MAX(generation) FROM archive_keyring_revisions
    )
)
BEGIN
    SELECT RAISE(ABORT, 'archive keyring generation must advance');
END;
""",
}
