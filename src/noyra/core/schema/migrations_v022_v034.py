"""Migrations v022 v034 definitions; execution remains in core.database."""

from __future__ import annotations

MIGRATIONS_V022_V034: dict[int, str] = {
    22: """
CREATE TABLE IF NOT EXISTS self_modification_proposals (
    proposal_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    setting_key TEXT NOT NULL,
    old_value_json TEXT NOT NULL,
    proposed_value_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    risk_score REAL NOT NULL CHECK (risk_score >= 0 AND risk_score <= 1),
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'validated', 'simulated', 'applied', 'accepted',
                   'rejected', 'rolled_back')
    ),
    validation_json TEXT NOT NULL,
    simulation_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    applied_at TEXT,
    observation_deadline TEXT
);
CREATE INDEX IF NOT EXISTS idx_self_modification_subject_status
    ON self_modification_proposals(subject_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS self_modification_settings (
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    setting_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES self_modification_proposals(proposal_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(subject_id, setting_key)
);

CREATE TABLE IF NOT EXISTS self_modification_revisions (
    revision_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    proposal_id TEXT NOT NULL REFERENCES self_modification_proposals(proposal_id),
    setting_key TEXT NOT NULL,
    old_value_json TEXT NOT NULL,
    new_value_json TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('apply', 'accept', 'rollback')),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_self_modification_revisions_subject
    ON self_modification_revisions(subject_id, created_at, revision_id);
CREATE TRIGGER IF NOT EXISTS prevent_self_modification_revision_update
BEFORE UPDATE ON self_modification_revisions BEGIN
    SELECT RAISE(ABORT, 'self modification revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_self_modification_revision_delete
BEFORE DELETE ON self_modification_revisions BEGIN
    SELECT RAISE(ABORT, 'self modification revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_self_modification_proposal_delete
BEFORE DELETE ON self_modification_proposals BEGIN
    SELECT RAISE(ABORT, 'self modification proposals cannot be deleted');
END;
""",
    23: """
CREATE TABLE IF NOT EXISTS archive_transfer_queue (
    transfer_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    storage_class TEXT NOT NULL CHECK (storage_class IN ('cold', 'cloud')),
    object_key TEXT NOT NULL,
    payload_path TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'uploading', 'uploaded', 'failed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, object_key)
);
CREATE INDEX IF NOT EXISTS idx_archive_transfer_due
    ON archive_transfer_queue(subject_id, status, next_attempt_at);
CREATE TRIGGER IF NOT EXISTS prevent_archive_transfer_delete
BEFORE DELETE ON archive_transfer_queue BEGIN
    SELECT RAISE(ABORT, 'archive transfer records cannot be deleted');
END;
""",
    24: """
CREATE TABLE IF NOT EXISTS snapshot_archives (
    archive_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    first_version INTEGER NOT NULL CHECK (first_version > 0),
    last_version INTEGER NOT NULL CHECK (last_version >= first_version),
    snapshot_count INTEGER NOT NULL CHECK (snapshot_count > 0),
    compressed_payload BLOB NOT NULL,
    payload_hash TEXT NOT NULL,
    compressed_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_archives_subject_version
    ON snapshot_archives(subject_id, last_version DESC);
CREATE TRIGGER IF NOT EXISTS prevent_snapshot_archive_update
BEFORE UPDATE ON snapshot_archives BEGIN
    SELECT RAISE(ABORT, 'snapshot archives are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_snapshot_archive_delete
BEFORE DELETE ON snapshot_archives BEGIN
    SELECT RAISE(ABORT, 'snapshot archives are append-only');
END;
""",
    25: """
CREATE TABLE IF NOT EXISTS memory_integrations (
    integration_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    operation TEXT NOT NULL CHECK (operation IN ('merge', 'supersede', 'contradict')),
    source_memory_ids_json TEXT NOT NULL,
    output_memory_id TEXT REFERENCES memories(memory_id),
    evidence_event_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'reverted')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reverted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_integrations_subject_time
    ON memory_integrations(subject_id, created_at DESC);
CREATE TABLE IF NOT EXISTS memory_integration_revisions (
    revision_id TEXT PRIMARY KEY,
    integration_id TEXT NOT NULL REFERENCES memory_integrations(integration_id),
    action TEXT NOT NULL CHECK (action IN ('commit', 'revert')),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS prevent_memory_integration_delete
BEFORE DELETE ON memory_integrations BEGIN
    SELECT RAISE(ABORT, 'memory integrations cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_integration_revision_update
BEFORE UPDATE ON memory_integration_revisions BEGIN
    SELECT RAISE(ABORT, 'memory integration revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_integration_revision_delete
BEFORE DELETE ON memory_integration_revisions BEGIN
    SELECT RAISE(ABORT, 'memory integration revisions are append-only');
END;
""",
    26: """
CREATE TABLE IF NOT EXISTS entity_evidence_links (
    link_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    source_type TEXT NOT NULL CHECK (
        source_type IN ('memory', 'belief', 'event', 'relationship', 'memory_integration')
    ),
    source_id TEXT NOT NULL,
    role TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, entity_id, source_type, source_id, role)
);
CREATE INDEX IF NOT EXISTS idx_entity_evidence_links_source
    ON entity_evidence_links(subject_id, source_type, source_id);
CREATE TRIGGER IF NOT EXISTS prevent_entity_evidence_link_update
BEFORE UPDATE ON entity_evidence_links BEGIN
    SELECT RAISE(ABORT, 'entity evidence links are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_entity_evidence_link_delete
BEFORE DELETE ON entity_evidence_links BEGIN
    SELECT RAISE(ABORT, 'entity evidence links are append-only');
END;
""",
    27: """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    provider TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    vector_json TEXT NOT NULL,
    vector_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_subject_provider
    ON memory_embeddings(subject_id, provider);
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    entity_type TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    UNIQUE(subject_id, entity_type, canonical_key)
);
CREATE INDEX IF NOT EXISTS idx_entities_subject_type
    ON entities(subject_id, entity_type, canonical_key);
CREATE TABLE IF NOT EXISTS entity_relations (
    relation_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    source_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    relation_type TEXT NOT NULL,
    target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    valid_from TEXT,
    valid_until TEXT,
    observed_at TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'contradicted')),
    source_event_ids_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    UNIQUE(subject_id, source_entity_id, relation_type, target_entity_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_entity_relations_subject_time
    ON entity_relations(subject_id, relation_type, observed_at DESC);
CREATE TRIGGER IF NOT EXISTS prevent_memory_embedding_delete
BEFORE DELETE ON memory_embeddings BEGIN
    SELECT RAISE(ABORT, 'memory embeddings are rebuildable but deletion must be explicit');
END;
CREATE TRIGGER IF NOT EXISTS prevent_entity_relation_update
BEFORE UPDATE ON entity_relations BEGIN
    SELECT RAISE(ABORT, 'entity relations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_entity_relation_delete
BEFORE DELETE ON entity_relations BEGIN
    SELECT RAISE(ABORT, 'entity relations are append-only');
END;
""",
    28: """
CREATE TABLE IF NOT EXISTS interaction_transports (
    transport_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    channel TEXT NOT NULL,
    label TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    secret_reference TEXT,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'revoked')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, channel, label)
);
CREATE INDEX IF NOT EXISTS idx_interaction_transports_subject
    ON interaction_transports(subject_id, channel, status);

CREATE TABLE IF NOT EXISTS interaction_deliveries (
    delivery_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL REFERENCES interactions(interaction_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    transport_id TEXT NOT NULL REFERENCES interaction_transports(transport_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'sending', 'delivered', 'failed', 'unknown', 'cancelled')
    ),
    provider_message_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_interaction_deliveries_subject_status
    ON interaction_deliveries(subject_id, status, updated_at);

CREATE TABLE IF NOT EXISTS common_knowledge_packages (
    package_id TEXT PRIMARY KEY,
    publisher_subject_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('skill', 'pitfall', 'protocol', 'reference')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    key_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    status TEXT NOT NULL CHECK (status IN ('published', 'revoked', 'superseded')),
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_scope_status
    ON common_knowledge_packages(scope, status, created_at);

CREATE TABLE IF NOT EXISTS common_knowledge_imports (
    import_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL REFERENCES common_knowledge_packages(package_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    status TEXT NOT NULL CHECK (status IN ('quarantined', 'accepted', 'rejected', 'revoked')),
    reason TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(package_id, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_common_knowledge_imports_subject
    ON common_knowledge_imports(subject_id, status, imported_at);

CREATE TABLE IF NOT EXISTS secret_cleanup_queue (
    task_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    secret_reference TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'failed', 'removed')),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    last_error TEXT,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(resource_type, resource_id, secret_reference)
);
CREATE INDEX IF NOT EXISTS idx_secret_cleanup_subject_status
    ON secret_cleanup_queue(subject_id, status, updated_at);

CREATE TABLE IF NOT EXISTS resource_pool_pressures (
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    pool TEXT NOT NULL CHECK (pool IN ('economy', 'deep', 'search', 'browser', 'embedding')),
    pressure REAL NOT NULL CHECK (pressure >= 0 AND pressure <= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(subject_id, pool)
);

CREATE TABLE IF NOT EXISTS autonomy_loop_state (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    circuit_status TEXT NOT NULL DEFAULT 'closed' CHECK (
        circuit_status IN ('closed', 'open', 'half_open')
    ),
    next_retry_at TEXT,
    last_tick_started_at TEXT,
    last_successful_tick_at TEXT,
    last_error_type TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    workflow_id TEXT NOT NULL,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    workflow_type TEXT NOT NULL,
    checkpoint_version INTEGER NOT NULL CHECK (checkpoint_version > 0),
    status TEXT NOT NULL CHECK (
        status IN ('running', 'interrupted', 'waiting', 'completed', 'failed')
    ),
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(workflow_id, checkpoint_version)
);
CREATE INDEX IF NOT EXISTS idx_workflow_checkpoints_subject_status
    ON workflow_checkpoints(subject_id, status, created_at);

CREATE TABLE IF NOT EXISTS browser_search_reservations (
    reservation_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    idempotency_key TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_browser_search_reservations_subject_time
    ON browser_search_reservations(subject_id, reserved_at);

CREATE TABLE IF NOT EXISTS event_chain_roots (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    previous_root_hash TEXT,
    root_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS idx_event_chain_subject_sequence
    ON event_chain_roots(subject_id, sequence_number);
CREATE TRIGGER IF NOT EXISTS prevent_event_immutable_update
BEFORE UPDATE ON events
WHEN NEW.event_id != OLD.event_id
  OR NEW.subject_id != OLD.subject_id
  OR NEW.event_type != OLD.event_type
  OR NEW.source != OLD.source
  OR NEW.occurred_at != OLD.occurred_at
  OR NEW.observed_at != OLD.observed_at
  OR NEW.payload_json != OLD.payload_json
  OR NEW.payload_hash != OLD.payload_hash
  OR NEW.privacy_level != OLD.privacy_level
  OR NEW.causal_parent_ids_json != OLD.causal_parent_ids_json
BEGIN
    SELECT RAISE(ABORT, 'event evidence is append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_event_delete
BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_event_chain_update
BEFORE UPDATE ON event_chain_roots BEGIN
    SELECT RAISE(ABORT, 'event chain roots are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_event_chain_delete
BEFORE DELETE ON event_chain_roots BEGIN
    SELECT RAISE(ABORT, 'event chain roots are append-only');
END;

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    subject_id UNINDEXED,
    content,
    tokenize = 'unicode61'
);
CREATE TRIGGER IF NOT EXISTS memory_fts_insert
AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(memory_id, subject_id, content)
    VALUES (NEW.memory_id, NEW.subject_id, NEW.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_update
AFTER UPDATE OF content, status ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = OLD.memory_id;
    INSERT INTO memory_fts(memory_id, subject_id, content)
    SELECT NEW.memory_id, NEW.subject_id, NEW.content WHERE NEW.status = 'active';
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_delete
AFTER DELETE ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = OLD.memory_id;
END;

CREATE TABLE IF NOT EXISTS common_knowledge_trusted_keys (
    key_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS memory_blocks (
    block_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    block_type TEXT NOT NULL CHECK (
        block_type IN ('working_context', 'self_summary', 'knowledge_focus', 'relationship_context')
    ),
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    privacy_level TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, block_type, label)
);
CREATE TABLE IF NOT EXISTS memory_block_revisions (
    revision_id TEXT PRIMARY KEY,
    block_id TEXT NOT NULL REFERENCES memory_blocks(block_id),
    version INTEGER NOT NULL CHECK (version > 0),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(block_id, version)
);
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_update
BEFORE UPDATE ON memory_block_revisions BEGIN
    SELECT RAISE(ABORT, 'memory block revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_block_revision_delete
BEFORE DELETE ON memory_block_revisions BEGIN
    SELECT RAISE(ABORT, 'memory block revisions are append-only');
END;

CREATE TABLE IF NOT EXISTS event_payload_segments (
    segment_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    object_key TEXT NOT NULL,
    first_occurred_at TEXT NOT NULL,
    last_occurred_at TEXT NOT NULL,
    event_count INTEGER NOT NULL CHECK (event_count > 0),
    compressed_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, object_key)
);

CREATE TABLE IF NOT EXISTS export_jobs (
    job_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    export_kind TEXT NOT NULL CHECK (export_kind IN ('runtime', 'training')),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
    ),
    artifact_path TEXT,
    filename TEXT,
    sha256 TEXT,
    byte_size INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_export_jobs_subject_time
    ON export_jobs(subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS embedding_resources (
    config_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    label TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER,
    timeout_seconds REAL NOT NULL,
    key_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'revoked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_embedding_resources_subject_status
    ON embedding_resources(subject_id, status, updated_at);
""",
    29: """
CREATE TABLE IF NOT EXISTS action_revisions (
    revision_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'executing', 'succeeded', 'failed', 'unknown', 'cancelled')
    ),
    state_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(action_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_action_revisions_subject_time
    ON action_revisions(subject_id, created_at, action_id);
CREATE TRIGGER IF NOT EXISTS validate_action_revision_insert
BEFORE INSERT ON action_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM actions a
    WHERE a.action_id = NEW.action_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.status
      AND NEW.revision_number = COALESCE(
          (SELECT MAX(r.revision_number) + 1 FROM action_revisions r
           WHERE r.action_id = NEW.action_id), 1
      )
)
BEGIN
    SELECT RAISE(ABORT, 'action revision does not match the current action state');
END;
CREATE TRIGGER IF NOT EXISTS prevent_action_revision_update
BEFORE UPDATE ON action_revisions BEGIN
    SELECT RAISE(ABORT, 'action revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_action_revision_delete
BEFORE DELETE ON action_revisions BEGIN
    SELECT RAISE(ABORT, 'action revisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS validate_action_goal_subject_insert
BEFORE INSERT ON actions
WHEN NEW.goal_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM goals g WHERE g.goal_id = NEW.goal_id AND g.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'action goal belongs to another subject');
END;
CREATE TRIGGER IF NOT EXISTS validate_action_goal_subject_update
BEFORE UPDATE OF subject_id, goal_id ON actions
WHEN NEW.goal_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM goals g WHERE g.goal_id = NEW.goal_id AND g.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'action goal belongs to another subject');
END;
CREATE TRIGGER IF NOT EXISTS validate_action_project_subject_insert
BEFORE INSERT ON actions
WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM autonomous_projects p
    WHERE p.project_id = NEW.project_id AND p.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'action project belongs to another subject');
END;
CREATE TRIGGER IF NOT EXISTS validate_action_project_subject_update
BEFORE UPDATE OF subject_id, project_id ON actions
WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM autonomous_projects p
    WHERE p.project_id = NEW.project_id AND p.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'action project belongs to another subject');
END;
CREATE TRIGGER IF NOT EXISTS validate_action_phase_subject_insert
BEFORE INSERT ON actions
WHEN NEW.phase_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM autonomous_project_phases ph
    JOIN autonomous_projects p ON p.project_id = ph.project_id
    WHERE ph.phase_id = NEW.phase_id
      AND p.subject_id = NEW.subject_id
      AND (NEW.project_id IS NULL OR ph.project_id = NEW.project_id)
)
BEGIN
    SELECT RAISE(ABORT, 'action phase belongs to another subject or project');
END;
CREATE TRIGGER IF NOT EXISTS validate_action_phase_subject_update
BEFORE UPDATE OF subject_id, project_id, phase_id ON actions
WHEN NEW.phase_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM autonomous_project_phases ph
    JOIN autonomous_projects p ON p.project_id = ph.project_id
    WHERE ph.phase_id = NEW.phase_id
      AND p.subject_id = NEW.subject_id
      AND (NEW.project_id IS NULL OR ph.project_id = NEW.project_id)
)
BEGIN
    SELECT RAISE(ABORT, 'action phase belongs to another subject or project');
END;

CREATE TRIGGER IF NOT EXISTS validate_behavior_log_subject_insert
BEFORE INSERT ON behavior_logs
WHEN NOT EXISTS (
    SELECT 1 FROM actions a
    WHERE a.action_id = NEW.action_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.result_status
)
BEGIN
    SELECT RAISE(ABORT, 'behavior log does not match its action');
END;
CREATE TRIGGER IF NOT EXISTS validate_behavior_log_subject_update
BEFORE UPDATE OF action_id, subject_id, result_status ON behavior_logs
WHEN NOT EXISTS (
    SELECT 1 FROM actions a
    WHERE a.action_id = NEW.action_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.result_status
)
BEGIN
    SELECT RAISE(ABORT, 'behavior log does not match its action');
END;
""",
    30: """
-- Additive columns are installed idempotently by _ensure_optional_features.
""",
    31: """
-- Additive columns are installed idempotently by _ensure_optional_features.
""",
    32: """
CREATE TABLE IF NOT EXISTS secret_cleanup_queue (
    task_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    secret_reference TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'failed', 'removed')),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    last_error TEXT,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(resource_type, resource_id, secret_reference)
);
CREATE INDEX IF NOT EXISTS idx_secret_cleanup_subject_status
    ON secret_cleanup_queue(subject_id, status, updated_at);
""",
    33: """
CREATE TABLE IF NOT EXISTS behavior_log_revisions (
    revision_id TEXT PRIMARY KEY,
    log_id TEXT NOT NULL REFERENCES behavior_logs(log_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
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
    redaction_reason TEXT,
    state_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(log_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_behavior_log_revisions_subject_time
    ON behavior_log_revisions(subject_id, occurred_at, log_id, revision_number);
CREATE INDEX IF NOT EXISTS idx_behavior_log_revisions_action
    ON behavior_log_revisions(action_id, revision_number);
CREATE TRIGGER IF NOT EXISTS validate_behavior_log_revision_insert
BEFORE INSERT ON behavior_log_revisions
WHEN NOT EXISTS (
    SELECT 1
    FROM behavior_logs l
    JOIN actions a ON a.action_id = l.action_id
    WHERE l.log_id = NEW.log_id
      AND l.action_id = NEW.action_id
      AND l.subject_id = NEW.subject_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.result_status
      AND NEW.revision_number = COALESCE(
          (SELECT MAX(r.revision_number) + 1
           FROM behavior_log_revisions r WHERE r.log_id = NEW.log_id), 1
      )
      AND (
          NEW.revision_number = 1
          OR (
              NEW.revision_number = 2
              AND NEW.result_status <> 'unknown'
              AND EXISTS (
                  SELECT 1
                  FROM behavior_log_revisions previous
                  WHERE previous.log_id = NEW.log_id
                    AND previous.revision_number = 1
                    AND previous.result_status = 'unknown'
                    AND previous.action_type IS NEW.action_type
                    AND previous.public_goal_reference IS NEW.public_goal_reference
                    AND previous.tool IS NEW.tool
                    AND previous.public_target IS NEW.public_target
                    AND previous.resource_summary IS NEW.resource_summary
                    AND previous.redaction_reason IS NEW.redaction_reason
              )
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'behavior log revision does not match reconciliation contract');
END;
CREATE TRIGGER IF NOT EXISTS prevent_behavior_log_revision_update
BEFORE UPDATE ON behavior_log_revisions BEGIN
    SELECT RAISE(ABORT, 'behavior log revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_behavior_log_revision_delete
BEFORE DELETE ON behavior_log_revisions BEGIN
    SELECT RAISE(ABORT, 'behavior log revisions cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_behavior_log_update
BEFORE UPDATE ON behavior_logs BEGIN
    SELECT RAISE(ABORT, 'behavior logs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_behavior_log_delete
BEFORE DELETE ON behavior_logs BEGIN
    SELECT RAISE(ABORT, 'behavior logs cannot be deleted');
END;
""",
    34: """
CREATE TABLE IF NOT EXISTS embedding_usage_entries (
    usage_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('authorized', 'executing', 'succeeded', 'failed', 'unknown', 'cancelled')
    ),
    text_count INTEGER NOT NULL CHECK (text_count BETWEEN 1 AND 128),
    reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens >= 0),
    input_tokens INTEGER CHECK (input_tokens >= 0),
    reserved_cost_microusd INTEGER NOT NULL CHECK (reserved_cost_microusd >= 0),
    cost_microusd INTEGER CHECK (cost_microusd >= 0),
    usage_estimated INTEGER NOT NULL DEFAULT 1 CHECK (usage_estimated IN (0, 1)),
    provider_request_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_embedding_usage_subject_day
    ON embedding_usage_entries(subject_id, resource_id, budget_day, status);
CREATE INDEX IF NOT EXISTS idx_embedding_usage_subject_resource
    ON embedding_usage_entries(subject_id, resource_id, created_at);
CREATE TRIGGER IF NOT EXISTS prevent_embedding_usage_delete
BEFORE DELETE ON embedding_usage_entries BEGIN
    SELECT RAISE(ABORT, 'embedding usage entries cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS embedding_circuit_states (
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('closed', 'open', 'half_open')),
    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
    next_probe_at TEXT,
    probe_usage_id TEXT REFERENCES embedding_usage_entries(usage_id),
    last_error_code TEXT,
    state_hash TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(subject_id, resource_id)
);
CREATE TABLE IF NOT EXISTS embedding_circuit_transitions (
    transition_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource_id TEXT NOT NULL,
    old_status TEXT NOT NULL CHECK (old_status IN ('closed', 'open', 'half_open')),
    new_status TEXT NOT NULL CHECK (new_status IN ('closed', 'open', 'half_open')),
    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
    next_probe_at TEXT,
    probe_usage_id TEXT REFERENCES embedding_usage_entries(usage_id),
    error_code TEXT,
    state_hash TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, resource_id, version)
);
CREATE INDEX IF NOT EXISTS idx_embedding_circuit_transitions_subject
    ON embedding_circuit_transitions(subject_id, resource_id, version);
CREATE TRIGGER IF NOT EXISTS prevent_embedding_circuit_transition_update
BEFORE UPDATE ON embedding_circuit_transitions BEGIN
    SELECT RAISE(ABORT, 'embedding circuit transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_embedding_circuit_transition_delete
BEFORE DELETE ON embedding_circuit_transitions BEGIN
    SELECT RAISE(ABORT, 'embedding circuit transitions are append-only');
END;
""",
}
