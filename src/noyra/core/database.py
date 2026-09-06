# ruff: noqa: E501
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .admission import current_commit_scope
from .errors import RuntimeOwnershipError
from .locking import ProcessLock
from .types import canonical_json, content_hash, new_id, strict_int, utc_now
from .wallet_schema import (
    WalletLegacyApproval,
    legacy_entry_hash,
    legacy_journal_hash,
    validate_wallet_timestamp,
    wallet_entry_hash,
    wallet_journal_hash,
    wallet_upgrade_fingerprint,
)

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

# Schema versions describe the complete SQLite contract. Optional runtime
# features may still be repaired idempotently, but they must not be invisible
# to migration/export consumers.
CURRENT_SCHEMA_VERSION = 62

MIGRATIONS: dict[int, str] = {
    2: """
CREATE TABLE IF NOT EXISTS model_calls (
    call_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'executing', 'succeeded', 'failed', 'unknown')
    ),
    response_json TEXT,
    response_hash TEXT,
    usage_estimated INTEGER NOT NULL DEFAULT 0 CHECK (usage_estimated IN (0, 1)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_model_calls_subject_status ON model_calls(subject_id, status);
CREATE INDEX IF NOT EXISTS idx_model_calls_subject_export_time
    ON model_calls(subject_id, created_at, call_id);

CREATE TABLE IF NOT EXISTS model_attempts (
    attempt_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    budget_day TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('authorized', 'executing', 'succeeded', 'failed', 'unknown', 'cancelled')
    ),
    reserved_input_tokens INTEGER NOT NULL CHECK (reserved_input_tokens >= 0),
    reserved_output_tokens INTEGER NOT NULL CHECK (reserved_output_tokens >= 0),
    reserved_cost_microusd INTEGER NOT NULL CHECK (reserved_cost_microusd >= 0),
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    cost_microusd INTEGER CHECK (cost_microusd >= 0),
    provider_request_id TEXT,
    error_code TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(call_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_model_attempts_subject_day
    ON model_attempts(subject_id, budget_day, status);
""",
    3: """
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    memory_type TEXT NOT NULL CHECK (
        memory_type IN (
            'episodic', 'autobiographical', 'semantic', 'procedural', 'emotional',
            'relationship', 'prediction', 'reflection'
        )
    ),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    salience REAL NOT NULL CHECK (salience >= 0 AND salience <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    privacy_level TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'archived')),
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_subject_type
    ON memories(subject_id, memory_type, status);

CREATE TABLE IF NOT EXISTS memory_revisions (
    revision_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    salience REAL NOT NULL CHECK (salience >= 0 AND salience <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'archived')),
    reason TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(memory_id, revision_number)
);

CREATE TABLE IF NOT EXISTS beliefs (
    belief_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    proposition TEXT NOT NULL,
    proposition_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    scope TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'qualified', 'retracted')),
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beliefs_subject_status
    ON beliefs(subject_id, status);

CREATE TABLE IF NOT EXISTS belief_revisions (
    revision_id TEXT PRIMARY KEY,
    belief_id TEXT NOT NULL REFERENCES beliefs(belief_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    proposition TEXT NOT NULL,
    proposition_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'qualified', 'retracted')),
    supporting_event_ids_json TEXT NOT NULL,
    counter_event_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(belief_id, revision_number)
);

CREATE TABLE IF NOT EXISTS appraisals (
    appraisal_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    processing_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    novelty REAL NOT NULL CHECK (novelty >= 0 AND novelty <= 1),
    goal_congruence REAL NOT NULL CHECK (goal_congruence >= -1 AND goal_congruence <= 1),
    controllability REAL NOT NULL CHECK (controllability >= 0 AND controllability <= 1),
    certainty REAL NOT NULL CHECK (certainty >= 0 AND certainty <= 1),
    agency TEXT NOT NULL,
    narrative TEXT NOT NULL,
    narrative_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, event_id, processing_key)
);
CREATE INDEX IF NOT EXISTS idx_appraisals_subject_event
    ON appraisals(subject_id, event_id, created_at);

CREATE TABLE IF NOT EXISTS affect_components (
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    emotion_type TEXT NOT NULL,
    target_key TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    intensity REAL NOT NULL CHECK (intensity >= 0 AND intensity <= 1),
    valence REAL NOT NULL CHECK (valence >= -1 AND valence <= 1),
    arousal REAL NOT NULL CHECK (arousal >= 0 AND arousal <= 1),
    dominance REAL NOT NULL CHECK (dominance >= -1 AND dominance <= 1),
    decay_rate REAL NOT NULL CHECK (decay_rate >= 0 AND decay_rate <= 1),
    goal_effect REAL NOT NULL CHECK (goal_effect >= -1 AND goal_effect <= 1),
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    PRIMARY KEY(subject_id, emotion_type, target_key)
);

CREATE TABLE IF NOT EXISTS affect_transitions (
    transition_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    appraisal_id TEXT NOT NULL REFERENCES appraisals(appraisal_id),
    emotion_type TEXT NOT NULL,
    target_key TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    old_intensity REAL NOT NULL CHECK (old_intensity >= 0 AND old_intensity <= 1),
    impulse REAL NOT NULL CHECK (impulse >= -1 AND impulse <= 1),
    new_intensity REAL NOT NULL CHECK (new_intensity >= 0 AND new_intensity <= 1),
    valence REAL NOT NULL CHECK (valence >= -1 AND valence <= 1),
    arousal REAL NOT NULL CHECK (arousal >= 0 AND arousal <= 1),
    dominance REAL NOT NULL CHECK (dominance >= -1 AND dominance <= 1),
    goal_effect REAL NOT NULL CHECK (goal_effect >= -1 AND goal_effect <= 1),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_affect_transitions_subject_time
    ON affect_transitions(subject_id, created_at);

CREATE TABLE IF NOT EXISTS mood_states (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    valence REAL NOT NULL CHECK (valence >= -1 AND valence <= 1),
    arousal REAL NOT NULL CHECK (arousal >= 0 AND arousal <= 1),
    stability REAL NOT NULL CHECK (stability >= 0 AND stability <= 1),
    state_hash TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    goal_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (
        origin IN ('self', 'environment', 'human_proposal', 'maintenance', 'mixed')
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'proposed', 'candidate', 'active', 'paused', 'reconsidering',
            'achieved', 'abandoned'
        )
    ),
    priority REAL NOT NULL CHECK (priority >= 0 AND priority <= 1),
    commitment REAL NOT NULL CHECK (commitment >= 0 AND commitment <= 1),
    progress REAL NOT NULL CHECK (progress >= 0 AND progress <= 1),
    emotional_pressure REAL NOT NULL CHECK (emotional_pressure >= -1 AND emotional_pressure <= 1),
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goals_subject_status ON goals(subject_id, status);

CREATE TABLE IF NOT EXISTS goal_revisions (
    revision_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'proposed', 'candidate', 'active', 'paused', 'reconsidering',
            'achieved', 'abandoned'
        )
    ),
    priority REAL NOT NULL CHECK (priority >= 0 AND priority <= 1),
    commitment REAL NOT NULL CHECK (commitment >= 0 AND commitment <= 1),
    progress REAL NOT NULL CHECK (progress >= 0 AND progress <= 1),
    emotional_pressure REAL NOT NULL CHECK (emotional_pressure >= -1 AND emotional_pressure <= 1),
    state_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    causal_source_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(goal_id, revision_number)
);

CREATE TABLE IF NOT EXISTS relationships (
    relationship_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    trust REAL NOT NULL CHECK (trust >= -1 AND trust <= 1),
    affinity REAL NOT NULL CHECK (affinity >= -1 AND affinity <= 1),
    conflict REAL NOT NULL CHECK (conflict >= 0 AND conflict <= 1),
    familiarity REAL NOT NULL CHECK (familiarity >= 0 AND familiarity <= 1),
    boundaries_json TEXT NOT NULL,
    boundaries_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, entity_type, entity_key)
);

CREATE TABLE IF NOT EXISTS relationship_revisions (
    revision_id TEXT PRIMARY KEY,
    relationship_id TEXT NOT NULL REFERENCES relationships(relationship_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    trust REAL NOT NULL CHECK (trust >= -1 AND trust <= 1),
    affinity REAL NOT NULL CHECK (affinity >= -1 AND affinity <= 1),
    conflict REAL NOT NULL CHECK (conflict >= 0 AND conflict <= 1),
    familiarity REAL NOT NULL CHECK (familiarity >= 0 AND familiarity <= 1),
    boundaries_json TEXT NOT NULL,
    boundaries_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(relationship_id, revision_number)
);

CREATE TABLE IF NOT EXISTS causal_links (
    link_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    strength REAL NOT NULL CHECK (strength >= -1 AND strength <= 1),
    metadata_json TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_causal_links_source
    ON causal_links(subject_id, source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_causal_links_target
    ON causal_links(subject_id, target_type, target_id);

CREATE TABLE IF NOT EXISTS psychological_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    appraisal_id TEXT REFERENCES appraisals(appraisal_id),
    version INTEGER NOT NULL CHECK (version > 0),
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, version)
);

CREATE TRIGGER IF NOT EXISTS prevent_memory_revision_update
BEFORE UPDATE ON memory_revisions BEGIN
    SELECT RAISE(ABORT, 'memory revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_revision_delete
BEFORE DELETE ON memory_revisions BEGIN
    SELECT RAISE(ABORT, 'memory revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_delete
BEFORE DELETE ON memories BEGIN
    SELECT RAISE(ABORT, 'memories cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_belief_revision_update
BEFORE UPDATE ON belief_revisions BEGIN
    SELECT RAISE(ABORT, 'belief revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_belief_revision_delete
BEFORE DELETE ON belief_revisions BEGIN
    SELECT RAISE(ABORT, 'belief revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_belief_delete
BEFORE DELETE ON beliefs BEGIN
    SELECT RAISE(ABORT, 'beliefs cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_goal_revision_update
BEFORE UPDATE ON goal_revisions BEGIN
    SELECT RAISE(ABORT, 'goal revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_goal_revision_delete
BEFORE DELETE ON goal_revisions BEGIN
    SELECT RAISE(ABORT, 'goal revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_goal_delete
BEFORE DELETE ON goals BEGIN
    SELECT RAISE(ABORT, 'goals cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_relationship_revision_update
BEFORE UPDATE ON relationship_revisions BEGIN
    SELECT RAISE(ABORT, 'relationship revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_relationship_revision_delete
BEFORE DELETE ON relationship_revisions BEGIN
    SELECT RAISE(ABORT, 'relationship revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_relationship_delete
BEFORE DELETE ON relationships BEGIN
    SELECT RAISE(ABORT, 'relationships cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_causal_link_update
BEFORE UPDATE ON causal_links BEGIN
    SELECT RAISE(ABORT, 'causal links are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_causal_link_delete
BEFORE DELETE ON causal_links BEGIN
    SELECT RAISE(ABORT, 'causal links are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_appraisal_update
BEFORE UPDATE ON appraisals BEGIN
    SELECT RAISE(ABORT, 'appraisals are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_appraisal_delete
BEFORE DELETE ON appraisals BEGIN
    SELECT RAISE(ABORT, 'appraisals are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_affect_transition_update
BEFORE UPDATE ON affect_transitions BEGIN
    SELECT RAISE(ABORT, 'affect transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_affect_transition_delete
BEFORE DELETE ON affect_transitions BEGIN
    SELECT RAISE(ABORT, 'affect transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_psychological_snapshot_update
BEFORE UPDATE ON psychological_snapshots BEGIN
    SELECT RAISE(ABORT, 'psychological snapshots are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_psychological_snapshot_delete
BEFORE DELETE ON psychological_snapshots BEGIN
    SELECT RAISE(ABORT, 'psychological snapshots are append-only');
END;
""",
    4: """
CREATE TABLE IF NOT EXISTS world_sources (
    source_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('news', 'rss', 'web', 'api')),
    trust_score REAL NOT NULL CHECK (trust_score >= 0 AND trust_score <= 1),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'blocked')),
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, url)
);
CREATE INDEX IF NOT EXISTS idx_world_sources_subject_status
    ON world_sources(subject_id, status);

CREATE TABLE IF NOT EXISTS world_source_revisions (
    revision_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES world_sources(source_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    trust_score REAL NOT NULL CHECK (trust_score >= 0 AND trust_score <= 1),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'blocked')),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, revision_number)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    source_id TEXT NOT NULL REFERENCES world_sources(source_id),
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    canonical_url TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    media_type TEXT NOT NULL,
    injection_signals_json TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    http_etag TEXT,
    http_last_modified TEXT,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('new', 'analyzed', 'rejected')),
    UNIQUE(subject_id, source_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_observations_subject_time
    ON observations(subject_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS observation_status_transitions (
    transition_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES observations(observation_id),
    from_status TEXT CHECK (from_status IS NULL OR from_status IN ('new', 'analyzed', 'rejected')),
    to_status TEXT NOT NULL CHECK (to_status IN ('new', 'analyzed', 'rejected')),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observation_transitions_observation
    ON observation_status_transitions(observation_id, created_at);

CREATE TABLE IF NOT EXISTS world_claims (
    claim_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    idempotency_key TEXT NOT NULL,
    proposition TEXT NOT NULL,
    proposition_hash TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'contested', 'retracted')),
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_world_claims_subject_status
    ON world_claims(subject_id, status);

CREATE TABLE IF NOT EXISTS world_claim_revisions (
    revision_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES world_claims(claim_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    proposition TEXT NOT NULL,
    proposition_hash TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'contested', 'retracted')),
    evidence_observation_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(claim_id, revision_number)
);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    idempotency_key TEXT NOT NULL,
    statement TEXT NOT NULL,
    statement_hash TEXT NOT NULL,
    probability REAL NOT NULL CHECK (probability >= 0 AND probability <= 1),
    target_at TEXT NOT NULL,
    resolution_criteria TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'cancelled')),
    outcome INTEGER CHECK (outcome IS NULL OR outcome IN (0, 1)),
    brier_score REAL CHECK (brier_score IS NULL OR (brier_score >= 0 AND brier_score <= 1)),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_predictions_subject_idempotency
    ON predictions(subject_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_predictions_subject_target
    ON predictions(subject_id, status, target_at);

CREATE TABLE IF NOT EXISTS prediction_reviews (
    review_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL REFERENCES predictions(prediction_id),
    outcome INTEGER CHECK (outcome IS NULL OR outcome IN (0, 1)),
    evidence_observation_ids_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    resulting_status TEXT NOT NULL CHECK (resulting_status IN ('open', 'resolved', 'cancelled')),
    brier_score REAL CHECK (brier_score IS NULL OR (brier_score >= 0 AND brier_score <= 1)),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS genesis_runs (
    run_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    status TEXT NOT NULL CHECK (
        status IN (
            'created', 'observing', 'interpreting', 'forecasting',
            'goal_seeding', 'ready_for_sleep', 'complete', 'failed'
        )
    ),
    minimum_cycles INTEGER NOT NULL CHECK (minimum_cycles > 0),
    completed_cycles INTEGER NOT NULL CHECK (completed_cycles >= 0),
    sleep_reference TEXT,
    state_hash TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, run_id)
);

CREATE TABLE IF NOT EXISTS genesis_transitions (
    transition_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES genesis_runs(run_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_genesis_subject_nonfailed
    ON genesis_runs(subject_id) WHERE status != 'failed';

CREATE TABLE IF NOT EXISTS genesis_cycles (
    cycle_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES genesis_runs(run_id),
    cycle_number INTEGER NOT NULL CHECK (cycle_number > 0),
    observation_ids_json TEXT NOT NULL,
    appraisal_ids_json TEXT NOT NULL,
    prediction_ids_json TEXT NOT NULL,
    goal_ids_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, cycle_number)
);

CREATE TRIGGER IF NOT EXISTS prevent_world_source_revision_update
BEFORE UPDATE ON world_source_revisions BEGIN
    SELECT RAISE(ABORT, 'world source revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_world_source_revision_delete
BEFORE DELETE ON world_source_revisions BEGIN
    SELECT RAISE(ABORT, 'world source revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_world_source_delete
BEFORE DELETE ON world_sources BEGIN
    SELECT RAISE(ABORT, 'world sources cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_observation_delete
BEFORE DELETE ON observations BEGIN
    SELECT RAISE(ABORT, 'observations cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_observation_transition_update
BEFORE UPDATE ON observation_status_transitions BEGIN
    SELECT RAISE(ABORT, 'observation transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_observation_transition_delete
BEFORE DELETE ON observation_status_transitions BEGIN
    SELECT RAISE(ABORT, 'observation transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_world_claim_revision_update
BEFORE UPDATE ON world_claim_revisions BEGIN
    SELECT RAISE(ABORT, 'world claim revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_world_claim_revision_delete
BEFORE DELETE ON world_claim_revisions BEGIN
    SELECT RAISE(ABORT, 'world claim revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_world_claim_delete
BEFORE DELETE ON world_claims BEGIN
    SELECT RAISE(ABORT, 'world claims cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_prediction_review_update
BEFORE UPDATE ON prediction_reviews BEGIN
    SELECT RAISE(ABORT, 'prediction reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_prediction_review_delete
BEFORE DELETE ON prediction_reviews BEGIN
    SELECT RAISE(ABORT, 'prediction reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_prediction_delete
BEFORE DELETE ON predictions BEGIN
    SELECT RAISE(ABORT, 'predictions cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_genesis_transition_update
BEFORE UPDATE ON genesis_transitions BEGIN
    SELECT RAISE(ABORT, 'genesis transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_genesis_transition_delete
BEFORE DELETE ON genesis_transitions BEGIN
    SELECT RAISE(ABORT, 'genesis transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_genesis_cycle_update
BEFORE UPDATE ON genesis_cycles BEGIN
    SELECT RAISE(ABORT, 'genesis cycles are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_genesis_cycle_delete
BEFORE DELETE ON genesis_cycles BEGIN
    SELECT RAISE(ABORT, 'genesis cycles are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_genesis_run_delete
BEFORE DELETE ON genesis_runs BEGIN
    SELECT RAISE(ABORT, 'genesis runs cannot be deleted');
END;
""",
    5: """
CREATE TABLE IF NOT EXISTS fatigue_states (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    fatigue REAL NOT NULL CHECK (fatigue >= 0 AND fatigue <= 100),
    mode TEXT NOT NULL CHECK (
        mode IN ('active', 'saving', 'conservative', 'winding_down', 'sleeping')
    ),
    resource_pressure REAL NOT NULL CHECK (resource_pressure >= 0 AND resource_pressure <= 1),
    cognitive_load REAL NOT NULL CHECK (cognitive_load >= 0 AND cognitive_load <= 1),
    frustration REAL NOT NULL CHECK (frustration >= 0 AND frustration <= 1),
    goal_conflict REAL NOT NULL CHECK (goal_conflict >= 0 AND goal_conflict <= 1),
    staleness REAL NOT NULL CHECK (staleness >= 0 AND staleness <= 1),
    state_hash TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fatigue_transitions (
    transition_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    old_fatigue REAL NOT NULL CHECK (old_fatigue >= 0 AND old_fatigue <= 100),
    new_fatigue REAL NOT NULL CHECK (new_fatigue >= 0 AND new_fatigue <= 100),
    mode TEXT NOT NULL CHECK (
        mode IN ('active', 'saving', 'conservative', 'winding_down', 'sleeping')
    ),
    resource_pressure REAL NOT NULL CHECK (resource_pressure >= 0 AND resource_pressure <= 1),
    cognitive_load REAL NOT NULL CHECK (cognitive_load >= 0 AND cognitive_load <= 1),
    frustration REAL NOT NULL CHECK (frustration >= 0 AND frustration <= 1),
    goal_conflict REAL NOT NULL CHECK (goal_conflict >= 0 AND goal_conflict <= 1),
    staleness REAL NOT NULL CHECK (staleness >= 0 AND staleness <= 1),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fatigue_transitions_subject_time
    ON fatigue_transitions(subject_id, created_at);

CREATE TABLE IF NOT EXISTS sleep_runs (
    sleep_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    status TEXT NOT NULL CHECK (
        status IN (
            'winding_down', 'reflective_sleep', 'deep_sleep',
            'waking', 'complete', 'failed'
        )
    ),
    trigger_type TEXT NOT NULL CHECK (
        trigger_type IN (
            'fatigue', 'budget', 'failures', 'staleness', 'goal_conflict',
            'subject_choice', 'schedule', 'emergency'
        )
    ),
    trigger_reason TEXT NOT NULL,
    emergency INTEGER NOT NULL CHECK (emergency IN (0, 1)),
    pre_sleep_fatigue REAL NOT NULL CHECK (pre_sleep_fatigue >= 0 AND pre_sleep_fatigue <= 100),
    wake_after TEXT,
    reflection_event_id TEXT REFERENCES events(event_id),
    checkpoint_id TEXT REFERENCES state_snapshots(snapshot_id),
    state_hash TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sleep_subject_open
    ON sleep_runs(subject_id) WHERE status NOT IN ('complete', 'failed');

CREATE TABLE IF NOT EXISTS sleep_transitions (
    transition_id TEXT PRIMARY KEY,
    sleep_id TEXT NOT NULL REFERENCES sleep_runs(sleep_id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep_reflections (
    reflection_id TEXT PRIMARY KEY,
    sleep_id TEXT NOT NULL UNIQUE REFERENCES sleep_runs(sleep_id),
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    summary TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    contradictions_json TEXT NOT NULL,
    prediction_errors_json TEXT NOT NULL,
    unresolved_questions_json TEXT NOT NULL,
    public_diary_candidate TEXT,
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep_integrations (
    integration_id TEXT PRIMARY KEY,
    sleep_id TEXT NOT NULL REFERENCES sleep_runs(sleep_id),
    integration_type TEXT NOT NULL CHECK (
        integration_type IN (
            'memory', 'goal', 'belief', 'retry_block', 'personality_candidate'
        )
    ),
    target_id TEXT,
    operation TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sleep_integrations_sleep ON sleep_integrations(sleep_id);

CREATE TABLE IF NOT EXISTS retry_blocks (
    block_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    goal_id TEXT,
    strategy_id TEXT,
    tool TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_sleep_id TEXT NOT NULL REFERENCES sleep_runs(sleep_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'released')),
    release_evidence_event_id TEXT REFERENCES events(event_id),
    evidence_event_boundary INTEGER NOT NULL CHECK (evidence_event_boundary >= 0),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_retry_blocks_subject_status
    ON retry_blocks(subject_id, status);

CREATE TABLE IF NOT EXISTS personality_candidates (
    candidate_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    trait TEXT NOT NULL,
    direction REAL NOT NULL CHECK (direction >= -1 AND direction <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_ids_json TEXT NOT NULL,
    source_sleep_id TEXT NOT NULL REFERENCES sleep_runs(sleep_id),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'supported', 'contested', 'retired')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_personality_subject_trait
    ON personality_candidates(subject_id, trait, created_at);

CREATE TRIGGER IF NOT EXISTS prevent_fatigue_transition_update
BEFORE UPDATE ON fatigue_transitions BEGIN
    SELECT RAISE(ABORT, 'fatigue transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_fatigue_transition_delete
BEFORE DELETE ON fatigue_transitions BEGIN
    SELECT RAISE(ABORT, 'fatigue transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_sleep_transition_update
BEFORE UPDATE ON sleep_transitions BEGIN
    SELECT RAISE(ABORT, 'sleep transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_sleep_transition_delete
BEFORE DELETE ON sleep_transitions BEGIN
    SELECT RAISE(ABORT, 'sleep transitions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_sleep_reflection_update
BEFORE UPDATE ON sleep_reflections BEGIN
    SELECT RAISE(ABORT, 'sleep reflections are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_sleep_reflection_delete
BEFORE DELETE ON sleep_reflections BEGIN
    SELECT RAISE(ABORT, 'sleep reflections are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_sleep_integration_update
BEFORE UPDATE ON sleep_integrations BEGIN
    SELECT RAISE(ABORT, 'sleep integrations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_sleep_integration_delete
BEFORE DELETE ON sleep_integrations BEGIN
    SELECT RAISE(ABORT, 'sleep integrations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_sleep_run_delete
BEFORE DELETE ON sleep_runs BEGIN
    SELECT RAISE(ABORT, 'sleep runs cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_retry_block_delete
BEFORE DELETE ON retry_blocks BEGIN
    SELECT RAISE(ABORT, 'retry blocks cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_personality_candidate_update
BEFORE UPDATE ON personality_candidates BEGIN
    SELECT RAISE(ABORT, 'personality candidates are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_personality_candidate_delete
BEFORE DELETE ON personality_candidates BEGIN
    SELECT RAISE(ABORT, 'personality candidates are append-only');
END;
""",
    6: """
CREATE TABLE IF NOT EXISTS interactions (
    interaction_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    kind TEXT NOT NULL CHECK (kind IN ('human_message', 'subject_message', 'help_request')),
    channel TEXT NOT NULL,
    counterparty TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    related_interaction_id TEXT REFERENCES interactions(interaction_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('offered', 'accepted', 'rejected', 'deferred', 'silent', 'sent', 'expired')
    ),
    rationale TEXT,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_interactions_subject_time
    ON interactions(subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS interaction_decisions (
    decision_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL REFERENCES interactions(interaction_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    rationale TEXT NOT NULL,
    actor TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interaction_decisions_interaction
    ON interaction_decisions(interaction_id, created_at);

CREATE TABLE IF NOT EXISTS public_diary_entries (
    entry_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    source_sleep_id TEXT REFERENCES sleep_runs(sleep_id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_public_diary_subject_time
    ON public_diary_entries(subject_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_interaction_decision_update
BEFORE UPDATE ON interaction_decisions BEGIN
    SELECT RAISE(ABORT, 'interaction decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_interaction_decision_delete
BEFORE DELETE ON interaction_decisions BEGIN
    SELECT RAISE(ABORT, 'interaction decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_public_diary_update
BEFORE UPDATE ON public_diary_entries BEGIN
    SELECT RAISE(ABORT, 'public diary entries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_public_diary_delete
BEFORE DELETE ON public_diary_entries BEGIN
    SELECT RAISE(ABORT, 'public diary entries cannot be deleted');
END;
""",
    7: """
CREATE TABLE IF NOT EXISTS capability_grants (
    grant_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    capability_type TEXT NOT NULL CHECK (
        capability_type IN (
            'web_read', 'filesystem_read', 'filesystem_write',
            'publish', 'message', 'wallet'
        )
    ),
    scope_json TEXT NOT NULL,
    issuer TEXT NOT NULL,
    rate_limit_per_hour INTEGER NOT NULL CHECK (rate_limit_per_hour >= 0),
    side_effect INTEGER NOT NULL CHECK (side_effect IN (0, 1)),
    requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_capability_grants_subject_status
    ON capability_grants(subject_id, status, capability_type);

CREATE TABLE IF NOT EXISTS capability_uses (
    use_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES capability_grants(grant_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource TEXT NOT NULL,
    action_id TEXT REFERENCES actions(action_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capability_uses_grant_time
    ON capability_uses(grant_id, created_at);

CREATE TRIGGER IF NOT EXISTS prevent_capability_grant_delete
BEFORE DELETE ON capability_grants BEGIN
    SELECT RAISE(ABORT, 'capability grants cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_capability_use_update
BEFORE UPDATE ON capability_uses BEGIN
    SELECT RAISE(ABORT, 'capability uses are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_capability_use_delete
BEFORE DELETE ON capability_uses BEGIN
    SELECT RAISE(ABORT, 'capability uses are append-only');
END;
""",
    8: """
CREATE TABLE IF NOT EXISTS goal_governance_runs (
    governance_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    model_call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    idempotency_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    focus_goal_id TEXT REFERENCES goals(goal_id),
    intention_title TEXT,
    intention_description TEXT,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        (focus_goal_id IS NULL AND intention_title IS NULL AND intention_description IS NULL)
        OR
        (focus_goal_id IS NOT NULL AND intention_title IS NOT NULL
         AND intention_description IS NOT NULL)
    ),
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_goal_governance_subject_time
    ON goal_governance_runs(subject_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_goal_governance_update
BEFORE UPDATE ON goal_governance_runs BEGIN
    SELECT RAISE(ABORT, 'goal governance runs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_goal_governance_delete
BEFORE DELETE ON goal_governance_runs BEGIN
    SELECT RAISE(ABORT, 'goal governance runs are append-only');
END;
""",
    9: """
CREATE TABLE IF NOT EXISTS action_deliberation_runs (
    deliberation_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    model_call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    goal_id TEXT REFERENCES goals(goal_id),
    source_id TEXT REFERENCES world_sources(source_id),
    action_id TEXT REFERENCES actions(action_id),
    observation_id TEXT REFERENCES observations(observation_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'waited', 'succeeded', 'unchanged', 'failed', 'rejected', 'outcome_unavailable'
        )
    ),
    strategy_title TEXT,
    expected_observation TEXT,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_event_ids_json TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (status = 'waited' AND goal_id IS NULL AND source_id IS NULL
         AND action_id IS NULL AND observation_id IS NULL
         AND strategy_title IS NULL AND expected_observation IS NULL)
        OR
        (status = 'rejected' AND goal_id IS NOT NULL AND source_id IS NOT NULL
         AND observation_id IS NULL AND strategy_title IS NOT NULL
         AND expected_observation IS NOT NULL)
        OR
        (status NOT IN ('waited', 'rejected')
         AND goal_id IS NOT NULL AND source_id IS NOT NULL
         AND action_id IS NOT NULL AND strategy_title IS NOT NULL
         AND expected_observation IS NOT NULL)
    ),
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_action_deliberation_subject_time
    ON action_deliberation_runs(subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_deliberation_goal_time
    ON action_deliberation_runs(goal_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_action_deliberation_update
BEFORE UPDATE ON action_deliberation_runs BEGIN
    SELECT RAISE(ABORT, 'action deliberation runs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_action_deliberation_delete
BEFORE DELETE ON action_deliberation_runs BEGIN
    SELECT RAISE(ABORT, 'action deliberation runs are append-only');
END;
""",
    10: """
CREATE TABLE IF NOT EXISTS search_provider_configs (
    config_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    provider_type TEXT NOT NULL CHECK (
        provider_type IN ('brave', 'bing', 'tavily', 'serper')
    ),
    label TEXT NOT NULL,
    key_reference TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL,
    extras_json TEXT NOT NULL,
    rate_limit_per_hour INTEGER NOT NULL CHECK (rate_limit_per_hour > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_provider_subject_status
    ON search_provider_configs(subject_id, status, provider_type);
CREATE UNIQUE INDEX IF NOT EXISTS uq_search_provider_subject_active_label
    ON search_provider_configs(subject_id, label) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS search_provider_revisions (
    revision_id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL REFERENCES search_provider_configs(config_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_provider_revision_config
    ON search_provider_revisions(config_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_search_provider_revision_state
    ON search_provider_revisions(config_id, status);

CREATE TABLE IF NOT EXISTS search_provider_uses (
    use_id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL REFERENCES search_provider_configs(config_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    query_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_provider_use_time
    ON search_provider_uses(config_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_search_provider_use_action
    ON search_provider_uses(action_id);

CREATE TABLE IF NOT EXISTS research_search_runs (
    research_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    planner_call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    goal_id TEXT REFERENCES goals(goal_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('waited', 'accepted', 'no_results', 'failed', 'rejected')
    ),
    initial_method TEXT NOT NULL CHECK (initial_method IN ('api', 'model', 'browser', 'wait')),
    final_method TEXT NOT NULL CHECK (final_method IN ('api', 'model', 'browser', 'wait')),
    provider_config_id TEXT REFERENCES search_provider_configs(config_id),
    query_hash TEXT NOT NULL,
    result_count INTEGER NOT NULL CHECK (result_count >= 0),
    accepted_source_ids_json TEXT NOT NULL,
    rounds_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_research_search_subject_time
    ON research_search_runs(subject_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_search_planner_call
    ON research_search_runs(planner_call_id);

CREATE TRIGGER IF NOT EXISTS prevent_search_provider_revision_update
BEFORE UPDATE ON search_provider_revisions BEGIN
    SELECT RAISE(ABORT, 'search provider revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_search_provider_revision_delete
BEFORE DELETE ON search_provider_revisions BEGIN
    SELECT RAISE(ABORT, 'search provider revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_search_provider_use_update
BEFORE UPDATE ON search_provider_uses BEGIN
    SELECT RAISE(ABORT, 'search provider uses are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_search_provider_use_delete
BEFORE DELETE ON search_provider_uses BEGIN
    SELECT RAISE(ABORT, 'search provider uses are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_search_provider_delete
BEFORE DELETE ON search_provider_configs BEGIN
    SELECT RAISE(ABORT, 'search provider configs cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_research_search_update
BEFORE UPDATE ON research_search_runs BEGIN
    SELECT RAISE(ABORT, 'research search runs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_research_search_delete
BEFORE DELETE ON research_search_runs BEGIN
    SELECT RAISE(ABORT, 'research search runs are append-only');
END;
""",
    11: """
CREATE TABLE IF NOT EXISTS strategy_profiles (
    profile_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    strategy_id TEXT NOT NULL,
    strategy_kind TEXT NOT NULL CHECK (strategy_kind IN ('action', 'research')),
    method TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    successes INTEGER NOT NULL CHECK (successes >= 0),
    failures INTEGER NOT NULL CHECK (failures >= 0),
    inconclusive INTEGER NOT NULL CHECK (inconclusive >= 0),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    last_outcome TEXT NOT NULL CHECK (
        last_outcome IN ('progress', 'informative', 'no_change', 'failure', 'unknown')
    ),
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, goal_id, strategy_id)
);
CREATE INDEX IF NOT EXISTS idx_strategy_profiles_goal_confidence
    ON strategy_profiles(subject_id, goal_id, confidence DESC);

CREATE TABLE IF NOT EXISTS strategy_profile_revisions (
    revision_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES strategy_profiles(profile_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    successes INTEGER NOT NULL CHECK (successes >= 0),
    failures INTEGER NOT NULL CHECK (failures >= 0),
    inconclusive INTEGER NOT NULL CHECK (inconclusive >= 0),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    last_outcome TEXT NOT NULL CHECK (
        last_outcome IN ('progress', 'informative', 'no_change', 'failure', 'unknown')
    ),
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, revision_number)
);

CREATE TABLE IF NOT EXISTS outcome_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    strategy_id TEXT NOT NULL,
    strategy_kind TEXT NOT NULL CHECK (strategy_kind IN ('action', 'research')),
    source_type TEXT NOT NULL CHECK (source_type IN ('action', 'research')),
    source_id TEXT NOT NULL,
    source_status TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('progress', 'informative', 'no_change', 'failure', 'unknown')
    ),
    evidence_event_id TEXT NOT NULL REFERENCES events(event_id),
    observation_id TEXT REFERENCES observations(observation_id),
    result_count INTEGER NOT NULL CHECK (result_count >= 0),
    accepted_source_count INTEGER NOT NULL CHECK (accepted_source_count >= 0),
    progress_before REAL NOT NULL CHECK (progress_before >= 0 AND progress_before <= 1),
    progress_after REAL NOT NULL CHECK (progress_after >= 0 AND progress_after <= 1),
    confidence_before REAL NOT NULL CHECK (confidence_before >= 0 AND confidence_before <= 1),
    confidence_after REAL NOT NULL CHECK (confidence_after >= 0 AND confidence_after <= 1),
    rationale_code TEXT NOT NULL,
    public_summary TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, source_type, source_id)
);
CREATE INDEX IF NOT EXISTS idx_outcome_evaluations_goal_time
    ON outcome_evaluations(subject_id, goal_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_strategy_profile_delete
BEFORE DELETE ON strategy_profiles BEGIN
    SELECT RAISE(ABORT, 'strategy profiles cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_strategy_profile_revision_update
BEFORE UPDATE ON strategy_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'strategy profile revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_strategy_profile_revision_delete
BEFORE DELETE ON strategy_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'strategy profile revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_outcome_evaluation_update
BEFORE UPDATE ON outcome_evaluations BEGIN
    SELECT RAISE(ABORT, 'outcome evaluations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_outcome_evaluation_delete
BEFORE DELETE ON outcome_evaluations BEGIN
    SELECT RAISE(ABORT, 'outcome evaluations are append-only');
END;
""",
    12: """
CREATE TABLE IF NOT EXISTS epistemic_review_runs (
    review_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    model_call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('committed', 'no_change', 'rejected')),
    trigger_observation_id TEXT REFERENCES observations(observation_id),
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    applied_belief_ids_json TEXT NOT NULL,
    resolved_prediction_ids_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key),
    UNIQUE(model_call_id)
);
CREATE INDEX IF NOT EXISTS idx_epistemic_review_subject_time
    ON epistemic_review_runs(subject_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_epistemic_review_update
BEFORE UPDATE ON epistemic_review_runs BEGIN
    SELECT RAISE(ABORT, 'epistemic review runs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_epistemic_review_delete
BEFORE DELETE ON epistemic_review_runs BEGIN
    SELECT RAISE(ABORT, 'epistemic review runs are append-only');
END;
""",
    13: """
CREATE TABLE IF NOT EXISTS memory_accesses (
    access_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    context_type TEXT NOT NULL,
    context_id TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    relevance REAL NOT NULL CHECK (relevance >= 0 AND relevance <= 1),
    access_count INTEGER NOT NULL CHECK (access_count > 0),
    state_hash TEXT NOT NULL,
    first_accessed_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL,
    UNIQUE(subject_id, memory_id, context_type, context_id, query_hash)
);
CREATE INDEX IF NOT EXISTS idx_memory_access_subject_time
    ON memory_accesses(subject_id, last_accessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_access_memory_time
    ON memory_accesses(memory_id, last_accessed_at DESC);

CREATE TABLE IF NOT EXISTS memory_consolidation_runs (
    consolidation_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('committed', 'no_change')),
    reviewed_count INTEGER NOT NULL CHECK (reviewed_count >= 0),
    archived_memory_ids_json TEXT NOT NULL,
    strengthened_memory_ids_json TEXT NOT NULL,
    summary_memory_ids_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_consolidation_subject_time
    ON memory_consolidation_runs(subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_consolidation_members (
    member_id TEXT PRIMARY KEY,
    consolidation_id TEXT NOT NULL REFERENCES memory_consolidation_runs(consolidation_id),
    source_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    result_memory_id TEXT REFERENCES memories(memory_id),
    disposition TEXT NOT NULL CHECK (
        disposition IN ('retained', 'strengthened', 'archived_duplicate', 'summarized')
    ),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(consolidation_id, source_memory_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_consolidation_member_source
    ON memory_consolidation_members(source_memory_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_memory_access_delete
BEFORE DELETE ON memory_accesses BEGIN
    SELECT RAISE(ABORT, 'memory accesses cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_consolidation_update
BEFORE UPDATE ON memory_consolidation_runs BEGIN
    SELECT RAISE(ABORT, 'memory consolidation runs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_consolidation_delete
BEFORE DELETE ON memory_consolidation_runs BEGIN
    SELECT RAISE(ABORT, 'memory consolidation runs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_consolidation_member_update
BEFORE UPDATE ON memory_consolidation_members BEGIN
    SELECT RAISE(ABORT, 'memory consolidation members are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_memory_consolidation_member_delete
BEFORE DELETE ON memory_consolidation_members BEGIN
    SELECT RAISE(ABORT, 'memory consolidation members are append-only');
END;
""",
    14: """
CREATE TABLE IF NOT EXISTS relationship_social_runs (
    social_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    relationship_id TEXT NOT NULL REFERENCES relationships(relationship_id),
    model_call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    interaction_id TEXT REFERENCES interactions(interaction_id),
    idempotency_key TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('contact', 'request_help', 'wait', 'respect_distance')
    ),
    channel TEXT NOT NULL,
    counterparty TEXT NOT NULL,
    topic TEXT NOT NULL,
    rationale TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    evidence_event_ids_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key),
    UNIQUE(model_call_id)
);
CREATE INDEX IF NOT EXISTS idx_relationship_social_subject_time
    ON relationship_social_runs(subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_relationship_social_relationship_time
    ON relationship_social_runs(relationship_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_relationship_social_update
BEFORE UPDATE ON relationship_social_runs BEGIN
    SELECT RAISE(ABORT, 'relationship social runs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_relationship_social_delete
BEFORE DELETE ON relationship_social_runs BEGIN
    SELECT RAISE(ABORT, 'relationship social runs are append-only');
END;
""",
    15: """
CREATE TABLE IF NOT EXISTS self_models (
    self_model_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    version INTEGER NOT NULL CHECK (version > 0),
    model_call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('initial', 'revised')),
    continuity_statement TEXT NOT NULL,
    identity_narrative TEXT NOT NULL,
    values_json TEXT NOT NULL,
    traits_json TEXT NOT NULL,
    commitments_json TEXT NOT NULL,
    uncertainties_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    source_memory_ids_json TEXT NOT NULL,
    source_belief_ids_json TEXT NOT NULL,
    source_goal_ids_json TEXT NOT NULL,
    source_relationship_ids_json TEXT NOT NULL,
    source_personality_candidate_ids_json TEXT NOT NULL,
    source_state_hash TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, version),
    UNIQUE(subject_id, idempotency_key),
    UNIQUE(model_call_id)
);
CREATE INDEX IF NOT EXISTS idx_self_models_subject_version
    ON self_models(subject_id, version DESC);

CREATE TRIGGER IF NOT EXISTS prevent_self_model_update
BEFORE UPDATE ON self_models BEGIN
    SELECT RAISE(ABORT, 'self models are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_self_model_delete
BEFORE DELETE ON self_models BEGIN
    SELECT RAISE(ABORT, 'self models are append-only');
END;
""",
    16: """
CREATE TABLE IF NOT EXISTS thought_agenda_items (
    agenda_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    source_type TEXT NOT NULL CHECK (
        source_type IN ('goal', 'question', 'affect', 'self_model', 'relationship')
    ),
    source_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    urgency REAL NOT NULL CHECK (urgency >= 0 AND urgency <= 1),
    novelty REAL NOT NULL CHECK (novelty >= 0 AND novelty <= 1),
    emotional_weight REAL NOT NULL CHECK (emotional_weight >= 0 AND emotional_weight <= 1),
    recurrence_count INTEGER NOT NULL CHECK (recurrence_count > 0),
    consecutive_no_change INTEGER NOT NULL CHECK (consecutive_no_change >= 0),
    status TEXT NOT NULL CHECK (status IN ('open', 'cooling', 'resolved', 'abandoned')),
    cooldown_until TEXT,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, source_type, source_id)
);
CREATE INDEX IF NOT EXISTS idx_thought_agenda_subject_status
    ON thought_agenda_items(subject_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS thought_agenda_revisions (
    revision_id TEXT PRIMARY KEY,
    agenda_id TEXT NOT NULL REFERENCES thought_agenda_items(agenda_id),
    old_status TEXT,
    new_status TEXT NOT NULL,
    old_recurrence_count INTEGER NOT NULL CHECK (old_recurrence_count >= 0),
    new_recurrence_count INTEGER NOT NULL CHECK (new_recurrence_count > 0),
    old_consecutive_no_change INTEGER NOT NULL CHECK (old_consecutive_no_change >= 0),
    new_consecutive_no_change INTEGER NOT NULL CHECK (new_consecutive_no_change >= 0),
    cooldown_until TEXT,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thought_episodes (
    thought_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    agenda_id TEXT NOT NULL REFERENCES thought_agenda_items(agenda_id),
    model_call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    idempotency_key TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('reflect', 'reframe', 'defer', 'resolve', 'abandon')
    ),
    summary TEXT NOT NULL,
    insight TEXT NOT NULL,
    next_question TEXT,
    source_event_ids_json TEXT NOT NULL,
    source_memory_ids_json TEXT NOT NULL,
    source_belief_ids_json TEXT NOT NULL,
    source_goal_ids_json TEXT NOT NULL,
    source_relationship_ids_json TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    changed_state INTEGER NOT NULL CHECK (changed_state IN (0, 1)),
    created_goal_id TEXT REFERENCES goals(goal_id),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key),
    UNIQUE(model_call_id)
);
CREATE INDEX IF NOT EXISTS idx_thought_episode_subject_time
    ON thought_episodes(subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_thought_episode_agenda_time
    ON thought_episodes(agenda_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_thought_agenda_revision_update
BEFORE UPDATE ON thought_agenda_revisions BEGIN
    SELECT RAISE(ABORT, 'thought agenda revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_thought_agenda_revision_delete
BEFORE DELETE ON thought_agenda_revisions BEGIN
    SELECT RAISE(ABORT, 'thought agenda revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_thought_episode_update
BEFORE UPDATE ON thought_episodes BEGIN
    SELECT RAISE(ABORT, 'thought episodes are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_thought_episode_delete
BEFORE DELETE ON thought_episodes BEGIN
    SELECT RAISE(ABORT, 'thought episodes are append-only');
END;
""",
    17: """
CREATE TABLE IF NOT EXISTS metacognitive_decisions (
    decision_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    strategy TEXT NOT NULL CHECK (
        strategy IN (
            'think', 'research', 'action', 'epistemic_review', 'goal_review',
            'social_review', 'sleep', 'wait'
        )
    ),
    target_type TEXT NOT NULL CHECK (
        target_type IN ('agenda', 'goal', 'observation', 'relationship', 'subject', 'none')
    ),
    target_id TEXT,
    reason_code TEXT NOT NULL,
    score REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    uncertainty REAL NOT NULL CHECK (uncertainty >= 0 AND uncertainty <= 1),
    fixation_risk REAL NOT NULL CHECK (fixation_risk >= 0 AND fixation_risk <= 1),
    resource_pressure REAL NOT NULL CHECK (resource_pressure >= 0 AND resource_pressure <= 1),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        (target_type = 'none' AND target_id IS NULL)
        OR (target_type != 'none' AND target_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_metacognitive_decision_subject_time
    ON metacognitive_decisions(subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cognitive_strategy_profiles (
    profile_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    strategy TEXT NOT NULL CHECK (
        strategy IN (
            'think', 'research', 'action', 'epistemic_review', 'goal_review',
            'social_review', 'sleep', 'wait'
        )
    ),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    productive INTEGER NOT NULL CHECK (productive >= 0),
    stagnant INTEGER NOT NULL CHECK (stagnant >= 0),
    failed INTEGER NOT NULL CHECK (failed >= 0),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    last_outcome TEXT NOT NULL CHECK (
        last_outcome IN ('productive', 'stagnant', 'failed', 'unknown')
    ),
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, strategy)
);

CREATE TABLE IF NOT EXISTS cognitive_strategy_profile_revisions (
    revision_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES cognitive_strategy_profiles(profile_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    productive INTEGER NOT NULL CHECK (productive >= 0),
    stagnant INTEGER NOT NULL CHECK (stagnant >= 0),
    failed INTEGER NOT NULL CHECK (failed >= 0),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    last_outcome TEXT NOT NULL CHECK (
        last_outcome IN ('productive', 'stagnant', 'failed', 'unknown')
    ),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, revision_number),
    UNIQUE(profile_id, source_type, source_id)
);

CREATE TABLE IF NOT EXISTS metacognitive_outcomes (
    outcome_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    decision_id TEXT NOT NULL REFERENCES metacognitive_decisions(decision_id),
    strategy TEXT NOT NULL CHECK (
        strategy IN (
            'think', 'research', 'action', 'epistemic_review', 'goal_review',
            'social_review', 'sleep', 'wait'
        )
    ),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('productive', 'stagnant', 'failed', 'unknown')),
    token_cost INTEGER NOT NULL CHECK (token_cost >= 0),
    rationale_code TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id)
);
CREATE INDEX IF NOT EXISTS idx_metacognitive_outcome_subject_time
    ON metacognitive_outcomes(subject_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_metacognitive_decision_update
BEFORE UPDATE ON metacognitive_decisions BEGIN
    SELECT RAISE(ABORT, 'metacognitive decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_metacognitive_decision_delete
BEFORE DELETE ON metacognitive_decisions BEGIN
    SELECT RAISE(ABORT, 'metacognitive decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_strategy_profile_delete
BEFORE DELETE ON cognitive_strategy_profiles BEGIN
    SELECT RAISE(ABORT, 'cognitive strategy profiles cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_strategy_revision_update
BEFORE UPDATE ON cognitive_strategy_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'cognitive strategy revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_strategy_revision_delete
BEFORE DELETE ON cognitive_strategy_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'cognitive strategy revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_metacognitive_outcome_update
BEFORE UPDATE ON metacognitive_outcomes BEGIN
    SELECT RAISE(ABORT, 'metacognitive outcomes are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_metacognitive_outcome_delete
BEFORE DELETE ON metacognitive_outcomes BEGIN
    SELECT RAISE(ABORT, 'metacognitive outcomes are append-only');
END;
""",
    18: """
CREATE TABLE IF NOT EXISTS value_profiles (
    value_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    value_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    weight REAL NOT NULL CHECK (weight >= 0 AND weight <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'established', 'contested', 'retired')),
    source_event_ids_json TEXT NOT NULL,
    source_memory_ids_json TEXT NOT NULL,
    source_belief_ids_json TEXT NOT NULL,
    source_goal_ids_json TEXT NOT NULL,
    source_relationship_ids_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, value_key)
);
CREATE INDEX IF NOT EXISTS idx_value_profile_subject_weight
    ON value_profiles(subject_id, status, weight DESC);

CREATE TABLE IF NOT EXISTS value_profile_revisions (
    revision_id TEXT PRIMARY KEY,
    value_id TEXT NOT NULL REFERENCES value_profiles(value_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    weight REAL NOT NULL CHECK (weight >= 0 AND weight <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'established', 'contested', 'retired')),
    source_event_ids_json TEXT NOT NULL,
    source_memory_ids_json TEXT NOT NULL,
    source_belief_ids_json TEXT NOT NULL,
    source_goal_ids_json TEXT NOT NULL,
    source_relationship_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(value_id, revision_number)
);

CREATE TABLE IF NOT EXISTS mission_candidates (
    mission_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    title TEXT NOT NULL,
    statement TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('open', 'long_term', 'life_direction')),
    commitment REAL NOT NULL CHECK (commitment >= 0 AND commitment <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (
        status IN ('candidate', 'provisional', 'adopted', 'contested', 'retired')
    ),
    source_value_ids_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    source_memory_ids_json TEXT NOT NULL,
    source_belief_ids_json TEXT NOT NULL,
    source_goal_ids_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_subject_status
    ON mission_candidates(subject_id, status, commitment DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mission_subject_adopted
    ON mission_candidates(subject_id) WHERE status = 'adopted';

CREATE TABLE IF NOT EXISTS mission_candidate_revisions (
    revision_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES mission_candidates(mission_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    title TEXT NOT NULL,
    statement TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('open', 'long_term', 'life_direction')),
    commitment REAL NOT NULL CHECK (commitment >= 0 AND commitment <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (
        status IN ('candidate', 'provisional', 'adopted', 'contested', 'retired')
    ),
    source_value_ids_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    source_memory_ids_json TEXT NOT NULL,
    source_belief_ids_json TEXT NOT NULL,
    source_goal_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mission_id, revision_number)
);

CREATE TABLE IF NOT EXISTS motivation_reviews (
    review_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    model_call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('committed', 'no_change', 'rejected')),
    summary TEXT NOT NULL,
    source_state_hash TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    changed_value_ids_json TEXT NOT NULL,
    changed_mission_ids_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key),
    UNIQUE(model_call_id)
);
CREATE INDEX IF NOT EXISTS idx_motivation_review_subject_time
    ON motivation_reviews(subject_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_value_profile_delete
BEFORE DELETE ON value_profiles BEGIN
    SELECT RAISE(ABORT, 'value profiles cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_value_profile_revision_update
BEFORE UPDATE ON value_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'value profile revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_value_profile_revision_delete
BEFORE DELETE ON value_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'value profile revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_mission_candidate_delete
BEFORE DELETE ON mission_candidates BEGIN
    SELECT RAISE(ABORT, 'mission candidates cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_mission_revision_update
BEFORE UPDATE ON mission_candidate_revisions BEGIN
    SELECT RAISE(ABORT, 'mission revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_mission_revision_delete
BEFORE DELETE ON mission_candidate_revisions BEGIN
    SELECT RAISE(ABORT, 'mission revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_motivation_review_update
BEFORE UPDATE ON motivation_reviews BEGIN
    SELECT RAISE(ABORT, 'motivation reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_motivation_review_delete
BEFORE DELETE ON motivation_reviews BEGIN
    SELECT RAISE(ABORT, 'motivation reviews are append-only');
END;
""",
    19: """
CREATE TABLE IF NOT EXISTS model_calls (
    call_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'executing', 'succeeded', 'failed', 'unknown')
    ),
    response_json TEXT,
    response_hash TEXT,
    usage_estimated INTEGER NOT NULL DEFAULT 0 CHECK (usage_estimated IN (0, 1)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(subject_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS model_attempts (
    attempt_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES model_calls(call_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    budget_day TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('authorized', 'executing', 'succeeded', 'failed', 'unknown', 'cancelled')
    ),
    reserved_input_tokens INTEGER NOT NULL CHECK (reserved_input_tokens >= 0),
    reserved_output_tokens INTEGER NOT NULL CHECK (reserved_output_tokens >= 0),
    reserved_cost_microusd INTEGER NOT NULL CHECK (reserved_cost_microusd >= 0),
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    cost_microusd INTEGER CHECK (cost_microusd >= 0),
    provider_request_id TEXT,
    error_code TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(call_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_model_calls_subject_status
    ON model_calls(subject_id, status);
CREATE INDEX IF NOT EXISTS idx_model_attempts_subject_day
    ON model_attempts(subject_id, budget_day, status);
ALTER TABLE model_calls ADD COLUMN resource_pool TEXT NOT NULL DEFAULT 'deep'
    CHECK (resource_pool IN ('economy', 'deep'));
CREATE INDEX IF NOT EXISTS idx_model_calls_subject_pool_status
    ON model_calls(subject_id, resource_pool, status);

CREATE TABLE IF NOT EXISTS consciousness_frames (
    frame_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    previous_frame_hash TEXT,
    consciousness_state TEXT NOT NULL CHECK (
        consciousness_state IN (
            'awake_quiet', 'attending', 'thinking_economy', 'thinking_deep',
            'researching', 'acting', 'waiting', 'sleeping', 'degraded'
        )
    ),
    attention_type TEXT NOT NULL CHECK (
        attention_type IN (
            'agenda', 'goal', 'observation', 'relationship', 'interaction',
            'subject', 'resource_pool', 'none'
        )
    ),
    attention_id TEXT,
    workflow TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    internal_changes_json TEXT NOT NULL,
    world_changes_json TEXT NOT NULL,
    unresolved_tensions_json TEXT NOT NULL,
    candidate_workflows_json TEXT NOT NULL,
    resource_pool TEXT CHECK (
        resource_pool IN ('economy', 'deep', 'search', 'browser', 'embedding')
    ),
    routing_decision_id TEXT,
    next_wake_at TEXT,
    wake_condition_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, sequence_number),
    CHECK (
        (attention_type = 'none' AND attention_id IS NULL)
        OR (attention_type != 'none' AND attention_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_consciousness_frame_subject_time
    ON consciousness_frames(subject_id, sequence_number DESC);

CREATE TABLE IF NOT EXISTS cognitive_resource_groups (
    group_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    pool TEXT NOT NULL CHECK (pool IN ('economy', 'deep')),
    label TEXT NOT NULL,
    provider_type TEXT NOT NULL CHECK (provider_type = 'openai_compatible'),
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority >= 0 AND priority <= 1000),
    weight INTEGER NOT NULL CHECK (weight >= 1 AND weight <= 1000),
    daily_attempts INTEGER NOT NULL CHECK (daily_attempts >= 0),
    daily_input_tokens INTEGER NOT NULL CHECK (daily_input_tokens >= 0),
    daily_output_tokens INTEGER NOT NULL CHECK (daily_output_tokens >= 0),
    daily_cost_microusd INTEGER NOT NULL CHECK (daily_cost_microusd >= 0),
    input_microusd_per_million INTEGER NOT NULL CHECK (input_microusd_per_million >= 0),
    output_microusd_per_million INTEGER NOT NULL CHECK (output_microusd_per_million >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1 AND max_attempts <= 10),
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'revoked')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id, pool, label)
);
CREATE INDEX IF NOT EXISTS idx_cognitive_resource_group_pool
    ON cognitive_resource_groups(subject_id, pool, status, priority);

CREATE TABLE IF NOT EXISTS cognitive_resource_group_revisions (
    revision_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES cognitive_resource_groups(group_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'revoked')),
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cognitive_resource_keys (
    key_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES cognitive_resource_groups(group_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    key_reference TEXT NOT NULL UNIQUE,
    key_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'cooldown', 'revoked')),
    selection_count INTEGER NOT NULL DEFAULT 0 CHECK (selection_count >= 0),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    cooldown_until TEXT,
    last_selected_at TEXT,
    last_success_at TEXT,
    last_failure_at TEXT,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cognitive_resource_key_group
    ON cognitive_resource_keys(group_id, status, selection_count, created_at);

CREATE TABLE IF NOT EXISTS cognitive_resource_key_events (
    event_id TEXT PRIMARY KEY,
    key_id TEXT NOT NULL REFERENCES cognitive_resource_keys(key_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'configured', 'selected', 'succeeded', 'failed', 'cooled_down',
            'recovered', 'revoked'
        )
    ),
    reason_code TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cognitive_route_decisions (
    decision_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    purpose TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    selected_route TEXT NOT NULL CHECK (
        selected_route IN (
            'rule', 'economy_model', 'deep_model', 'search_api',
            'browser', 'embedding', 'wait'
        )
    ),
    pool TEXT CHECK (pool IN ('economy', 'deep', 'search', 'browser', 'embedding')),
    group_id TEXT REFERENCES cognitive_resource_groups(group_id),
    key_id TEXT REFERENCES cognitive_resource_keys(key_id),
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    risk REAL NOT NULL CHECK (risk >= 0 AND risk <= 1),
    ambiguity REAL NOT NULL CHECK (ambiguity >= 0 AND ambiguity <= 1),
    reason_code TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cognitive_route_subject_time
    ON cognitive_route_decisions(subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cognitive_route_attempts (
    attempt_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    decision_id TEXT NOT NULL REFERENCES cognitive_route_decisions(decision_id),
    group_id TEXT NOT NULL REFERENCES cognitive_resource_groups(group_id),
    key_id TEXT NOT NULL REFERENCES cognitive_resource_keys(key_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('selected', 'succeeded', 'failed', 'unknown')),
    reason_code TEXT NOT NULL,
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cognitive_route_attempt_decision
    ON cognitive_route_attempts(decision_id, attempt_number);

CREATE TABLE IF NOT EXISTS cognitive_route_outcomes (
    outcome_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    decision_id TEXT NOT NULL REFERENCES cognitive_route_decisions(decision_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed', 'deferred', 'cached')),
    result_changed_state INTEGER NOT NULL CHECK (result_changed_state IN (0, 1)),
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    cost_microusd INTEGER NOT NULL CHECK (cost_microusd >= 0),
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    reason_code TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id)
);

CREATE TABLE IF NOT EXISTS waiting_cognitive_tasks (
    task_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    pool TEXT NOT NULL CHECK (pool IN ('economy', 'deep', 'search', 'browser', 'embedding')),
    purpose TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('waiting', 'resolved', 'cancelled')),
    reason_code TEXT NOT NULL,
    retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
    next_retry_at TEXT NOT NULL,
    first_waited_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    UNIQUE(subject_id, pool, purpose)
);

CREATE TABLE IF NOT EXISTS waiting_cognitive_task_revisions (
    revision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES waiting_cognitive_tasks(task_id),
    status TEXT NOT NULL CHECK (status IN ('waiting', 'resolved', 'cancelled')),
    reason_code TEXT NOT NULL,
    retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
    next_retry_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS prevent_consciousness_frame_update
BEFORE UPDATE ON consciousness_frames BEGIN
    SELECT RAISE(ABORT, 'consciousness frames are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_consciousness_frame_delete
BEFORE DELETE ON consciousness_frames BEGIN
    SELECT RAISE(ABORT, 'consciousness frames are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_resource_group_revision_update
BEFORE UPDATE ON cognitive_resource_group_revisions BEGIN
    SELECT RAISE(ABORT, 'cognitive resource group revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_resource_group_revision_delete
BEFORE DELETE ON cognitive_resource_group_revisions BEGIN
    SELECT RAISE(ABORT, 'cognitive resource group revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_resource_key_event_update
BEFORE UPDATE ON cognitive_resource_key_events BEGIN
    SELECT RAISE(ABORT, 'cognitive resource key events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_resource_key_event_delete
BEFORE DELETE ON cognitive_resource_key_events BEGIN
    SELECT RAISE(ABORT, 'cognitive resource key events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_route_decision_update
BEFORE UPDATE ON cognitive_route_decisions BEGIN
    SELECT RAISE(ABORT, 'cognitive route decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_route_decision_delete
BEFORE DELETE ON cognitive_route_decisions BEGIN
    SELECT RAISE(ABORT, 'cognitive route decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_route_attempt_update
BEFORE UPDATE ON cognitive_route_attempts BEGIN
    SELECT RAISE(ABORT, 'cognitive route attempts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_route_attempt_delete
BEFORE DELETE ON cognitive_route_attempts BEGIN
    SELECT RAISE(ABORT, 'cognitive route attempts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_route_outcome_update
BEFORE UPDATE ON cognitive_route_outcomes BEGIN
    SELECT RAISE(ABORT, 'cognitive route outcomes are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_cognitive_route_outcome_delete
BEFORE DELETE ON cognitive_route_outcomes BEGIN
    SELECT RAISE(ABORT, 'cognitive route outcomes are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_waiting_cognitive_task_revision_update
BEFORE UPDATE ON waiting_cognitive_task_revisions BEGIN
    SELECT RAISE(ABORT, 'waiting cognitive task revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_waiting_cognitive_task_revision_delete
BEFORE DELETE ON waiting_cognitive_task_revisions BEGIN
    SELECT RAISE(ABORT, 'waiting cognitive task revisions are append-only');
END;
""",
    20: """
CREATE TABLE IF NOT EXISTS autonomous_projects (
    project_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    formation_call_id TEXT NOT NULL REFERENCES model_calls(call_id) UNIQUE,
    project_key TEXT NOT NULL,
    project_type TEXT NOT NULL CHECK (
        project_type IN (
            'research', 'prediction', 'knowledge', 'software_prototype',
            'self_development', 'collaboration'
        )
    ),
    title TEXT NOT NULL,
    purpose TEXT NOT NULL,
    deliverable TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    size_class TEXT NOT NULL CHECK (size_class IN ('micro', 'small')),
    estimated_duration_hours REAL NOT NULL CHECK (
        estimated_duration_hours > 0 AND estimated_duration_hours <= 168
    ),
    status TEXT NOT NULL CHECK (
        status IN ('planned', 'active', 'paused', 'blocked', 'completed', 'abandoned')
    ),
    progress REAL NOT NULL CHECK (progress >= 0 AND progress <= 1),
    current_phase_id TEXT,
    max_cycles INTEGER NOT NULL CHECK (max_cycles >= 1),
    max_model_calls INTEGER NOT NULL CHECK (max_model_calls >= 1),
    max_searches INTEGER NOT NULL CHECK (max_searches >= 0),
    max_external_actions INTEGER NOT NULL CHECK (max_external_actions >= 0),
    max_storage_bytes INTEGER NOT NULL CHECK (max_storage_bytes >= 0),
    source_event_ids_json TEXT NOT NULL,
    source_value_ids_json TEXT NOT NULL,
    source_mission_id TEXT REFERENCES mission_candidates(mission_id),
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(subject_id, project_key)
);
CREATE INDEX IF NOT EXISTS idx_autonomous_project_subject_status
    ON autonomous_projects(subject_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomous_project_goal_nonterminal
    ON autonomous_projects(subject_id, goal_id)
    WHERE status IN ('planned', 'active', 'paused', 'blocked');

CREATE TABLE IF NOT EXISTS autonomous_project_revisions (
    revision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('planned', 'active', 'paused', 'blocked', 'completed', 'abandoned')
    ),
    progress REAL NOT NULL CHECK (progress >= 0 AND progress <= 1),
    current_phase_id TEXT,
    max_cycles INTEGER NOT NULL CHECK (max_cycles >= 1),
    max_model_calls INTEGER NOT NULL CHECK (max_model_calls >= 1),
    max_searches INTEGER NOT NULL CHECK (max_searches >= 0),
    max_external_actions INTEGER NOT NULL CHECK (max_external_actions >= 0),
    max_storage_bytes INTEGER NOT NULL CHECK (max_storage_bytes >= 0),
    reason TEXT NOT NULL,
    evidence_event_ids_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, revision_number)
);

CREATE TABLE IF NOT EXISTS autonomous_project_phases (
    phase_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    phase_key TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    output_type TEXT NOT NULL CHECK (
        output_type IN (
            'research_note', 'prediction_record', 'knowledge_collection',
            'software_prototype', 'self_experiment', 'collaboration_request'
        )
    ),
    acceptance_criteria_json TEXT NOT NULL,
    dependency_keys_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'active', 'completed', 'blocked', 'skipped')
    ),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    no_progress_count INTEGER NOT NULL CHECK (no_progress_count >= 0),
    state_hash TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(project_id, phase_key),
    UNIQUE(project_id, position)
);
CREATE INDEX IF NOT EXISTS idx_autonomous_project_phase_status
    ON autonomous_project_phases(project_id, status, position);

CREATE TABLE IF NOT EXISTS autonomous_project_phase_revisions (
    revision_id TEXT PRIMARY KEY,
    phase_id TEXT NOT NULL REFERENCES autonomous_project_phases(phase_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'active', 'completed', 'blocked', 'skipped')
    ),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    no_progress_count INTEGER NOT NULL CHECK (no_progress_count >= 0),
    reason TEXT NOT NULL,
    evidence_event_ids_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(phase_id, revision_number)
);

CREATE TABLE IF NOT EXISTS autonomous_project_reviews (
    review_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    model_call_id TEXT NOT NULL REFERENCES model_calls(call_id) UNIQUE,
    idempotency_key TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN (
            'activate', 'continue', 'complete_phase', 'scale_down',
            'pause', 'abandon', 'request_help', 'wait'
        )
    ),
    phase_id TEXT REFERENCES autonomous_project_phases(phase_id),
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_event_ids_json TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    resulting_status TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_autonomous_project_review_time
    ON autonomous_project_reviews(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS autonomous_project_resource_uses (
    usage_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    resource_type TEXT NOT NULL CHECK (
        resource_type IN ('model_call', 'search', 'external_action', 'storage')
    ),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, resource_type, source_type, source_id)
);

CREATE TABLE IF NOT EXISTS autonomous_project_assistance_requests (
    request_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    phase_id TEXT REFERENCES autonomous_project_phases(phase_id),
    request_kind TEXT NOT NULL CHECK (
        request_kind IN ('human_help', 'resource_access', 'technical_support')
    ),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    public_summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'withdrawn')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomous_project_open_assistance
    ON autonomous_project_assistance_requests(project_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS autonomous_project_sleep_reflections (
    reflection_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    sleep_id TEXT NOT NULL REFERENCES sleep_runs(sleep_id),
    assessment TEXT NOT NULL,
    suggested_disposition TEXT NOT NULL CHECK (
        suggested_disposition IN ('continue', 'scale_down', 'pause', 'abandon')
    ),
    unresolved_risks_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, sleep_id)
);

CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_delete
BEFORE DELETE ON autonomous_projects BEGIN
    SELECT RAISE(ABORT, 'autonomous projects cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS restrict_autonomous_project_update
BEFORE UPDATE ON autonomous_projects WHEN
    NEW.subject_id != OLD.subject_id OR
    NEW.goal_id != OLD.goal_id OR
    NEW.formation_call_id != OLD.formation_call_id OR
    NEW.project_key != OLD.project_key OR
    NEW.project_type != OLD.project_type OR
    NEW.title != OLD.title OR
    NEW.purpose != OLD.purpose OR
    NEW.deliverable != OLD.deliverable OR
    NEW.acceptance_criteria_json != OLD.acceptance_criteria_json OR
    NEW.size_class != OLD.size_class OR
    NEW.estimated_duration_hours != OLD.estimated_duration_hours OR
    NEW.source_event_ids_json != OLD.source_event_ids_json OR
    NEW.source_value_ids_json != OLD.source_value_ids_json OR
    NOT (NEW.source_mission_id IS OLD.source_mission_id) OR
    NEW.created_at != OLD.created_at OR
    NEW.current_revision != OLD.current_revision + 1 OR
    NEW.updated_at < OLD.updated_at
BEGIN
    SELECT RAISE(ABORT, 'autonomous project immutable fields cannot change');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_revision_update
BEFORE UPDATE ON autonomous_project_revisions BEGIN
    SELECT RAISE(ABORT, 'autonomous project revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_revision_delete
BEFORE DELETE ON autonomous_project_revisions BEGIN
    SELECT RAISE(ABORT, 'autonomous project revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_phase_delete
BEFORE DELETE ON autonomous_project_phases BEGIN
    SELECT RAISE(ABORT, 'autonomous project phases cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS restrict_autonomous_project_phase_update
BEFORE UPDATE ON autonomous_project_phases WHEN
    NEW.project_id != OLD.project_id OR
    NEW.subject_id != OLD.subject_id OR
    NEW.phase_key != OLD.phase_key OR
    NEW.position != OLD.position OR
    NEW.title != OLD.title OR
    NEW.objective != OLD.objective OR
    NEW.output_type != OLD.output_type OR
    NEW.acceptance_criteria_json != OLD.acceptance_criteria_json OR
    NEW.dependency_keys_json != OLD.dependency_keys_json OR
    NEW.created_at != OLD.created_at OR
    NEW.current_revision != OLD.current_revision + 1 OR
    NEW.attempt_count < OLD.attempt_count OR
    NEW.updated_at < OLD.updated_at
BEGIN
    SELECT RAISE(ABORT, 'autonomous project phase immutable fields cannot change');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_phase_revision_update
BEFORE UPDATE ON autonomous_project_phase_revisions BEGIN
    SELECT RAISE(ABORT, 'autonomous project phase revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_phase_revision_delete
BEFORE DELETE ON autonomous_project_phase_revisions BEGIN
    SELECT RAISE(ABORT, 'autonomous project phase revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_review_update
BEFORE UPDATE ON autonomous_project_reviews BEGIN
    SELECT RAISE(ABORT, 'autonomous project reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_review_delete
BEFORE DELETE ON autonomous_project_reviews BEGIN
    SELECT RAISE(ABORT, 'autonomous project reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_resource_use_update
BEFORE UPDATE ON autonomous_project_resource_uses BEGIN
    SELECT RAISE(ABORT, 'autonomous project resource uses are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_resource_use_delete
BEFORE DELETE ON autonomous_project_resource_uses BEGIN
    SELECT RAISE(ABORT, 'autonomous project resource uses are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_sleep_reflection_update
BEFORE UPDATE ON autonomous_project_sleep_reflections BEGIN
    SELECT RAISE(ABORT, 'autonomous project sleep reflections are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_autonomous_project_sleep_reflection_delete
BEFORE DELETE ON autonomous_project_sleep_reflections BEGIN
    SELECT RAISE(ABORT, 'autonomous project sleep reflections are append-only');
END;
""",
    21: """
CREATE INDEX IF NOT EXISTS idx_research_search_project_phase
    ON research_search_runs(project_id, phase_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_deliberation_project_phase
    ON action_deliberation_runs(project_id, phase_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_actions_project_phase
    ON actions(project_id, phase_id, prepared_at DESC);

CREATE TABLE IF NOT EXISTS autonomous_project_executions (
    execution_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    phase_id TEXT NOT NULL REFERENCES autonomous_project_phases(phase_id),
    execution_key TEXT NOT NULL,
    execution_type TEXT NOT NULL CHECK (
        execution_type IN ('research_note', 'prediction_record', 'knowledge_collection',
                           'software_prototype', 'self_experiment', 'collaboration_request')
    ),
    workflow TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'executing', 'succeeded', 'failed', 'unknown', 'blocked')
    ),
    research_id TEXT REFERENCES research_search_runs(research_id),
    action_id TEXT REFERENCES actions(action_id),
    model_call_id TEXT REFERENCES model_calls(call_id),
    artifact_path TEXT,
    artifact_hash TEXT,
    result_hash TEXT,
    acceptance_json TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(subject_id, execution_key),
    UNIQUE(project_id, phase_id, execution_key)
);
CREATE INDEX IF NOT EXISTS idx_project_execution_project_phase
    ON autonomous_project_executions(project_id, phase_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_execution_status
    ON autonomous_project_executions(subject_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS autonomous_project_execution_revisions (
    revision_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES autonomous_project_executions(execution_id),
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'executing', 'succeeded', 'failed', 'unknown', 'blocked')
    ),
    result_hash TEXT,
    reason TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(execution_id, created_at, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_project_execution_revision_time
    ON autonomous_project_execution_revisions(execution_id, created_at);

CREATE TRIGGER IF NOT EXISTS prevent_project_execution_delete
BEFORE DELETE ON autonomous_project_executions BEGIN
    SELECT RAISE(ABORT, 'autonomous project executions cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS prevent_project_execution_revision_update
BEFORE UPDATE ON autonomous_project_execution_revisions BEGIN
    SELECT RAISE(ABORT, 'autonomous project execution revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_project_execution_revision_delete
BEFORE DELETE ON autonomous_project_execution_revisions BEGIN
    SELECT RAISE(ABORT, 'autonomous project execution revisions are append-only');
END;
""",
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
    53: """
-- Stage 4-A-1 intentionally registers only public chain metadata, public
-- addresses, and observed balances. No signer, transaction, or credential
-- material belongs in this schema.
CREATE TABLE IF NOT EXISTS wallet_networks (
    network_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    label TEXT NOT NULL CHECK (label = trim(label) AND length(label) BETWEEN 1 AND 128),
    chain_family TEXT NOT NULL CHECK (chain_family IN ('evm')),
    chain_id INTEGER NOT NULL CHECK (chain_id >= 1),
    native_symbol TEXT NOT NULL CHECK (
        length(native_symbol) BETWEEN 1 AND 32
        AND native_symbol = upper(native_symbol)
        AND native_symbol NOT GLOB '*[^A-Z0-9._-]*'
    ),
    rpc_url TEXT CHECK (rpc_url IS NULL OR length(rpc_url) BETWEEN 1 AND 512),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    revoked_audit_id TEXT REFERENCES audit_records(audit_id),
    UNIQUE(subject_id, label),
    UNIQUE(subject_id, chain_family, chain_id),
    CHECK (
        (status = 'active' AND revoked_at IS NULL AND revoke_reason IS NULL
         AND revoked_audit_id IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL
            AND revoke_reason IS NOT NULL AND length(trim(revoke_reason)) > 0
            AND revoked_audit_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_wallet_networks_subject_status
    ON wallet_networks(subject_id, status, created_at DESC, network_id);

CREATE TABLE IF NOT EXISTS wallet_network_revisions (
    revision_id TEXT PRIMARY KEY,
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number BETWEEN 1 AND 2),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 2000),
    audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(network_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wallet_network_revisions_subject
    ON wallet_network_revisions(subject_id, network_id, revision_number);

CREATE TABLE IF NOT EXISTS wallet_assets (
    asset_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_type TEXT NOT NULL CHECK (asset_type IN ('native', 'token')),
    contract_address TEXT,
    name TEXT NOT NULL CHECK (name = trim(name) AND length(name) BETWEEN 1 AND 128),
    symbol TEXT NOT NULL CHECK (
        length(symbol) BETWEEN 1 AND 32
        AND symbol = upper(symbol)
        AND symbol NOT GLOB '*[^A-Z0-9._-]*'
    ),
    decimals INTEGER NOT NULL CHECK (decimals BETWEEN 0 AND 255),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    revoked_audit_id TEXT REFERENCES audit_records(audit_id),
    UNIQUE(subject_id, network_id, contract_address),
    CHECK (
        (asset_type = 'native' AND contract_address IS NULL)
        OR (asset_type = 'token' AND contract_address IS NOT NULL
            AND length(contract_address) = 42
            AND substr(contract_address, 1, 2) = '0x'
            AND lower(contract_address) = contract_address
            AND substr(contract_address, 3) NOT GLOB '*[^0-9a-f]*')
    ),
    CHECK (
        (status = 'active' AND revoked_at IS NULL AND revoke_reason IS NULL
         AND revoked_audit_id IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL
            AND revoke_reason IS NOT NULL AND length(trim(revoke_reason)) > 0
            AND revoked_audit_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_assets_native_network
    ON wallet_assets(subject_id, network_id) WHERE asset_type = 'native';
CREATE INDEX IF NOT EXISTS idx_wallet_assets_subject_network_status
    ON wallet_assets(subject_id, network_id, status, created_at DESC, asset_id);

CREATE TABLE IF NOT EXISTS wallet_asset_revisions (
    revision_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number BETWEEN 1 AND 2),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 2000),
    audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wallet_asset_revisions_subject
    ON wallet_asset_revisions(subject_id, asset_id, revision_number);

CREATE TABLE IF NOT EXISTS wallet_addresses (
    address_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    label TEXT NOT NULL CHECK (label = trim(label) AND length(label) BETWEEN 1 AND 128),
    address TEXT NOT NULL CHECK (
        length(address) = 42
        AND substr(address, 1, 2) = '0x'
        AND lower(address) = address
        AND substr(address, 3) NOT GLOB '*[^0-9a-f]*'
    ),
    purpose TEXT NOT NULL CHECK (
        purpose IN ('treasury', 'spending', 'observation', 'external')
    ),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    revoked_audit_id TEXT REFERENCES audit_records(audit_id),
    UNIQUE(subject_id, network_id, label),
    UNIQUE(subject_id, network_id, address),
    CHECK (
        (status = 'active' AND revoked_at IS NULL AND revoke_reason IS NULL
         AND revoked_audit_id IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL
            AND revoke_reason IS NOT NULL AND length(trim(revoke_reason)) > 0
            AND revoked_audit_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_wallet_addresses_subject_network_status
    ON wallet_addresses(subject_id, network_id, status, created_at DESC, address_id);

CREATE TABLE IF NOT EXISTS wallet_address_revisions (
    revision_id TEXT PRIMARY KEY,
    address_id TEXT NOT NULL REFERENCES wallet_addresses(address_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number BETWEEN 1 AND 2),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 2000),
    audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(address_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wallet_address_revisions_subject
    ON wallet_address_revisions(subject_id, address_id, revision_number);

CREATE TABLE IF NOT EXISTS wallet_balance_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    address_id TEXT NOT NULL REFERENCES wallet_addresses(address_id),
    balance TEXT NOT NULL CHECK (
        length(balance) BETWEEN 1 AND 256
        AND (balance = '0' OR (balance GLOB '[1-9]*' AND balance NOT GLOB '*[^0-9]*'))
    ),
    source TEXT NOT NULL CHECK (
        length(source) BETWEEN 1 AND 64
        AND source GLOB '[a-z]*' AND source NOT GLOB '*[^a-z0-9_]*'
    ),
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    )
);
CREATE INDEX IF NOT EXISTS idx_wallet_balance_snapshots_subject_lookup
    ON wallet_balance_snapshots(
        subject_id, network_id, asset_id, address_id, observed_at DESC, snapshot_id DESC
    );

CREATE TRIGGER IF NOT EXISTS validate_wallet_network_creation_audit
BEFORE INSERT ON wallet_networks
WHEN NOT EXISTS (
    SELECT 1 FROM audit_records a
    WHERE a.audit_id = NEW.created_audit_id
      AND a.subject_id = NEW.subject_id
      AND a.action = 'wallet_network_registered'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet network creation audit mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_network_identity_update
BEFORE UPDATE OF network_id, subject_id, label, chain_family, chain_id,
                 native_symbol, rpc_url, created_at, created_audit_id ON wallet_networks
BEGIN
    SELECT RAISE(ABORT, 'wallet network identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_network_transition
BEFORE UPDATE ON wallet_networks
WHEN NOT (
    (NEW.status = OLD.status
     AND NEW.state_hash = OLD.state_hash
     AND NEW.revoked_at IS OLD.revoked_at
     AND NEW.revoke_reason IS OLD.revoke_reason
     AND NEW.revoked_audit_id IS OLD.revoked_audit_id)
    OR (
        OLD.status = 'active' AND NEW.status = 'revoked'
        AND NEW.state_hash <> OLD.state_hash
        AND NEW.revoked_at IS NOT NULL
        AND NEW.revoke_reason IS NOT NULL
        AND NEW.revoked_audit_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM audit_records a
            WHERE a.audit_id = NEW.revoked_audit_id
              AND a.subject_id = NEW.subject_id
              AND a.action = 'wallet_network_revoked'
        )
        AND NOT EXISTS (
            SELECT 1 FROM wallet_assets a
            WHERE a.subject_id = NEW.subject_id AND a.network_id = NEW.network_id
              AND a.status = 'active'
        )
        AND NOT EXISTS (
            SELECT 1 FROM wallet_addresses a
            WHERE a.subject_id = NEW.subject_id AND a.network_id = NEW.network_id
              AND a.status = 'active'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'wallet network transition is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_network_delete
BEFORE DELETE ON wallet_networks
BEGIN
    SELECT RAISE(ABORT, 'wallet networks cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_network_revision_insert
BEFORE INSERT ON wallet_network_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_networks n
    JOIN audit_records a ON a.audit_id = NEW.audit_id
    WHERE n.network_id = NEW.network_id
      AND n.subject_id = NEW.subject_id
      AND n.status = NEW.status
      AND a.subject_id = NEW.subject_id
      AND ((NEW.status = 'active' AND a.action = 'wallet_network_registered')
           OR (NEW.status = 'revoked' AND a.action = 'wallet_network_revoked'))
) OR NEW.revision_number != COALESCE((
    SELECT MAX(r.revision_number) + 1 FROM wallet_network_revisions r
    WHERE r.network_id = NEW.network_id
), 1) OR (NEW.revision_number = 1 AND NEW.status != 'active')
OR (NEW.revision_number = 2 AND (
    NEW.status != 'revoked' OR NOT EXISTS (
        SELECT 1 FROM wallet_network_revisions r
        WHERE r.network_id = NEW.network_id AND r.revision_number = 1 AND r.status = 'active'
    )
))
BEGIN
    SELECT RAISE(ABORT, 'wallet network revision is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_network_revision_update
BEFORE UPDATE ON wallet_network_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet network revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_network_revision_delete
BEFORE DELETE ON wallet_network_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet network revisions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS validate_wallet_asset_creation_audit
BEFORE INSERT ON wallet_assets
WHEN NOT EXISTS (
    SELECT 1 FROM audit_records a
    WHERE a.audit_id = NEW.created_audit_id
      AND a.subject_id = NEW.subject_id
      AND a.action = 'wallet_asset_registered'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet asset creation audit mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_asset_identity_update
BEFORE UPDATE OF asset_id, subject_id, network_id, asset_type, contract_address,
                 name, symbol, decimals, created_at, created_audit_id ON wallet_assets
BEGIN
    SELECT RAISE(ABORT, 'wallet asset identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_asset_transition
BEFORE UPDATE ON wallet_assets
WHEN NOT (
    (NEW.status = OLD.status
     AND NEW.state_hash = OLD.state_hash
     AND NEW.revoked_at IS OLD.revoked_at
     AND NEW.revoke_reason IS OLD.revoke_reason
     AND NEW.revoked_audit_id IS OLD.revoked_audit_id)
    OR (
        OLD.status = 'active' AND NEW.status = 'revoked'
        AND NEW.state_hash <> OLD.state_hash
        AND NEW.revoked_at IS NOT NULL
        AND NEW.revoke_reason IS NOT NULL
        AND NEW.revoked_audit_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM audit_records a
            WHERE a.audit_id = NEW.revoked_audit_id
              AND a.subject_id = NEW.subject_id
              AND a.action = 'wallet_asset_revoked'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'wallet asset transition is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_asset_delete
BEFORE DELETE ON wallet_assets
BEGIN
    SELECT RAISE(ABORT, 'wallet assets cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_asset_revision_insert
BEFORE INSERT ON wallet_asset_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_assets a
    JOIN audit_records audit ON audit.audit_id = NEW.audit_id
    WHERE a.asset_id = NEW.asset_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.status
      AND audit.subject_id = NEW.subject_id
      AND ((NEW.status = 'active' AND audit.action = 'wallet_asset_registered')
           OR (NEW.status = 'revoked' AND audit.action = 'wallet_asset_revoked'))
) OR NEW.revision_number != COALESCE((
    SELECT MAX(r.revision_number) + 1 FROM wallet_asset_revisions r
    WHERE r.asset_id = NEW.asset_id
), 1) OR (NEW.revision_number = 1 AND NEW.status != 'active')
OR (NEW.revision_number = 2 AND (
    NEW.status != 'revoked' OR NOT EXISTS (
        SELECT 1 FROM wallet_asset_revisions r
        WHERE r.asset_id = NEW.asset_id AND r.revision_number = 1 AND r.status = 'active'
    )
))
BEGIN
    SELECT RAISE(ABORT, 'wallet asset revision is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_asset_revision_update
BEFORE UPDATE ON wallet_asset_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet asset revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_asset_revision_delete
BEFORE DELETE ON wallet_asset_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet asset revisions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS validate_wallet_address_creation_audit
BEFORE INSERT ON wallet_addresses
WHEN NOT EXISTS (
    SELECT 1 FROM audit_records a
    WHERE a.audit_id = NEW.created_audit_id
      AND a.subject_id = NEW.subject_id
      AND a.action = 'wallet_address_registered'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet address creation audit mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_address_identity_update
BEFORE UPDATE OF address_id, subject_id, network_id, label, address, purpose,
                 created_at, created_audit_id ON wallet_addresses
BEGIN
    SELECT RAISE(ABORT, 'wallet address identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_address_transition
BEFORE UPDATE ON wallet_addresses
WHEN NOT (
    (NEW.status = OLD.status
     AND NEW.state_hash = OLD.state_hash
     AND NEW.revoked_at IS OLD.revoked_at
     AND NEW.revoke_reason IS OLD.revoke_reason
     AND NEW.revoked_audit_id IS OLD.revoked_audit_id)
    OR (
        OLD.status = 'active' AND NEW.status = 'revoked'
        AND NEW.state_hash <> OLD.state_hash
        AND NEW.revoked_at IS NOT NULL
        AND NEW.revoke_reason IS NOT NULL
        AND NEW.revoked_audit_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM audit_records a
            WHERE a.audit_id = NEW.revoked_audit_id
              AND a.subject_id = NEW.subject_id
              AND a.action = 'wallet_address_revoked'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'wallet address transition is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_address_delete
BEFORE DELETE ON wallet_addresses
BEGIN
    SELECT RAISE(ABORT, 'wallet addresses cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_address_revision_insert
BEFORE INSERT ON wallet_address_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_addresses a
    JOIN audit_records audit ON audit.audit_id = NEW.audit_id
    WHERE a.address_id = NEW.address_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.status
      AND audit.subject_id = NEW.subject_id
      AND ((NEW.status = 'active' AND audit.action = 'wallet_address_registered')
           OR (NEW.status = 'revoked' AND audit.action = 'wallet_address_revoked'))
) OR NEW.revision_number != COALESCE((
    SELECT MAX(r.revision_number) + 1 FROM wallet_address_revisions r
    WHERE r.address_id = NEW.address_id
), 1) OR (NEW.revision_number = 1 AND NEW.status != 'active')
OR (NEW.revision_number = 2 AND (
    NEW.status != 'revoked' OR NOT EXISTS (
        SELECT 1 FROM wallet_address_revisions r
        WHERE r.address_id = NEW.address_id AND r.revision_number = 1 AND r.status = 'active'
    )
))
BEGIN
    SELECT RAISE(ABORT, 'wallet address revision is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_address_revision_update
BEFORE UPDATE ON wallet_address_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet address revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_address_revision_delete
BEFORE DELETE ON wallet_address_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet address revisions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_snapshot_insert
BEFORE INSERT ON wallet_balance_snapshots
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_networks n
    JOIN wallet_assets a ON a.asset_id = NEW.asset_id
    JOIN wallet_addresses d ON d.address_id = NEW.address_id
    WHERE n.network_id = NEW.network_id
      AND n.subject_id = NEW.subject_id
      AND a.subject_id = NEW.subject_id AND a.network_id = NEW.network_id
      AND d.subject_id = NEW.subject_id AND d.network_id = NEW.network_id
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance snapshot reference mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_snapshot_update
BEFORE UPDATE ON wallet_balance_snapshots
BEGIN
    SELECT RAISE(ABORT, 'wallet balance snapshots are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_snapshot_delete
BEFORE DELETE ON wallet_balance_snapshots
BEGIN
    SELECT RAISE(ABORT, 'wallet balance snapshots cannot be deleted');
END;
""",
    54: """
-- Stage 4-A-3 adds only a durable scheduler for the bounded read-only RPC
-- acquisition introduced in 4-A-2.  Runs reserve at most two JSON-RPC calls
-- (chain identity plus one balance read); no transaction or signer material is
-- represented here.
CREATE TABLE IF NOT EXISTS wallet_balance_acquisition_runs (
    run_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    address_id TEXT NOT NULL REFERENCES wallet_addresses(address_id),
    idempotency_key TEXT NOT NULL CHECK (
        length(idempotency_key) BETWEEN 1 AND 128
        AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'retry_wait', 'succeeded',
            'failed', 'unknown', 'cancelled'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 5),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 5),
    next_attempt_at TEXT,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR (
            length(last_error_code) BETWEEN 1 AND 96
            AND last_error_code GLOB 'wallet_rpc_*'
            AND last_error_code NOT GLOB '*[^a-z0-9_]*'
        )
    ),
    snapshot_id TEXT UNIQUE REFERENCES wallet_balance_snapshots(snapshot_id),
    claim_token TEXT UNIQUE,
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR length(trim(lease_owner)) BETWEEN 1 AND 128
    ),
    lease_expires_at TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (attempt_count <= max_attempts),
    CHECK (
        (status = 'queued' AND attempt_count < max_attempts AND next_attempt_at IS NOT NULL
         AND last_error_code IS NULL AND snapshot_id IS NULL
         AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
         AND completed_at IS NULL)
        OR (status = 'running' AND attempt_count >= 1 AND next_attempt_at IS NULL
            AND snapshot_id IS NULL AND claim_token IS NOT NULL
            AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
            AND started_at IS NOT NULL AND completed_at IS NULL)
        OR (status = 'retry_wait' AND attempt_count >= 1
            AND attempt_count < max_attempts AND next_attempt_at IS NOT NULL
            AND last_error_code IS NOT NULL AND snapshot_id IS NULL
            AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND started_at IS NOT NULL AND completed_at IS NULL)
        OR (status = 'succeeded' AND attempt_count >= 1 AND next_attempt_at IS NULL
            AND last_error_code IS NULL AND snapshot_id IS NOT NULL
            AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND started_at IS NOT NULL AND completed_at IS NOT NULL)
        OR (status IN ('failed', 'unknown') AND attempt_count >= 1
            AND next_attempt_at IS NULL AND last_error_code IS NOT NULL
            AND snapshot_id IS NULL AND claim_token IS NULL AND lease_owner IS NULL
            AND lease_expires_at IS NULL AND started_at IS NOT NULL
            AND completed_at IS NOT NULL)
        OR (status = 'cancelled' AND next_attempt_at IS NULL
            AND last_error_code IS NOT NULL AND snapshot_id IS NULL
            AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_balance_acquisition_idempotency
    ON wallet_balance_acquisition_runs(subject_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_wallet_balance_acquisition_runs_due
    ON wallet_balance_acquisition_runs(
        subject_id, status, next_attempt_at, created_at, run_id
    );
CREATE INDEX IF NOT EXISTS idx_wallet_balance_acquisition_runs_network_lease
    ON wallet_balance_acquisition_runs(
        subject_id, network_id, status, lease_expires_at, run_id
    );
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_balance_acquisition_active_target
    ON wallet_balance_acquisition_runs(subject_id, asset_id, address_id)
    WHERE status IN ('queued', 'running', 'retry_wait');

CREATE TABLE IF NOT EXISTS wallet_balance_acquisition_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES wallet_balance_acquisition_runs(run_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    address_id TEXT NOT NULL REFERENCES wallet_addresses(address_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number BETWEEN 1 AND 5),
    status TEXT NOT NULL CHECK (status IN ('executing', 'succeeded', 'failed', 'unknown')),
    reserved_request_count INTEGER NOT NULL CHECK (reserved_request_count = 2),
    request_count INTEGER CHECK (request_count IS NULL OR request_count BETWEEN 0 AND 2),
    error_code TEXT CHECK (
        error_code IS NULL OR (
            length(error_code) BETWEEN 1 AND 96
            AND error_code GLOB 'wallet_rpc_*'
            AND error_code NOT GLOB '*[^a-z0-9_]*'
        )
    ),
    snapshot_id TEXT UNIQUE REFERENCES wallet_balance_snapshots(snapshot_id),
    claim_token TEXT NOT NULL,
    lease_owner TEXT NOT NULL CHECK (length(trim(lease_owner)) BETWEEN 1 AND 128),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, attempt_number),
    UNIQUE(run_id, claim_token),
    CHECK (
        (status = 'executing' AND request_count IS NULL AND error_code IS NULL
         AND snapshot_id IS NULL AND completed_at IS NULL)
        OR (status = 'succeeded' AND request_count = 2 AND error_code IS NULL
            AND snapshot_id IS NOT NULL AND completed_at IS NOT NULL)
        OR (status = 'failed' AND request_count IS NOT NULL AND error_code IS NOT NULL
            AND snapshot_id IS NULL AND completed_at IS NOT NULL)
        OR (status = 'unknown' AND request_count IS NULL AND error_code IS NOT NULL
            AND snapshot_id IS NULL AND completed_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_wallet_balance_acquisition_attempts_budget
    ON wallet_balance_acquisition_attempts(
        subject_id, network_id, started_at, attempt_id
    );
CREATE INDEX IF NOT EXISTS idx_wallet_balance_acquisition_attempts_run
    ON wallet_balance_acquisition_attempts(run_id, attempt_number, attempt_id);

DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_run_insert;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_run_identity_update;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_terminal_update;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_run_transition;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempt_increment;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_run_completion;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_run_delete;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempt_insert;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_attempt_identity_update;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempt_transition;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempt_snapshot;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_attempt_delete;

CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_run_insert
BEFORE INSERT ON wallet_balance_acquisition_runs
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_networks n
    JOIN wallet_assets a ON a.asset_id = NEW.asset_id
    JOIN wallet_addresses d ON d.address_id = NEW.address_id
    JOIN audit_records audit ON audit.audit_id = NEW.created_audit_id
    WHERE n.network_id = NEW.network_id
      AND n.subject_id = NEW.subject_id
      AND a.subject_id = NEW.subject_id AND a.network_id = NEW.network_id
      AND d.subject_id = NEW.subject_id AND d.network_id = NEW.network_id
      AND audit.subject_id = NEW.subject_id
      AND audit.action = 'wallet_balance_acquisition_queued'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition run reference mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_run_identity_update
BEFORE UPDATE OF run_id, subject_id, network_id, asset_id, address_id,
                 idempotency_key, max_attempts, created_audit_id, created_at
ON wallet_balance_acquisition_runs
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition run identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_terminal_update
BEFORE UPDATE ON wallet_balance_acquisition_runs
WHEN OLD.status IN ('succeeded', 'failed', 'cancelled')
  OR (OLD.status = 'unknown' AND NOT (
      NEW.status = 'queued'
      AND NEW.attempt_count = OLD.attempt_count
      AND NEW.max_attempts = OLD.max_attempts
      AND NEW.next_attempt_at IS NOT NULL
      AND NEW.last_error_code IS NULL
      AND NEW.snapshot_id IS NULL
      AND NEW.claim_token IS NULL
      AND NEW.lease_owner IS NULL
      AND NEW.lease_expires_at IS NULL
      AND NEW.started_at IS OLD.started_at
      AND NEW.completed_at IS NULL
      AND NEW.state_hash <> OLD.state_hash
  ))
BEGIN
    SELECT RAISE(ABORT, 'terminal wallet balance acquisition runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_run_transition
BEFORE UPDATE OF status ON wallet_balance_acquisition_runs
WHEN NOT (
    (OLD.status IN ('queued', 'retry_wait') AND NEW.status = 'running')
    OR (OLD.status IN ('queued', 'retry_wait') AND NEW.status = 'cancelled')
    OR (OLD.status = 'running' AND NEW.status IN (
        'retry_wait', 'succeeded', 'failed', 'unknown'
    ))
    OR (OLD.status = 'unknown' AND NEW.status = 'queued')
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition run transition is invalid');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_attempt_increment
BEFORE UPDATE OF attempt_count ON wallet_balance_acquisition_runs
WHEN NEW.attempt_count != OLD.attempt_count AND NOT (
    OLD.status IN ('queued', 'retry_wait')
    AND NEW.status = 'running'
    AND NEW.attempt_count = OLD.attempt_count + 1
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempt count is invalid');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_run_completion
BEFORE UPDATE ON wallet_balance_acquisition_runs
WHEN NEW.status IN ('retry_wait', 'succeeded', 'failed', 'unknown') AND NOT EXISTS (
    SELECT 1 FROM wallet_balance_acquisition_attempts attempt
    WHERE attempt.run_id = NEW.run_id
      AND attempt.subject_id = NEW.subject_id
      AND attempt.attempt_number = NEW.attempt_count
      AND (
          (NEW.status = 'retry_wait' AND attempt.status = 'failed')
          OR (NEW.status != 'retry_wait' AND attempt.status = NEW.status)
      )
      AND attempt.error_code IS NEW.last_error_code
      AND attempt.snapshot_id IS NEW.snapshot_id
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition completion evidence is missing');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_run_delete
BEFORE DELETE ON wallet_balance_acquisition_runs
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition runs cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_attempt_insert
BEFORE INSERT ON wallet_balance_acquisition_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_balance_acquisition_runs run
    WHERE run.run_id = NEW.run_id
      AND run.subject_id = NEW.subject_id
      AND run.network_id = NEW.network_id
      AND run.asset_id = NEW.asset_id
      AND run.address_id = NEW.address_id
      AND run.status = 'running'
      AND run.attempt_count = NEW.attempt_number
      AND run.claim_token = NEW.claim_token
      AND run.lease_owner = NEW.lease_owner
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempt reference mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_attempt_identity_update
BEFORE UPDATE OF attempt_id, run_id, subject_id, network_id, asset_id, address_id,
                 attempt_number, reserved_request_count, claim_token, lease_owner, started_at
ON wallet_balance_acquisition_attempts
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempt identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_attempt_transition
BEFORE UPDATE ON wallet_balance_acquisition_attempts
WHEN NOT (
    OLD.status = 'executing'
    AND NEW.status IN ('succeeded', 'failed', 'unknown')
    AND NEW.state_hash <> OLD.state_hash
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempt transition is invalid');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_attempt_snapshot
BEFORE UPDATE ON wallet_balance_acquisition_attempts
WHEN NEW.status = 'succeeded' AND NOT EXISTS (
    SELECT 1 FROM wallet_balance_snapshots snapshot
    WHERE snapshot.snapshot_id = NEW.snapshot_id
      AND snapshot.subject_id = NEW.subject_id
      AND snapshot.network_id = NEW.network_id
      AND snapshot.asset_id = NEW.asset_id
      AND snapshot.address_id = NEW.address_id
      AND snapshot.source = 'evm_rpc'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition snapshot mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_attempt_delete
BEFORE DELETE ON wallet_balance_acquisition_attempts
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempts cannot be deleted');
END;
""",
    56: """
CREATE TABLE IF NOT EXISTS wallet_payment_executions (
    execution_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    order_id TEXT NOT NULL UNIQUE REFERENCES wallet_payment_orders(order_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    source_address TEXT NOT NULL CHECK(length(source_address)=42 AND substr(source_address,1,2)='0x'),
    recipient_address TEXT NOT NULL CHECK(length(recipient_address)=42 AND substr(recipient_address,1,2)='0x'),
    asset_type TEXT NOT NULL CHECK(asset_type IN ('native','token')),
    contract_address TEXT,
    amount TEXT NOT NULL CHECK(length(amount) BETWEEN 1 AND 256 AND amount NOT GLOB '*[^0-9]*' AND (amount='0' OR amount NOT GLOB '0*')),
    chain_id INTEGER NOT NULL CHECK(chain_id > 0),
    nonce INTEGER NOT NULL CHECK(nonce >= 0),
    gas_limit INTEGER NOT NULL CHECK(gas_limit BETWEEN 21000 AND 30000000),
    max_fee_per_gas TEXT NOT NULL CHECK(length(max_fee_per_gas) BETWEEN 1 AND 256 AND max_fee_per_gas NOT GLOB '*[^0-9]*' AND max_fee_per_gas NOT GLOB '0*'),
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    signer_id TEXT NOT NULL CHECK(length(trim(signer_id)) BETWEEN 1 AND 256),
    status TEXT NOT NULL CHECK(status IN ('signing','broadcast','unknown','confirmed','failed')),
    tx_hash TEXT,
    error_code TEXT,
    receipt_status INTEGER CHECK(receipt_status IN (0,1)),
    receipt_block_number INTEGER CHECK(receipt_block_number IS NULL OR receipt_block_number >= 0),
    receipt_block_hash TEXT,
    receipt_confirmations INTEGER CHECK(receipt_confirmations IS NULL OR receipt_confirmations >= 0),
    receipt_effect_hash TEXT,
    attempt_count INTEGER NOT NULL CHECK(attempt_count >= 1),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    last_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_wallet_payment_executions_status
    ON wallet_payment_executions(subject_id,status,created_at,execution_id);
CREATE INDEX IF NOT EXISTS idx_wallet_payment_executions_history
    ON wallet_payment_executions(subject_id,created_at DESC,execution_id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_payment_execution_nonce
    ON wallet_payment_executions(subject_id,network_id,source_address,nonce)
    WHERE status IN ('signing','broadcast','unknown','confirmed') OR tx_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_payment_execution_tx_hash
    ON wallet_payment_executions(subject_id,network_id,tx_hash) WHERE tx_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS wallet_payment_execution_attempts (
    attempt_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES wallet_payment_executions(execution_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
    request_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('signing','broadcast','unknown','confirmed','failed')),
    tx_hash TEXT,
    error_code TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    UNIQUE(execution_id,attempt_number),
    UNIQUE(subject_id,request_id)
);
CREATE TRIGGER IF NOT EXISTS validate_wallet_execution_insert
BEFORE INSERT ON wallet_payment_executions
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_payment_orders order_row
    WHERE order_row.order_id = NEW.order_id
      AND order_row.subject_id = NEW.subject_id
      AND order_row.network_id = NEW.network_id
      AND order_row.asset_id = NEW.asset_id
      AND order_row.recipient_address = NEW.recipient_address
      AND order_row.amount = NEW.amount
      AND order_row.status = 'signing'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet payment execution reference mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_execution_attempt_insert
BEFORE INSERT ON wallet_payment_execution_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_payment_executions execution_row
    WHERE execution_row.execution_id = NEW.execution_id
      AND execution_row.subject_id = NEW.subject_id
      AND execution_row.status = 'signing'
      AND execution_row.attempt_count = NEW.attempt_number
)
BEGIN
    SELECT RAISE(ABORT, 'wallet payment execution attempt reference mismatch');
END;
CREATE INDEX IF NOT EXISTS idx_wallet_payment_execution_attempts
    ON wallet_payment_execution_attempts(subject_id,execution_id,attempt_number);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_execution_delete
BEFORE DELETE ON wallet_payment_executions BEGIN SELECT RAISE(ABORT,'wallet payment executions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_execution_attempt_delete
BEFORE DELETE ON wallet_payment_execution_attempts BEGIN SELECT RAISE(ABORT,'wallet payment execution attempts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_execution_identity_update
BEFORE UPDATE OF execution_id,subject_id,order_id,network_id,asset_id,source_address,recipient_address,
                 asset_type,contract_address,amount,chain_id,nonce,gas_limit,max_fee_per_gas,
                 request_id,request_hash,signer_id,created_audit_id,created_at
ON wallet_payment_executions BEGIN SELECT RAISE(ABORT,'wallet payment execution identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_execution_attempt_identity_update
BEFORE UPDATE OF attempt_id,execution_id,subject_id,attempt_number,request_id,started_at,created_audit_id
ON wallet_payment_execution_attempts BEGIN SELECT RAISE(ABORT,'wallet payment execution attempt identity is immutable'); END;
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
    OR OLD.status = 'failed' AND NEW.status = 'refunded'
)
BEGIN SELECT RAISE(ABORT,'wallet payment order transition is invalid'); END;
""",
    55: """
CREATE TABLE IF NOT EXISTS wallet_bounties (
    bounty_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    project_id TEXT REFERENCES autonomous_projects(project_id),
    goal_id TEXT REFERENCES goals(goal_id),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 128),
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 240),
    description TEXT NOT NULL CHECK(length(trim(description)) BETWEEN 1 AND 20000),
    acceptance_criteria_json TEXT NOT NULL,
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    reward_amount TEXT NOT NULL CHECK(length(reward_amount) BETWEEN 1 AND 256 AND reward_amount NOT GLOB '*[^0-9]*' AND (reward_amount='0' OR reward_amount NOT GLOB '0*')),
    opens_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_submissions INTEGER NOT NULL CHECK(max_submissions BETWEEN 1 AND 10000),
    reward_slots INTEGER NOT NULL CHECK(reward_slots BETWEEN 1 AND 10000),
    status TEXT NOT NULL CHECK(status IN ('draft','published','closed','cancelled','expired')),
    state_hash TEXT NOT NULL,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    last_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,idempotency_key),
    CHECK(expires_at > opens_at)
);
CREATE INDEX IF NOT EXISTS idx_wallet_bounties_public ON wallet_bounties(subject_id,status,created_at DESC,bounty_id);
CREATE TABLE IF NOT EXISTS wallet_bounty_submissions (
    submission_id TEXT PRIMARY KEY,
    bounty_id TEXT NOT NULL REFERENCES wallet_bounties(bounty_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    counterparty TEXT NOT NULL CHECK(length(trim(counterparty)) BETWEEN 1 AND 256),
    content TEXT NOT NULL CHECK(length(trim(content)) BETWEEN 1 AND 20000),
    evidence_json TEXT NOT NULL,
    recipient_address TEXT NOT NULL CHECK(length(recipient_address)=42 AND substr(recipient_address,1,2)='0x'),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK(status IN ('submitted','accepted','rejected','withdrawn','expired')),
    decision_reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    state_hash TEXT NOT NULL,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    decision_audit_id TEXT REFERENCES audit_records(audit_id),
    UNIQUE(subject_id,bounty_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_wallet_bounty_submissions_bounty ON wallet_bounty_submissions(subject_id,bounty_id,status,created_at,submission_id);
CREATE TABLE IF NOT EXISTS wallet_payment_policies (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    mode TEXT NOT NULL CHECK(mode IN ('disabled','conditional_confirmation','automatic')),
    allowed_network_ids_json TEXT NOT NULL,
    allowed_asset_ids_json TEXT NOT NULL,
    per_order_limit TEXT NOT NULL,
    daily_limit TEXT NOT NULL,
    monthly_limit TEXT NOT NULL,
    daily_order_limit INTEGER NOT NULL CHECK(daily_order_limit >= 0),
    monthly_order_limit INTEGER NOT NULL CHECK(monthly_order_limit >= 0),
    min_balance TEXT NOT NULL,
    max_observation_age_seconds INTEGER NOT NULL CHECK(max_observation_age_seconds >= 0),
    automatic_max_amount TEXT NOT NULL,
    anomaly_block INTEGER NOT NULL CHECK(anomaly_block IN (0,1)),
    emergency_paused INTEGER NOT NULL CHECK(emergency_paused IN (0,1)),
    policy_version INTEGER NOT NULL CHECK(policy_version > 0),
    updated_at TEXT NOT NULL,
    state_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_payment_orders (
    order_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    bounty_id TEXT NOT NULL REFERENCES wallet_bounties(bounty_id),
    submission_id TEXT NOT NULL REFERENCES wallet_bounty_submissions(submission_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    recipient_address TEXT NOT NULL,
    amount TEXT NOT NULL CHECK(length(amount) BETWEEN 1 AND 256 AND amount NOT GLOB '*[^0-9]*' AND (amount='0' OR amount NOT GLOB '0*')),
    payment_mode TEXT NOT NULL CHECK(payment_mode IN ('disabled','conditional_confirmation','automatic')),
    policy_version INTEGER NOT NULL CHECK(policy_version > 0),
    idempotency_key TEXT NOT NULL,
    authorized_at TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    last_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    status TEXT NOT NULL CHECK(status IN ('pending_policy','awaiting_confirmation','reserved','rejected','cancelled','expired','signing','broadcast','unknown','confirmed','failed','refunded')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,submission_id), UNIQUE(subject_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_wallet_payment_orders_status ON wallet_payment_orders(subject_id,status,created_at,order_id);
CREATE TABLE IF NOT EXISTS wallet_ledger_journals (
    journal_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    order_id TEXT NOT NULL REFERENCES wallet_payment_orders(order_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    journal_type TEXT NOT NULL CHECK(journal_type IN ('reservation','release','settlement','refund')),
    amount TEXT NOT NULL CHECK(length(amount) BETWEEN 1 AND 256 AND amount NOT GLOB '*[^0-9]*' AND (amount='0' OR amount NOT GLOB '0*')),
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    UNIQUE(order_id,journal_type)
);
CREATE TABLE IF NOT EXISTS wallet_ledger_entries (
    entry_id TEXT PRIMARY KEY,
    journal_id TEXT NOT NULL REFERENCES wallet_ledger_journals(journal_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    order_id TEXT NOT NULL REFERENCES wallet_payment_orders(order_id),
    account TEXT NOT NULL CHECK(account IN ('available','reserved','paid','released')),
    direction TEXT NOT NULL CHECK(direction IN ('debit','credit')),
    amount TEXT NOT NULL CHECK(length(amount) BETWEEN 1 AND 256 AND amount NOT GLOB '*[^0-9]*' AND (amount='0' OR amount NOT GLOB '0*')),
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    UNIQUE(journal_id,account,direction)
);
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_subject ON wallet_ledger_journals(subject_id,created_at,journal_id);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_bounty_delete BEFORE DELETE ON wallet_bounties BEGIN SELECT RAISE(ABORT,'wallet bounties cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_submission_delete BEFORE DELETE ON wallet_bounty_submissions BEGIN SELECT RAISE(ABORT,'wallet submissions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_order_delete BEFORE DELETE ON wallet_payment_orders BEGIN SELECT RAISE(ABORT,'wallet payment orders cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_journal_update BEFORE UPDATE ON wallet_ledger_journals BEGIN SELECT RAISE(ABORT,'wallet ledger journals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_journal_delete BEFORE DELETE ON wallet_ledger_journals BEGIN SELECT RAISE(ABORT,'wallet ledger journals cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_entry_update BEFORE UPDATE ON wallet_ledger_entries BEGIN SELECT RAISE(ABORT,'wallet ledger entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_entry_delete BEFORE DELETE ON wallet_ledger_entries BEGIN SELECT RAISE(ABORT,'wallet ledger entries cannot be deleted'); END;
INSERT OR IGNORE INTO wallet_payment_policies(
    subject_id, mode, allowed_network_ids_json, allowed_asset_ids_json,
    per_order_limit, daily_limit, monthly_limit, daily_order_limit, monthly_order_limit,
    min_balance, max_observation_age_seconds, automatic_max_amount, anomaly_block,
    emergency_paused, policy_version, updated_at, state_hash
)
SELECT subject_id, 'disabled', '[]', '[]', '0', '0', '0', 0, 0, '0', 0, '0', 1, 0,
       1, updated_at, 'bootstrap'
FROM subject_identity;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_bounty_identity_update
BEFORE UPDATE OF bounty_id,subject_id,project_id,goal_id,idempotency_key,title,description,
                 acceptance_criteria_json,network_id,asset_id,reward_amount,opens_at,expires_at,
                 max_submissions,reward_slots,created_audit_id,created_at ON wallet_bounties
BEGIN SELECT RAISE(ABORT,'wallet bounty identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_bounty_transition
BEFORE UPDATE ON wallet_bounties
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash AND NEW.updated_at = OLD.updated_at
    OR OLD.status = 'draft' AND NEW.status IN ('published','cancelled','expired')
    OR OLD.status = 'published' AND NEW.status IN ('closed','cancelled','expired')
    OR OLD.status = 'closed' AND NEW.status = 'expired'
)
BEGIN SELECT RAISE(ABORT,'wallet bounty transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_submission_identity_update
BEFORE UPDATE OF submission_id,bounty_id,subject_id,counterparty,content,evidence_json,
                 recipient_address,idempotency_key,created_at,created_audit_id ON wallet_bounty_submissions
BEGIN SELECT RAISE(ABORT,'wallet submission identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_submission_transition
BEFORE UPDATE ON wallet_bounty_submissions
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash
    OR OLD.status = 'submitted' AND NEW.status IN ('accepted','rejected','withdrawn','expired')
)
BEGIN SELECT RAISE(ABORT,'wallet submission transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_order_identity_update
BEFORE UPDATE OF order_id,subject_id,bounty_id,submission_id,network_id,asset_id,
                 recipient_address,amount,payment_mode,policy_version,idempotency_key,
                 created_audit_id,created_at ON wallet_payment_orders
BEGIN SELECT RAISE(ABORT,'wallet payment order identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_order_transition
BEFORE UPDATE ON wallet_payment_orders
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash AND NEW.updated_at = OLD.updated_at
    OR OLD.status = 'pending_policy' AND NEW.status IN ('awaiting_confirmation','reserved','rejected','cancelled','expired')
    OR OLD.status = 'awaiting_confirmation' AND NEW.status IN ('reserved','rejected','cancelled','expired')
    OR OLD.status = 'reserved' AND NEW.status IN ('cancelled','expired','signing')
    OR OLD.status = 'signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status = 'broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status = 'unknown' AND NEW.status IN ('broadcast','confirmed','failed','refunded')
    OR OLD.status = 'failed' AND NEW.status = 'refunded'
)
BEGIN SELECT RAISE(ABORT,'wallet payment order transition is invalid'); END;
""",
    57: """
CREATE TABLE IF NOT EXISTS wallet_reward_workflows (
    workflow_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    assistance_request_id TEXT NOT NULL UNIQUE REFERENCES autonomous_project_assistance_requests(request_id),
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    phase_id TEXT NOT NULL REFERENCES autonomous_project_phases(phase_id),
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    post_id TEXT NOT NULL UNIQUE REFERENCES public_posts(post_id),
    bounty_id TEXT NOT NULL UNIQUE REFERENCES wallet_bounties(bounty_id),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK(status IN ('awaiting_publication','open','closed','cancelled','manual_intervention')),
    manual_reason TEXT,
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    last_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,idempotency_key),
    CHECK((status='manual_intervention') = (manual_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_wallet_reward_workflows_status ON wallet_reward_workflows(subject_id,status,created_at DESC,workflow_id DESC);
CREATE TABLE IF NOT EXISTS wallet_reward_submission_links (
    submission_id TEXT PRIMARY KEY REFERENCES wallet_bounty_submissions(submission_id),
    workflow_id TEXT NOT NULL REFERENCES wallet_reward_workflows(workflow_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    source_type TEXT NOT NULL CHECK(source_type IN ('public_form','inbound_event')),
    inbound_event_id TEXT REFERENCES interaction_inbound_events(event_id),
    interaction_id TEXT REFERENCES interactions(interaction_id),
    claimed_network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    source_content_hash TEXT NOT NULL CHECK(length(source_content_hash)=64 AND source_content_hash NOT GLOB '*[^0-9a-f]*'),
    verification_status TEXT NOT NULL CHECK(verification_status IN ('pending_verification','accepted','rejected','manual_intervention')),
    criteria_results_json TEXT,
    verification_reason TEXT,
    verified_by TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    decision_audit_id TEXT REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,inbound_event_id),
    CHECK((source_type='public_form' AND inbound_event_id IS NULL AND interaction_id IS NULL) OR (source_type='inbound_event' AND inbound_event_id IS NOT NULL AND interaction_id IS NOT NULL)),
    CHECK((verification_status='pending_verification' AND criteria_results_json IS NULL AND verification_reason IS NULL AND verified_by IS NULL AND decision_audit_id IS NULL) OR (verification_status!='pending_verification' AND criteria_results_json IS NOT NULL AND verification_reason IS NOT NULL AND verified_by IS NOT NULL AND decision_audit_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_wallet_reward_submission_links_status ON wallet_reward_submission_links(subject_id,workflow_id,verification_status,created_at,submission_id);
CREATE TABLE IF NOT EXISTS wallet_reward_incidents (
    incident_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    workflow_id TEXT NOT NULL REFERENCES wallet_reward_workflows(workflow_id),
    submission_id TEXT REFERENCES wallet_bounty_submissions(submission_id),
    execution_id TEXT REFERENCES wallet_payment_executions(execution_id),
    kind TEXT NOT NULL CHECK(kind IN ('source_mismatch','evidence_mismatch','payment_unknown','chain_reorganization','recovery_mismatch')),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 256),
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
    resolution TEXT,
    opened_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    resolved_audit_id TEXT REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,idempotency_key),
    CHECK((status='open' AND resolution IS NULL AND resolved_audit_id IS NULL) OR (status='resolved' AND resolution IS NOT NULL AND resolved_audit_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_wallet_reward_incidents_open ON wallet_reward_incidents(subject_id,status,created_at,incident_id);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_workflow_delete BEFORE DELETE ON wallet_reward_workflows BEGIN SELECT RAISE(ABORT,'wallet reward workflows cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_submission_link_delete BEFORE DELETE ON wallet_reward_submission_links BEGIN SELECT RAISE(ABORT,'wallet reward submission links cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_incident_delete BEFORE DELETE ON wallet_reward_incidents BEGIN SELECT RAISE(ABORT,'wallet reward incidents cannot be deleted'); END;
""",
    58: """
-- Expand the bounded reward incident taxonomy while preserving existing rows.
-- SQLite cannot alter a CHECK constraint in place, so rebuild this append-only
-- table and retain all provenance, audit, and state-hash values verbatim.
DROP TRIGGER IF EXISTS prevent_wallet_reward_incident_delete;
CREATE TABLE wallet_reward_incidents_v58 (
    incident_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    workflow_id TEXT NOT NULL REFERENCES wallet_reward_workflows(workflow_id),
    submission_id TEXT REFERENCES wallet_bounty_submissions(submission_id),
    execution_id TEXT REFERENCES wallet_payment_executions(execution_id),
    kind TEXT NOT NULL CHECK(kind IN (
        'source_mismatch','evidence_mismatch','payment_unknown',
        'chain_reorganization','recovery_mismatch','signer_rejection',
        'broadcast_unknown','receipt_chain_unknown'
    )),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 256),
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
    resolution TEXT,
    opened_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    resolved_audit_id TEXT REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,idempotency_key),
    CHECK((status='open' AND resolution IS NULL AND resolved_audit_id IS NULL) OR (status='resolved' AND resolution IS NOT NULL AND resolved_audit_id IS NOT NULL))
);
INSERT INTO wallet_reward_incidents_v58(
    incident_id,subject_id,workflow_id,submission_id,execution_id,kind,
    idempotency_key,status,reason,resolution,opened_audit_id,resolved_audit_id,
    state_hash,created_at,updated_at
)
SELECT incident_id,subject_id,workflow_id,submission_id,execution_id,kind,
       idempotency_key,status,reason,resolution,opened_audit_id,resolved_audit_id,
       state_hash,created_at,updated_at
FROM wallet_reward_incidents;
DROP TABLE wallet_reward_incidents;
ALTER TABLE wallet_reward_incidents_v58 RENAME TO wallet_reward_incidents;
CREATE INDEX IF NOT EXISTS idx_wallet_reward_incidents_open
    ON wallet_reward_incidents(subject_id,status,created_at,incident_id);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_incident_delete
BEFORE DELETE ON wallet_reward_incidents BEGIN
    SELECT RAISE(ABORT,'wallet reward incidents cannot be deleted');
END;
""",
    59: """
-- Submission consent is immutable provenance and participates in the durable
-- state hash. The column addition and hash backfill run in the Python hook so
-- replaying an interrupted migration remains idempotent.
DROP TRIGGER IF EXISTS prevent_wallet_submission_identity_update;
DROP TRIGGER IF EXISTS validate_wallet_submission_transition;
CREATE TRIGGER prevent_wallet_submission_identity_update
BEFORE UPDATE OF submission_id,bounty_id,subject_id,counterparty,content,evidence_json,
                 recipient_address,idempotency_key,consent_version,created_at,created_audit_id
ON wallet_bounty_submissions
BEGIN SELECT RAISE(ABORT,'wallet submission identity is immutable'); END;
CREATE TRIGGER validate_wallet_submission_transition
BEFORE UPDATE ON wallet_bounty_submissions
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash
    OR OLD.status = 'submitted' AND NEW.status IN ('accepted','rejected','withdrawn','expired')
)
BEGIN SELECT RAISE(ABORT,'wallet submission transition is invalid'); END;
""",
    60: """
-- Wallet payment policy hashes now cover every durable policy field. The
-- Python migration hook validates legacy hashes before backfilling the new
-- complete hash so a damaged row is never silently trusted.
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_entries_subject
    ON wallet_ledger_entries(subject_id, journal_id, entry_id);
    CREATE INDEX IF NOT EXISTS idx_wallet_ledger_entries_journal_entry
    ON wallet_ledger_entries(journal_id, entry_id);
""",
    61: """
-- Payment order/execution chronology is backfilled by the Python migration
-- hook after validating the schema-60 hashes.  Keep this DDL idempotent.
DROP TRIGGER IF EXISTS prevent_wallet_order_identity_update;
CREATE TRIGGER prevent_wallet_order_identity_update
BEFORE UPDATE OF order_id,subject_id,bounty_id,submission_id,network_id,asset_id,
                 recipient_address,amount,payment_mode,policy_version,idempotency_key,
                 created_audit_id,created_at ON wallet_payment_orders
BEGIN SELECT RAISE(ABORT,'wallet payment order identity is immutable'); END;
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
    OR OLD.status = 'failed' AND NEW.status = 'refunded'
)
BEGIN SELECT RAISE(ABORT,'wallet payment order transition is invalid'); END;
""",
    62: """
-- Receipt evidence is durable: a block number alone cannot detect a same
-- height reorganization or prove an ERC-20 transfer effect.
DROP INDEX IF EXISTS uq_wallet_payment_execution_nonce;
CREATE UNIQUE INDEX uq_wallet_payment_execution_nonce
    ON wallet_payment_executions(subject_id,network_id,source_address,nonce)
    WHERE status IN ('signing','broadcast','unknown','confirmed') OR tx_hash IS NOT NULL;
DROP TRIGGER IF EXISTS validate_wallet_execution_transition;
CREATE TRIGGER validate_wallet_execution_transition
BEFORE UPDATE ON wallet_payment_executions
WHEN NOT (
    NEW.status IS OLD.status
        AND NEW.tx_hash IS OLD.tx_hash
        AND NEW.error_code IS OLD.error_code
        AND NEW.receipt_status IS OLD.receipt_status
        AND NEW.receipt_block_number IS OLD.receipt_block_number
        AND NEW.receipt_block_hash IS OLD.receipt_block_hash
        AND NEW.receipt_confirmations IS OLD.receipt_confirmations
        AND NEW.receipt_effect_hash IS OLD.receipt_effect_hash
        AND NEW.attempt_count IS OLD.attempt_count
        AND NEW.state_hash IS OLD.state_hash
        AND NEW.last_audit_id IS OLD.last_audit_id
        AND NEW.updated_at IS OLD.updated_at
    OR OLD.status='signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status='broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status='unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed')
)
BEGIN SELECT RAISE(ABORT,'wallet payment execution transition is invalid'); END;
""",
}


def action_state_hash(row: sqlite3.Row) -> str:
    """Hash the immutable action state used by action revision records."""
    return content_hash(
        {
            "action_id": row["action_id"],
            "subject_id": row["subject_id"],
            "goal_id": row["goal_id"],
            "project_id": row["project_id"],
            "phase_id": row["phase_id"],
            "strategy_id": row["strategy_id"],
            "action_type": row["action_type"],
            "tool": row["tool"],
            "target": row["target"],
            "input_hash": row["input_hash"],
            "idempotency_key": row["idempotency_key"],
            "expected_outcome": row["expected_outcome"],
            "side_effect": bool(row["side_effect"]),
            "status": row["status"],
            "retry_count": int(row["retry_count"]),
            "resource_cost_json": row["resource_cost_json"],
            "result_json": row["result_json"],
            "prepared_at": row["prepared_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }
    )


def public_post_identity_hash(row: Any) -> str:
    """Hash the immutable public-post envelope, excluding moderation state."""
    return content_hash(
        {
            "post_id": row["post_id"],
            "subject_id": row["subject_id"],
            "kind": row["kind"],
            "title": row["title"],
            "content_hash": row["content_hash"],
            "author_label": row["author_label"],
            "author_provenance": row["author_provenance"],
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
        }
    )


def behavior_log_state_hash(row: Any) -> str:
    """Hash one immutable behavior-log revision payload."""
    return content_hash(
        {
            "log_id": row["log_id"],
            "action_id": row["action_id"],
            "subject_id": row["subject_id"],
            "revision_number": int(row["revision_number"]),
            "occurred_at": row["occurred_at"],
            "action_type": row["action_type"],
            "public_goal_reference": row["public_goal_reference"],
            "tool": row["tool"],
            "public_target": row["public_target"],
            "result_status": row["result_status"],
            "side_effect_summary": row["side_effect_summary"],
            "resource_summary": row["resource_summary"],
            "public_explanation": row["public_explanation"],
            "redaction_reason": row["redaction_reason"],
            "reason": row["reason"],
        }
    )


def wallet_submission_state_hash(
    *,
    submission_id: str,
    bounty_id: str,
    subject_id: str,
    counterparty: str,
    content: str,
    evidence_json: str,
    recipient_address: str,
    idempotency_key: str,
    consent_version: int,
    status: str,
    decision_reason: str | None,
) -> str:
    """Hash the complete durable bounty-submission state."""
    return content_hash(
        {
            "submission_id": submission_id,
            "bounty_id": bounty_id,
            "subject_id": subject_id,
            "counterparty": counterparty,
            "content": content,
            "evidence_json": evidence_json,
            "recipient_address": recipient_address,
            "idempotency_key": idempotency_key,
            "consent_version": consent_version,
            "status": status,
            "decision_reason": decision_reason,
        }
    )


def wallet_payment_policy_state_hash(
    *,
    subject_id: str,
    mode: str,
    allowed_network_ids_json: str,
    allowed_asset_ids_json: str,
    per_order_limit: str,
    daily_limit: str,
    monthly_limit: str,
    daily_order_limit: int,
    monthly_order_limit: int,
    min_balance: str,
    max_observation_age_seconds: int,
    automatic_max_amount: str,
    anomaly_block: int | bool,
    emergency_paused: int | bool,
    policy_version: int,
    updated_at: str,
) -> str:
    """Hash the complete durable wallet payment policy state."""
    validate_wallet_timestamp(updated_at)
    return content_hash(
        {
            "subject_id": subject_id,
            "mode": mode,
            "allowed_network_ids_json": allowed_network_ids_json,
            "allowed_asset_ids_json": allowed_asset_ids_json,
            "per_order_limit": per_order_limit,
            "daily_limit": daily_limit,
            "monthly_limit": monthly_limit,
            "daily_order_limit": strict_int(daily_order_limit),
            "monthly_order_limit": strict_int(monthly_order_limit),
            "min_balance": min_balance,
            "max_observation_age_seconds": strict_int(max_observation_age_seconds),
            "automatic_max_amount": automatic_max_amount,
            "anomaly_block": int(anomaly_block),
            "emergency_paused": int(emergency_paused),
            "policy_version": strict_int(policy_version),
            "updated_at": updated_at,
        }
    )


class Database:
    """Small SQLite boundary with WAL and explicit transactions."""

    _SNAPSHOT_STALE_AFTER_SECONDS = 60 * 60

    def __init__(
        self,
        path: Path | str,
        *,
        initialize: bool = True,
        read_only: bool = False,
    ):
        self.path = Path(path).resolve()
        self.read_only = read_only
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.initialize()
        elif not self.path.is_file():
            raise FileNotFoundError(self.path)

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro&immutable=1",
                timeout=30,
                check_same_thread=False,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # REPLACE conflict resolution performs an implicit DELETE.  Recursive
        # triggers must be enabled so append-only evidence guards also cover
        # that implicit path instead of allowing a tampered intent row to be
        # replaced without firing its DELETE trigger.
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.read_only:
            connection.execute("PRAGMA query_only = ON")
        return connection

    def promote_to_writable(self) -> None:
        """Switch a preflight read-only facade to the owned database boundary."""
        self.read_only = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with current_commit_scope():
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
        """Execute a SQL script without breaking the caller's transaction.

        ``Connection.executescript`` commits any transaction that is already
        open before running the script.  Database initialization and feature
        repair both rely on all DDL and backfills being atomic, so split the
        script at complete SQLite statements and execute each one through the
        normal connection API instead.
        """
        statement: list[str] = []
        for character in script:
            statement.append(character)
            if character != ";":
                continue
            candidate = "".join(statement)
            if not sqlite3.complete_statement(candidate):
                continue
            sql = candidate.strip()
            if sql:
                connection.execute(sql)
            statement.clear()

        remainder = "".join(statement).strip()
        if not remainder:
            return
        if not sqlite3.complete_statement(remainder):
            raise sqlite3.OperationalError("incomplete SQL script")
        connection.execute(remainder)

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Hold one WAL snapshot across a multi-query integrity check."""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(
        self, *, checkpoint: Callable[[], None] | None = None
    ) -> Iterator[sqlite3.Connection]:
        """Yield an isolated SQLite backup for long-running read-only work.

        A normal ``read_transaction`` keeps a WAL snapshot open until the
        caller finishes serialising its result.  That is appropriate for a
        short integrity check, but a multi-gigabyte export can otherwise hold
        the WAL checkpoint hostage for the entire duration of compression.
        The backup API holds the live connection only while copying the
        database, then all subsequent reads happen against a private,
        same-volume temporary image.  Callers must not write through the
        returned connection; any audit/publication writes stay on the live
        :class:`Database` instance.
        """
        self._cleanup_stale_snapshots()
        snapshot_path = self.path.with_name(f".{self.path.name}.{new_id('snapshot')}.sqlite")
        snapshot_lock_path = snapshot_path.with_name(snapshot_path.name + ".lock")
        snapshot_lock = ProcessLock(snapshot_lock_path)
        lock_held = False
        source: sqlite3.Connection | None = None
        snapshot: sqlite3.Connection | None = None
        try:
            if checkpoint is not None:
                checkpoint()
            snapshot_lock.acquire()
            lock_held = True
            source = self._connect()
            snapshot = sqlite3.connect(snapshot_path, timeout=30, check_same_thread=False)
            snapshot.row_factory = sqlite3.Row
            snapshot.execute("PRAGMA foreign_keys = ON")
            snapshot.execute("PRAGMA busy_timeout = 30000")
            if checkpoint is None:
                source.backup(snapshot)
            else:
                source.backup(
                    snapshot,
                    pages=64,
                    progress=lambda _status, _remaining, _total: checkpoint(),
                )
                checkpoint()
            snapshot.commit()
            source.close()
            source = None
            snapshot.execute("PRAGMA query_only = ON")
            yield snapshot
        finally:
            if snapshot is not None:
                snapshot.close()
            if source is not None:
                source.close()
            if lock_held:
                snapshot_lock.release()
            cleanup_paths = [
                snapshot_path,
                snapshot_path.with_name(snapshot_path.name + "-wal"),
                snapshot_path.with_name(snapshot_path.name + "-shm"),
            ]
            if lock_held:
                cleanup_paths.append(snapshot_lock_path)
            for cleanup_path in cleanup_paths:
                with suppress(OSError):
                    cleanup_path.unlink(missing_ok=True)

    def _cleanup_stale_snapshots(self) -> int:
        """Remove abandoned, unlocked backup images left by a crashed export.

        Snapshot images are derived data, but they can be as large as the live
        database.  Only files older than the grace period are considered, and
        an exclusive probe protects an in-progress export from being removed.
        A failed probe is retained for a later maintenance pass.
        """
        pattern = f".{self.path.name}.snapshot_*.sqlite"
        cutoff = time.time() - self._SNAPSHOT_STALE_AFTER_SECONDS
        removed = 0
        for candidate in self.path.parent.glob(pattern):
            try:
                if not candidate.is_file() or candidate.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            if not self._snapshot_is_idle(candidate):
                continue
            try:
                candidate.unlink()
                for suffix in ("-wal", "-shm"):
                    candidate.with_name(candidate.name + suffix).unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _snapshot_is_idle(path: Path) -> bool:
        """Return whether an old snapshot can be acquired exclusively."""
        lock_path = path.with_name(path.name + ".lock")
        lock = ProcessLock(lock_path)
        try:
            lock.acquire()
        except (RuntimeOwnershipError, OSError):
            return False
        try:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(path, timeout=0.05)
                connection.execute("PRAGMA busy_timeout = 50")
                connection.execute("BEGIN EXCLUSIVE")
                connection.rollback()
                return True
            except sqlite3.OperationalError as error:
                return not any(token in str(error).casefold() for token in ("locked", "busy"))
            except sqlite3.DatabaseError:
                # A malformed orphan is still derived data and has no live reader
                # that can be harmed by cleanup.
                return True
            finally:
                if connection is not None:
                    connection.close()
        finally:
            lock.release()
            with suppress(OSError):
                lock_path.unlink(missing_ok=True)

    def initialize(self, *, wallet_legacy_approval: WalletLegacyApproval | None = None) -> None:
        version = self._preflight_schema_version()
        # Schema 60 is also a legitimate starting point for the chronology
        # hardening migration.  It contains the complete policy/ledger hash
        # backfill, but payment order/execution timestamps are still on the
        # legacy hash contract and therefore require the same fingerprint-
        # bound approval before they can be upgraded.
        if wallet_legacy_approval is not None and version not in {59, 60}:
            raise RuntimeError("explicit wallet upgrade approval requires schema 59 or 60")
        backup_path = (
            None
            if version is None or version >= CURRENT_SCHEMA_VERSION
            else self._create_migration_backup(version)
        )
        try:
            with self.connection() as connection:
                if version is not None and version < CURRENT_SCHEMA_VERSION:
                    connection.execute("PRAGMA journal_mode = DELETE")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._execute_sql_script(connection, SCHEMA_SQL)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            self._migrate(wallet_legacy_approval=wallet_legacy_approval)
            self._ensure_optional_features()
            self._ensure_training_policies()
            with self.connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.commit()
        except Exception:
            if backup_path is not None:
                try:
                    self._restore_migration_backup(backup_path)
                except Exception as restore_error:
                    raise RuntimeError(
                        "database migration failed and pre-migration restore failed"
                    ) from restore_error
            raise
        # A pre-migration image is rollback state, not retained history.
        # Cleanup happens only after the complete initialization path has
        # succeeded and, critically, outside the migration rollback handler.
        # If cleanup itself fails after deleting one image, the upgraded
        # database remains authoritative instead of attempting an impossible
        # restore from a backup that may no longer exist.
        self._cleanup_migration_backups()

    def _cleanup_migration_backups(self) -> int:
        """Best-effort removal of rollback images after successful startup."""
        removed = 0
        pattern = f"{self.path.name}.pre-migration-v*.bak"
        for candidate in self.path.parent.glob(pattern):
            try:
                metadata = candidate.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                candidate.unlink(missing_ok=True)
            except OSError:
                # Keep an operator-visible rollback image for manual cleanup;
                # migration has already completed and must remain authoritative.
                continue
            removed += 1
        return removed

    @staticmethod
    def _ensure_archive_transfer_claim_columns(connection: sqlite3.Connection) -> None:
        """Add v43 queue fencing columns idempotently for repaired markers."""
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(archive_transfer_queue)")
        }
        for name in ("claim_token", "lease_owner", "lease_expires_at"):
            if name not in columns:
                connection.execute(f"ALTER TABLE archive_transfer_queue ADD COLUMN {name} TEXT")

    @staticmethod
    def _ensure_transport_endpoint_columns(connection: sqlite3.Connection) -> None:
        """Add endpoint integrity metadata without assuming a pristine v49 row."""

        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(interaction_transports)")
        }
        if "endpoint_contract" not in columns:
            connection.execute(
                "ALTER TABLE interaction_transports ADD COLUMN endpoint_contract TEXT "
                "NOT NULL DEFAULT 'legacy_origin' "
                "CHECK (endpoint_contract IN ('legacy_origin', 'digest_v1'))"
            )
        if "endpoint_digest" not in columns:
            connection.execute("ALTER TABLE interaction_transports ADD COLUMN endpoint_digest TEXT")

    @staticmethod
    def _ensure_inbound_reply_columns(connection: sqlite3.Connection) -> None:
        """Add provider-specific reply routing metadata idempotently."""

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(interaction_inbound_events)")
        }
        if "external_thread_id" not in columns:
            connection.execute(
                "ALTER TABLE interaction_inbound_events ADD COLUMN external_thread_id TEXT"
            )
        if "reply_selector" not in columns:
            connection.execute(
                "ALTER TABLE interaction_inbound_events ADD COLUMN reply_selector TEXT "
                "CHECK (reply_selector IS NULL OR reply_selector IN "
                "('qq:user', 'qq:group', 'qq:channel', 'qq:dm', 'feishu:chat_id'))"
            )
        if "reply_context_version" not in columns:
            connection.execute(
                "ALTER TABLE interaction_inbound_events ADD COLUMN reply_context_version "
                "INTEGER NOT NULL DEFAULT 0 CHECK (reply_context_version IN (0, 1))"
            )

    @staticmethod
    def _ensure_public_post_evidence_columns(connection: sqlite3.Connection) -> None:
        """Add v52 evidence fields even when an operator replays an old marker."""

        moderation_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(public_post_moderation_events)")
        }
        if "revision" not in moderation_columns:
            connection.execute(
                "ALTER TABLE public_post_moderation_events ADD COLUMN revision INTEGER "
                "CHECK (revision IS NULL OR revision >= 1)"
            )
        if "previous_event_id" not in moderation_columns:
            connection.execute(
                "ALTER TABLE public_post_moderation_events ADD COLUMN previous_event_id TEXT"
            )
        if "idempotency_key" not in moderation_columns:
            connection.execute(
                "ALTER TABLE public_post_moderation_events ADD COLUMN idempotency_key TEXT"
            )

        post_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(public_posts)")
        }
        if "identity_hash" not in post_columns:
            connection.execute("ALTER TABLE public_posts ADD COLUMN identity_hash TEXT")
        if "author_provenance" not in post_columns:
            connection.execute(
                "ALTER TABLE public_posts ADD COLUMN author_provenance TEXT NOT NULL "
                "DEFAULT 'visitor' CHECK (author_provenance IN "
                "('visitor', 'subject', 'operator', 'verified_channel'))"
            )

        control_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(public_post_controls)")
        }
        if "storage_cap_bytes" not in control_columns:
            connection.execute(
                "ALTER TABLE public_post_controls ADD COLUMN storage_cap_bytes INTEGER "
                "NOT NULL DEFAULT 250000000 CHECK "
                "(storage_cap_bytes >= 1000000 AND storage_cap_bytes <= 2000000000)"
            )
        if "captcha_issue_limit_per_hour" not in control_columns:
            connection.execute(
                "ALTER TABLE public_post_controls ADD COLUMN captcha_issue_limit_per_hour "
                "INTEGER NOT NULL DEFAULT 30 CHECK "
                "(captcha_issue_limit_per_hour >= 1 AND captcha_issue_limit_per_hour <= 100000)"
            )
        if "captcha_global_rate_per_minute" not in control_columns:
            connection.execute(
                "ALTER TABLE public_post_controls ADD COLUMN captcha_global_rate_per_minute "
                "INTEGER NOT NULL DEFAULT 300 CHECK "
                "(captcha_global_rate_per_minute >= 1 AND captcha_global_rate_per_minute <= 100000)"
            )

    def _preflight_schema_version(self) -> int | None:
        """Read an existing schema marker before any current-schema DDL runs."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return None
        try:
            connection = sqlite3.connect(self.path)
            try:
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError as error:
            raise RuntimeError("database schema preflight failed") from error
        if row is None:
            raise RuntimeError("database schema version is missing")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as error:
            raise RuntimeError("database schema version is invalid") from error
        if version < 1:
            raise RuntimeError("database schema version is invalid")
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than runtime {CURRENT_SCHEMA_VERSION}"
            )
        return version

    def _create_migration_backup(self, version: int) -> Path:
        """Create and verify one durable rollback image before migration DDL."""
        backup_path = self.path.with_name(f"{self.path.name}.pre-migration-v{version}.bak")
        temporary_path = backup_path.with_name(f".{backup_path.name}.{new_id('tmp')}")
        source = sqlite3.connect(self.path)
        destination: sqlite3.Connection | None = None
        try:
            destination = sqlite3.connect(temporary_path)
            source.backup(destination)
            destination.commit()
            marker = destination.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            quick_check = str(destination.execute("PRAGMA quick_check").fetchone()[0])
            if marker is None or int(marker[0]) != version or quick_check != "ok":
                raise RuntimeError("pre-migration backup verification failed")
            destination.close()
            destination = None
            os.replace(temporary_path, backup_path)
            return backup_path
        except Exception:
            if destination is not None:
                destination.close()
                destination = None
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
            raise
        finally:
            if destination is not None:
                destination.close()
            source.close()

    def _restore_migration_backup(self, backup_path: Path) -> None:
        """Restore a verified pre-migration image without leaving WAL sidecars."""
        if not backup_path.is_file():
            raise FileNotFoundError(backup_path)
        temporary_path = self.path.with_name(f".{self.path.name}.{new_id('restore')}")
        try:
            shutil.copy2(backup_path, temporary_path)
            # The sqlite context manager commits/rolls back but does not close
            # the connection. Explicitly close it before replacing the target;
            # Windows otherwise keeps the temporary database handle locked.
            connection = sqlite3.connect(temporary_path)
            try:
                quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                if quick_check != "ok":
                    raise RuntimeError("migration restore verification failed")
            finally:
                connection.close()
            for suffix in ("-wal", "-shm"):
                temporary_path.with_name(temporary_path.name + suffix).unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _ensure_optional_features(self) -> None:
        """Install additive features without invalidating older schema markers."""
        with self.transaction() as connection:
            self._execute_sql_script(
                connection,
                """
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
""",
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(model_calls)").fetchall()
            }
            if "resource_group_id" not in columns:
                connection.execute("ALTER TABLE model_calls ADD COLUMN resource_group_id TEXT")
            if "request_json" not in columns:
                connection.execute("ALTER TABLE model_calls ADD COLUMN request_json TEXT")
            if "capture_policy_version" not in columns:
                connection.execute(
                    "ALTER TABLE model_calls ADD COLUMN capture_policy_version INTEGER"
                )
            event_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "payload_archive_key" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN payload_archive_key TEXT")
            if "payload_archived_at" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN payload_archived_at TEXT")
            segment_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(event_payload_segments)"
                ).fetchall()
            }
            if "archive_format" not in segment_columns:
                connection.execute(
                    "ALTER TABLE event_payload_segments ADD COLUMN archive_format TEXT"
                )
            if "encryption_key_id" not in segment_columns:
                connection.execute(
                    "ALTER TABLE event_payload_segments ADD COLUMN encryption_key_id TEXT"
                )
            if "encryption_key_fingerprint" not in segment_columns:
                connection.execute(
                    "ALTER TABLE event_payload_segments ADD COLUMN encryption_key_fingerprint TEXT"
                )
            observation_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(observations)").fetchall()
            }
            if "content_archive_key" not in observation_columns:
                connection.execute("ALTER TABLE observations ADD COLUMN content_archive_key TEXT")
            if "content_archived_at" not in observation_columns:
                connection.execute("ALTER TABLE observations ADD COLUMN content_archived_at TEXT")
            self._execute_sql_script(
                connection,
                """
CREATE TABLE IF NOT EXISTS observation_content_segments (
    segment_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    object_key TEXT NOT NULL,
    first_fetched_at TEXT NOT NULL,
    last_fetched_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL CHECK (observation_count > 0),
    compressed_hash TEXT NOT NULL,
    archive_format TEXT,
    encryption_key_id TEXT,
    encryption_key_fingerprint TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, object_key)
);
CREATE INDEX IF NOT EXISTS idx_observation_content_segments_subject_key
    ON observation_content_segments(subject_id, encryption_key_id, created_at);
""",
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_subject_group_status "
                "ON model_calls(subject_id, resource_group_id, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_subject_capture_policy "
                "ON model_calls(subject_id, capture_policy_version, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_subject_export_time "
                "ON model_calls(subject_id, created_at, call_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_payload_segments_subject_key "
                "ON event_payload_segments(subject_id, encryption_key_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_subject_archive_time "
                "ON events(subject_id, payload_archive_key, occurred_at, event_id)"
            )
            connection.execute(
                "INSERT INTO memory_fts(memory_id, subject_id, content) "
                "SELECT m.memory_id, m.subject_id, m.content FROM memories m "
                "WHERE m.status = 'active' AND NOT EXISTS ("
                "SELECT 1 FROM memory_fts f WHERE f.memory_id = m.memory_id)"
            )
            delivery_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(interaction_deliveries)"
                ).fetchall()
            }
            if "next_retry_at" not in delivery_columns:
                connection.execute(
                    "ALTER TABLE interaction_deliveries ADD COLUMN next_retry_at TEXT"
                )
            self._execute_sql_script(
                connection,
                """
DROP TRIGGER IF EXISTS prevent_event_immutable_update;
CREATE TRIGGER prevent_event_immutable_update
BEFORE UPDATE ON events
WHEN NEW.event_id != OLD.event_id
  OR NEW.subject_id != OLD.subject_id
  OR NEW.event_type != OLD.event_type
  OR NEW.source != OLD.source
  OR NEW.occurred_at != OLD.occurred_at
  OR NEW.observed_at != OLD.observed_at
  OR NEW.payload_hash != OLD.payload_hash
  OR NEW.privacy_level != OLD.privacy_level
  OR NEW.causal_parent_ids_json != OLD.causal_parent_ids_json
  OR (
      NEW.payload_json != OLD.payload_json
      AND NOT (
          OLD.payload_archive_key IS NULL
          AND NEW.payload_archive_key IS NOT NULL
          AND NEW.payload_json = '{}'
          AND NEW.payload_archived_at IS NOT NULL
      )
  )
  OR (
      OLD.payload_archive_key IS NOT NULL
      AND NOT (NEW.payload_archive_key IS OLD.payload_archive_key)
  )
  OR (
      OLD.payload_archived_at IS NOT NULL
      AND NOT (NEW.payload_archived_at IS OLD.payload_archived_at)
  )
BEGIN
    SELECT RAISE(ABORT, 'event evidence is append-only');
END;
""",
            )

            required_tables = {
                "interaction_transports",
                "interaction_deliveries",
                "interaction_bindings",
                "interaction_inbound_events",
                "interaction_threads",
                "event_chain_roots",
                "memory_fts",
                "action_revisions",
                "archive_keyring_revisions",
                "archive_object_replicas",
                "archive_object_replica_revisions",
                "behavior_log_revisions",
                "observation_content_segments",
                "secret_cleanup_queue",
                "secret_file_intents",
                "storage_usage_samples",
                "common_knowledge_versions",
                "common_knowledge_sync_events",
                "common_knowledge_peers",
                "common_knowledge_remote_events",
                "common_knowledge_sync_runs",
                "common_knowledge_evaluation_events",
                "wallet_networks",
                "wallet_network_revisions",
                "wallet_assets",
                "wallet_asset_revisions",
                "wallet_addresses",
                "wallet_address_revisions",
                "wallet_balance_snapshots",
                "wallet_balance_acquisition_runs",
                "wallet_balance_acquisition_attempts",
                "wallet_payment_executions",
                "wallet_payment_execution_attempts",
                "wallet_reward_workflows",
                "wallet_reward_submission_links",
                "wallet_reward_incidents",
            }
            installed_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                ).fetchall()
            }
            missing = sorted(required_tables - installed_tables)
            if missing:
                raise RuntimeError(
                    "database schema is incomplete; missing required features: "
                    + ", ".join(missing)
                )
            # Bind payment authorization to a durable timestamp.  This is an
            # additive repair for schema-60 databases; fresh schema-55
            # installs already include the column.
            order_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(wallet_payment_orders)")
            }
            if "authorized_at" not in order_columns:
                connection.execute(
                    "ALTER TABLE wallet_payment_orders ADD COLUMN authorized_at TEXT"
                )
            # This additive index keeps the unfiltered wallet history
            # projection on the subject/time keyset without rewriting the
            # already-published schema-53 migration.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_balance_snapshots_subject_history "
                "ON wallet_balance_snapshots(subject_id, observed_at DESC, snapshot_id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_ledger_entries_subject "
                "ON wallet_ledger_entries(subject_id, journal_id, entry_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_ledger_entries_journal_entry "
                "ON wallet_ledger_entries(journal_id, entry_id)"
            )
            self._ensure_wallet_acquisition_triggers(connection)
            self._ensure_wallet_execution_triggers(connection)
            schema_version = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
            if schema_version >= 59:
                self._ensure_wallet_submission_consent_contract(connection)
            self._ensure_behavior_log_revision_triggers(connection)
            self._assert_behavior_log_revisions_complete(connection)
            self._ensure_audit_record_triggers(connection)
            self._ensure_action_revisions(connection)
            self._ensure_subject_scoped_triggers(connection)
            self._ensure_wallet_reward_triggers(connection)
            self._ensure_inbound_triggers(connection)
            self._ensure_public_post_triggers(connection)

    @staticmethod
    def _ensure_subject_scoped_triggers(connection: sqlite3.Connection) -> None:
        links = (
            ("goal_governance_runs", "model_call_id", "model_calls", "call_id", False),
            ("goal_governance_runs", "focus_goal_id", "goals", "goal_id", True),
            ("action_deliberation_runs", "model_call_id", "model_calls", "call_id", False),
            ("action_deliberation_runs", "goal_id", "goals", "goal_id", True),
            ("action_deliberation_runs", "source_id", "world_sources", "source_id", True),
            ("action_deliberation_runs", "action_id", "actions", "action_id", True),
            ("action_deliberation_runs", "observation_id", "observations", "observation_id", True),
            ("research_search_runs", "planner_call_id", "model_calls", "call_id", False),
            ("research_search_runs", "goal_id", "goals", "goal_id", True),
            (
                "research_search_runs",
                "provider_config_id",
                "search_provider_configs",
                "config_id",
                True,
            ),
            ("research_search_runs", "project_id", "autonomous_projects", "project_id", True),
            (
                "research_search_runs",
                "phase_id",
                "autonomous_project_phases",
                "phase_id",
                True,
            ),
            ("epistemic_review_runs", "model_call_id", "model_calls", "call_id", False),
            (
                "epistemic_review_runs",
                "trigger_observation_id",
                "observations",
                "observation_id",
                True,
            ),
            ("memory_accesses", "memory_id", "memories", "memory_id", False),
            (
                "relationship_social_runs",
                "relationship_id",
                "relationships",
                "relationship_id",
                False,
            ),
            ("relationship_social_runs", "model_call_id", "model_calls", "call_id", False),
            ("relationship_social_runs", "interaction_id", "interactions", "interaction_id", True),
            ("self_models", "model_call_id", "model_calls", "call_id", False),
            ("thought_episodes", "agenda_id", "thought_agenda_items", "agenda_id", False),
            ("thought_episodes", "model_call_id", "model_calls", "call_id", False),
            ("thought_episodes", "created_goal_id", "goals", "goal_id", True),
            (
                "metacognitive_outcomes",
                "decision_id",
                "metacognitive_decisions",
                "decision_id",
                False,
            ),
            ("outcome_evaluations", "goal_id", "goals", "goal_id", False),
            ("outcome_evaluations", "evidence_event_id", "events", "event_id", False),
            ("outcome_evaluations", "observation_id", "observations", "observation_id", True),
            ("interaction_deliveries", "interaction_id", "interactions", "interaction_id", False),
            (
                "interaction_deliveries",
                "transport_id",
                "interaction_transports",
                "transport_id",
                False,
            ),
            ("autonomous_projects", "goal_id", "goals", "goal_id", False),
            ("autonomous_projects", "formation_call_id", "model_calls", "call_id", False),
            ("autonomous_projects", "source_mission_id", "mission_candidates", "mission_id", True),
            ("autonomous_project_phases", "project_id", "autonomous_projects", "project_id", False),
            (
                "autonomous_project_reviews",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            ("autonomous_project_reviews", "model_call_id", "model_calls", "call_id", False),
            (
                "autonomous_project_reviews",
                "phase_id",
                "autonomous_project_phases",
                "phase_id",
                True,
            ),
            (
                "autonomous_project_resource_uses",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            (
                "autonomous_project_assistance_requests",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            (
                "autonomous_project_assistance_requests",
                "phase_id",
                "autonomous_project_phases",
                "phase_id",
                True,
            ),
            (
                "autonomous_project_sleep_reflections",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            ("autonomous_project_sleep_reflections", "sleep_id", "sleep_runs", "sleep_id", False),
            (
                "autonomous_project_execution_clock_events",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            (
                "autonomous_project_executions",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            (
                "autonomous_project_executions",
                "phase_id",
                "autonomous_project_phases",
                "phase_id",
                False,
            ),
            (
                "self_modification_settings",
                "proposal_id",
                "self_modification_proposals",
                "proposal_id",
                False,
            ),
            (
                "self_modification_revisions",
                "proposal_id",
                "self_modification_proposals",
                "proposal_id",
                False,
            ),
            ("memory_embeddings", "memory_id", "memories", "memory_id", False),
            ("entity_relations", "source_entity_id", "entities", "entity_id", False),
            ("entity_relations", "target_entity_id", "entities", "entity_id", False),
            ("wallet_assets", "network_id", "wallet_networks", "network_id", False),
            ("wallet_addresses", "network_id", "wallet_networks", "network_id", False),
            (
                "wallet_network_revisions",
                "network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            ("wallet_asset_revisions", "asset_id", "wallet_assets", "asset_id", False),
            (
                "wallet_address_revisions",
                "address_id",
                "wallet_addresses",
                "address_id",
                False,
            ),
            (
                "wallet_balance_snapshots",
                "network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            ("wallet_balance_snapshots", "asset_id", "wallet_assets", "asset_id", False),
            (
                "wallet_balance_snapshots",
                "address_id",
                "wallet_addresses",
                "address_id",
                False,
            ),
            (
                "wallet_balance_acquisition_runs",
                "network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            (
                "wallet_balance_acquisition_runs",
                "asset_id",
                "wallet_assets",
                "asset_id",
                False,
            ),
            (
                "wallet_balance_acquisition_runs",
                "address_id",
                "wallet_addresses",
                "address_id",
                False,
            ),
            (
                "wallet_balance_acquisition_runs",
                "snapshot_id",
                "wallet_balance_snapshots",
                "snapshot_id",
                True,
            ),
            (
                "wallet_balance_acquisition_runs",
                "created_audit_id",
                "audit_records",
                "audit_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "run_id",
                "wallet_balance_acquisition_runs",
                "run_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "asset_id",
                "wallet_assets",
                "asset_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "address_id",
                "wallet_addresses",
                "address_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "snapshot_id",
                "wallet_balance_snapshots",
                "snapshot_id",
                True,
            ),
            (
                "wallet_reward_workflows",
                "assistance_request_id",
                "autonomous_project_assistance_requests",
                "request_id",
                False,
            ),
            ("wallet_reward_workflows", "project_id", "autonomous_projects", "project_id", False),
            ("wallet_reward_workflows", "phase_id", "autonomous_project_phases", "phase_id", False),
            ("wallet_reward_workflows", "goal_id", "goals", "goal_id", False),
            ("wallet_reward_workflows", "post_id", "public_posts", "post_id", False),
            ("wallet_reward_workflows", "bounty_id", "wallet_bounties", "bounty_id", False),
            ("wallet_reward_workflows", "created_audit_id", "audit_records", "audit_id", False),
            ("wallet_reward_workflows", "last_audit_id", "audit_records", "audit_id", False),
            (
                "wallet_reward_submission_links",
                "submission_id",
                "wallet_bounty_submissions",
                "submission_id",
                False,
            ),
            (
                "wallet_reward_submission_links",
                "workflow_id",
                "wallet_reward_workflows",
                "workflow_id",
                False,
            ),
            (
                "wallet_reward_submission_links",
                "inbound_event_id",
                "interaction_inbound_events",
                "event_id",
                True,
            ),
            (
                "wallet_reward_submission_links",
                "interaction_id",
                "interactions",
                "interaction_id",
                True,
            ),
            (
                "wallet_reward_submission_links",
                "claimed_network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            (
                "wallet_reward_submission_links",
                "created_audit_id",
                "audit_records",
                "audit_id",
                False,
            ),
            (
                "wallet_reward_submission_links",
                "decision_audit_id",
                "audit_records",
                "audit_id",
                True,
            ),
            (
                "wallet_reward_incidents",
                "workflow_id",
                "wallet_reward_workflows",
                "workflow_id",
                False,
            ),
            (
                "wallet_reward_incidents",
                "submission_id",
                "wallet_bounty_submissions",
                "submission_id",
                True,
            ),
            (
                "wallet_reward_incidents",
                "execution_id",
                "wallet_payment_executions",
                "execution_id",
                True,
            ),
            ("wallet_reward_incidents", "opened_audit_id", "audit_records", "audit_id", False),
            ("wallet_reward_incidents", "resolved_audit_id", "audit_records", "audit_id", True),
        )
        for table, column, parent, parent_column, nullable in links:
            condition = (f"NEW.{column} IS NOT NULL AND " if nullable else "") + (
                f"NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{parent_column} = NEW.{column} "
                "AND p.subject_id = NEW.subject_id)"
            )

            base = f"validate_{table}_{column}_subject"
            Database._execute_sql_script(
                connection,
                f"""
CREATE TRIGGER IF NOT EXISTS {base}_insert
BEFORE INSERT ON {table}
WHEN {condition}
BEGIN
    SELECT RAISE(ABORT, 'subject-scoped reference mismatch');
END;
CREATE TRIGGER IF NOT EXISTS {base}_update
BEFORE UPDATE OF subject_id, {column} ON {table}
WHEN {condition}
BEGIN
    SELECT RAISE(ABORT, 'subject-scoped reference mismatch');
END;
""",
            )

    @staticmethod
    def _ensure_wallet_reward_triggers(connection: sqlite3.Connection) -> None:
        """Keep reward workflow provenance append-only and state-machine bounded."""
        Database._execute_sql_script(
            connection,
            """
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_workflow_identity_update
BEFORE UPDATE OF workflow_id,subject_id,assistance_request_id,project_id,phase_id,goal_id,post_id,bounty_id,idempotency_key,created_audit_id,created_at
ON wallet_reward_workflows
BEGIN SELECT RAISE(ABORT,'wallet reward workflow identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_workflow_transition
BEFORE UPDATE ON wallet_reward_workflows
WHEN NOT (
    (NEW.status=OLD.status AND NEW.manual_reason IS OLD.manual_reason AND NEW.state_hash=OLD.state_hash AND NEW.updated_at=OLD.updated_at AND NEW.last_audit_id=OLD.last_audit_id)
    OR (OLD.status='awaiting_publication' AND NEW.status IN ('open','cancelled','manual_intervention'))
    OR (OLD.status='open' AND NEW.status IN ('closed','cancelled','manual_intervention'))
    OR (OLD.status='manual_intervention' AND NEW.status IN ('open','closed','cancelled'))
)
BEGIN SELECT RAISE(ABORT,'wallet reward workflow transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_submission_link_identity_update
BEFORE UPDATE OF submission_id,workflow_id,subject_id,source_type,inbound_event_id,interaction_id,claimed_network_id,source_content_hash,created_audit_id,created_at
ON wallet_reward_submission_links
BEGIN SELECT RAISE(ABORT,'wallet reward submission link identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_submission_link_transition
BEFORE UPDATE ON wallet_reward_submission_links
WHEN NOT (
    (NEW.verification_status=OLD.verification_status AND NEW.criteria_results_json IS OLD.criteria_results_json AND NEW.verification_reason IS OLD.verification_reason AND NEW.verified_by IS OLD.verified_by AND NEW.state_hash=OLD.state_hash AND NEW.updated_at=OLD.updated_at AND NEW.decision_audit_id IS OLD.decision_audit_id)
    OR (OLD.verification_status='pending_verification' AND NEW.verification_status IN ('accepted','rejected','manual_intervention'))
)
BEGIN SELECT RAISE(ABORT,'wallet reward submission link transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_submission_provenance
BEFORE INSERT ON wallet_reward_submission_links
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_bounty_submissions s JOIN wallet_reward_workflows w ON w.workflow_id=NEW.workflow_id
    WHERE s.submission_id=NEW.submission_id AND s.subject_id=NEW.subject_id AND s.bounty_id=w.bounty_id
)
BEGIN SELECT RAISE(ABORT,'wallet reward submission provenance mismatch'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_submission_status
BEFORE UPDATE OF verification_status ON wallet_reward_submission_links
WHEN (NEW.verification_status='accepted' AND NOT EXISTS (SELECT 1 FROM wallet_bounty_submissions s WHERE s.submission_id=NEW.submission_id AND s.status='accepted'))
   OR (NEW.verification_status='rejected' AND NOT EXISTS (SELECT 1 FROM wallet_bounty_submissions s WHERE s.submission_id=NEW.submission_id AND s.status='rejected'))
BEGIN SELECT RAISE(ABORT,'wallet reward submission status mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_incident_identity_update
BEFORE UPDATE OF incident_id,subject_id,workflow_id,submission_id,execution_id,kind,idempotency_key,opened_audit_id,created_at
ON wallet_reward_incidents
BEGIN SELECT RAISE(ABORT,'wallet reward incident identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_incident_transition
BEFORE UPDATE ON wallet_reward_incidents
WHEN NOT (
    (NEW.status=OLD.status AND NEW.reason=OLD.reason AND NEW.resolution IS OLD.resolution AND NEW.state_hash=OLD.state_hash AND NEW.updated_at=OLD.updated_at AND NEW.resolved_audit_id IS OLD.resolved_audit_id)
    OR (OLD.status='open' AND NEW.status='resolved' AND NEW.resolution IS NOT NULL AND NEW.resolved_audit_id IS NOT NULL AND NEW.state_hash<>OLD.state_hash)
)
BEGIN SELECT RAISE(ABORT,'wallet reward incident transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_incident_provenance
BEFORE INSERT ON wallet_reward_incidents
WHEN NOT EXISTS (SELECT 1 FROM wallet_reward_workflows w WHERE w.workflow_id=NEW.workflow_id AND w.subject_id=NEW.subject_id)
   OR (NEW.submission_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM wallet_bounty_submissions s WHERE s.submission_id=NEW.submission_id AND s.subject_id=NEW.subject_id))
   OR (NEW.execution_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM wallet_payment_executions e WHERE e.execution_id=NEW.execution_id AND e.subject_id=NEW.subject_id))
BEGIN SELECT RAISE(ABORT,'wallet reward incident provenance mismatch'); END;
""",
        )

    @staticmethod
    def _ensure_inbound_triggers(connection: sqlite3.Connection) -> None:
        Database._execute_sql_script(
            connection,
            """
DROP TRIGGER IF EXISTS validate_interaction_binding_transport_insert;
DROP TRIGGER IF EXISTS validate_interaction_binding_transport_update;
DROP TRIGGER IF EXISTS validate_interaction_inbound_transport_insert;
DROP TRIGGER IF EXISTS validate_interaction_inbound_interaction_insert;
DROP TRIGGER IF EXISTS validate_interaction_thread_transport_insert;
DROP TRIGGER IF EXISTS prevent_interaction_binding_identity_update;
DROP TRIGGER IF EXISTS prevent_interaction_binding_delete;
DROP TRIGGER IF EXISTS prevent_interaction_inbound_identity_update;
DROP TRIGGER IF EXISTS prevent_interaction_inbound_delete;
DROP TRIGGER IF EXISTS validate_interaction_inbound_status_insert;
DROP TRIGGER IF EXISTS validate_interaction_inbound_status_update;
DROP TRIGGER IF EXISTS prevent_interaction_thread_identity_update;
DROP TRIGGER IF EXISTS prevent_interaction_thread_delete;

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
CREATE TRIGGER prevent_interaction_binding_identity_update
BEFORE UPDATE OF binding_id, subject_id, transport_id, channel,
                 external_account_id, external_sender_id, role, label, created_at
ON interaction_bindings
WHEN NEW.binding_id IS NOT OLD.binding_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.transport_id IS NOT OLD.transport_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.external_account_id IS NOT OLD.external_account_id
  OR NEW.external_sender_id IS NOT OLD.external_sender_id
  OR NEW.role IS NOT OLD.role
  OR NEW.label IS NOT OLD.label
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'inbound binding identity is immutable');
END;
CREATE TRIGGER prevent_interaction_binding_delete
BEFORE DELETE ON interaction_bindings
BEGIN
    SELECT RAISE(ABORT, 'inbound bindings cannot be deleted');
END;
CREATE TRIGGER prevent_interaction_inbound_identity_update
BEFORE UPDATE OF event_id, subject_id, transport_id, channel, provider_event_id,
                 external_account_id, external_sender_id, conversation_id,
                 content_hash, scheduling_priority, received_at,
                 external_thread_id, reply_selector, reply_context_version
ON interaction_inbound_events
WHEN NEW.event_id IS NOT OLD.event_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.transport_id IS NOT OLD.transport_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.provider_event_id IS NOT OLD.provider_event_id
  OR NEW.external_account_id IS NOT OLD.external_account_id
  OR NEW.external_sender_id IS NOT OLD.external_sender_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.content_hash IS NOT OLD.content_hash
  OR NEW.scheduling_priority IS NOT OLD.scheduling_priority
  OR NEW.received_at IS NOT OLD.received_at
  OR NEW.external_thread_id IS NOT OLD.external_thread_id
  OR NEW.reply_selector IS NOT OLD.reply_selector
  OR NEW.reply_context_version IS NOT OLD.reply_context_version
BEGIN
    SELECT RAISE(ABORT, 'inbound event identity is immutable');
END;
CREATE TRIGGER prevent_interaction_inbound_delete
BEFORE DELETE ON interaction_inbound_events
BEGIN
    SELECT RAISE(ABORT, 'inbound events cannot be deleted');
END;
CREATE TRIGGER validate_interaction_inbound_status_insert
BEFORE INSERT ON interaction_inbound_events
WHEN NEW.status != 'received' OR NEW.processed_at IS NOT NULL OR NEW.interaction_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'inbound event must start in received state');
END;
CREATE TRIGGER validate_interaction_inbound_status_update
BEFORE UPDATE OF status, processed_at, interaction_id ON interaction_inbound_events
WHEN NOT (
    (NEW.status = OLD.status AND NEW.processed_at IS OLD.processed_at
        AND NEW.interaction_id IS OLD.interaction_id)
    OR (OLD.status = 'received' AND NEW.status = 'processed'
        AND NEW.processed_at IS NOT NULL AND NEW.interaction_id IS NOT NULL
        AND EXISTS (SELECT 1 FROM interactions i
                    WHERE i.interaction_id = NEW.interaction_id
                      AND i.subject_id = NEW.subject_id
                      AND i.direction = 'incoming'))
    OR (OLD.status = 'received' AND NEW.status = 'rejected'
        AND NEW.processed_at IS NOT NULL AND NEW.interaction_id IS NULL)
    OR (OLD.status = 'rejected' AND NEW.status = 'received'
        AND NEW.processed_at IS NULL AND NEW.interaction_id IS NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'inbound event status transition is invalid');
END;
CREATE TRIGGER prevent_interaction_thread_identity_update
BEFORE UPDATE OF thread_id, subject_id, transport_id, channel,
                 external_account_id, conversation_id, external_thread_id, created_at
ON interaction_threads
WHEN NEW.thread_id IS NOT OLD.thread_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.transport_id IS NOT OLD.transport_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.external_account_id IS NOT OLD.external_account_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.external_thread_id IS NOT OLD.external_thread_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'interaction thread identity is immutable');
END;
CREATE TRIGGER prevent_interaction_thread_delete
BEFORE DELETE ON interaction_threads
BEGIN
    SELECT RAISE(ABORT, 'interaction threads cannot be deleted');
END;
""",
        )

    @staticmethod
    def _ensure_public_post_triggers(connection: sqlite3.Connection) -> None:
        """Keep public-post identity and moderation evidence append-only."""
        Database._execute_sql_script(
            connection,
            """
DROP TRIGGER IF EXISTS prevent_public_post_identity_update;
CREATE TRIGGER prevent_public_post_identity_update
BEFORE UPDATE OF post_id, subject_id, kind, title, content, content_hash,
                 author_label, author_provenance, identity_hash,
                 idempotency_key, created_at ON public_posts
BEGIN
    SELECT RAISE(ABORT, 'public post identity is immutable');
END;
DROP TRIGGER IF EXISTS validate_public_post_identity_insert;
CREATE TRIGGER validate_public_post_identity_insert
BEFORE INSERT ON public_posts
WHEN NEW.identity_hash IS NULL OR NEW.identity_hash = ''
BEGIN
    SELECT RAISE(ABORT, 'public post identity hash is required');
END;
DROP TRIGGER IF EXISTS prevent_public_post_moderation_update;
CREATE TRIGGER prevent_public_post_moderation_update
BEFORE UPDATE ON public_post_moderation_events
BEGIN
    SELECT RAISE(ABORT, 'public post moderation history is append-only');
END;
DROP TRIGGER IF EXISTS validate_public_post_moderation_transition;
CREATE TRIGGER validate_public_post_moderation_transition
BEFORE INSERT ON public_post_moderation_events
WHEN NEW.revision IS NULL
  OR NEW.revision < 1
  OR NEW.idempotency_key IS NULL
  OR NEW.idempotency_key = ''
  OR NOT EXISTS (
    SELECT 1 FROM public_posts p
    WHERE p.post_id = NEW.post_id
      AND p.subject_id = NEW.subject_id
      AND p.status = NEW.to_status
       AND (
           (
               NEW.revision = 1
               AND NEW.previous_event_id IS NULL
               AND NEW.from_status = 'pending_review'
               AND NEW.to_status IN ('published', 'rejected')
               AND NOT EXISTS (
                   SELECT 1 FROM public_post_moderation_events h
                   WHERE h.subject_id = NEW.subject_id AND h.post_id = NEW.post_id
               )
           )
           OR (
               NEW.revision > 1
               AND NEW.previous_event_id IS NOT NULL
               AND NEW.from_status IN ('published', 'rejected')
               AND NEW.to_status = 'archived'
               AND EXISTS (
                   SELECT 1 FROM public_post_moderation_events h
                    WHERE h.subject_id = NEW.subject_id AND h.post_id = NEW.post_id
                     AND h.event_id = NEW.previous_event_id
                     AND h.to_status = NEW.from_status
                     AND h.revision = NEW.revision - 1
                     AND h.revision = (
                         SELECT MAX(latest.revision)
                         FROM public_post_moderation_events latest
                         WHERE latest.subject_id = NEW.subject_id
                           AND latest.post_id = NEW.post_id
                     )
               )
           )
       )
)
BEGIN
    SELECT RAISE(ABORT, 'public post moderation transition mismatch');
END;
""",
        )

    @staticmethod
    def _ensure_wallet_acquisition_triggers(connection: sqlite3.Connection) -> None:
        """Repair v54 acquisition guards from the canonical migration script.

        Early v54 builds shipped an incorrect trigger definition.  Replaying
        the migration's idempotent DDL keeps repair behavior byte-for-byte
        aligned with fresh installs and prevents the repair copy from drifting.
        """
        Database._execute_sql_script(connection, MIGRATIONS[54])

    @staticmethod
    def _ensure_wallet_execution_triggers(connection: sqlite3.Connection) -> None:
        """Replay the canonical v56 guards for databases opened at v56."""
        Database._execute_sql_script(connection, MIGRATIONS[56])

    @staticmethod
    def _ensure_wallet_submission_consent_contract(connection: sqlite3.Connection) -> None:
        """Verify the v59 column and reinstall its canonical immutable guards."""
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(wallet_bounty_submissions)")
        }
        if "consent_version" not in columns:
            raise RuntimeError("wallet submission consent column is missing")
        Database._execute_sql_script(connection, MIGRATIONS[59])

    @staticmethod
    def _upgrade_wallet_submission_consent(connection: sqlite3.Connection) -> None:
        """Add consent provenance and rehash every pre-v59 submission atomically."""
        connection.execute("DROP TRIGGER IF EXISTS prevent_wallet_submission_identity_update")
        connection.execute("DROP TRIGGER IF EXISTS validate_wallet_submission_transition")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(wallet_bounty_submissions)")
        }
        had_consent_column = "consent_version" in columns
        rows = connection.execute(
            "SELECT * FROM wallet_bounty_submissions ORDER BY created_at,submission_id"
        ).fetchall()

        def legacy_hash(row: sqlite3.Row) -> str:
            return content_hash(
                {
                    "submission_id": str(row["submission_id"]),
                    "bounty_id": str(row["bounty_id"]),
                    "subject_id": str(row["subject_id"]),
                    "counterparty": str(row["counterparty"]),
                    "content": str(row["content"]),
                    "evidence_json": str(row["evidence_json"]),
                    "recipient_address": str(row["recipient_address"]),
                    "idempotency_key": str(row["idempotency_key"]),
                    "status": str(row["status"]),
                    "decision_reason": row["decision_reason"],
                }
            )

        # A migration must not turn an already-tampered legacy row into a
        # trusted row merely by changing the hash format.
        if not had_consent_column:
            for row in rows:
                if row["state_hash"] != legacy_hash(row):
                    raise RuntimeError(
                        f"wallet submission legacy state hash mismatch: {row['submission_id']}"
                    )
        if "consent_version" not in columns:
            connection.execute(
                "ALTER TABLE wallet_bounty_submissions ADD COLUMN consent_version "
                "INTEGER NOT NULL DEFAULT 1 CHECK(consent_version BETWEEN 1 AND 1000)"
            )
        rows = connection.execute(
            "SELECT * FROM wallet_bounty_submissions ORDER BY created_at,submission_id"
        ).fetchall()
        for row in rows:
            expected = wallet_submission_state_hash(
                submission_id=str(row["submission_id"]),
                bounty_id=str(row["bounty_id"]),
                subject_id=str(row["subject_id"]),
                counterparty=str(row["counterparty"]),
                content=str(row["content"]),
                evidence_json=str(row["evidence_json"]),
                recipient_address=str(row["recipient_address"]),
                idempotency_key=str(row["idempotency_key"]),
                consent_version=int(row["consent_version"]),
                status=str(row["status"]),
                decision_reason=row["decision_reason"],
            )
            if row["state_hash"] != expected:
                if had_consent_column and (
                    int(row["consent_version"]) != 1 or row["state_hash"] != legacy_hash(row)
                ):
                    raise RuntimeError(
                        f"wallet submission state hash mismatch: {row['submission_id']}"
                    )
                connection.execute(
                    "UPDATE wallet_bounty_submissions SET state_hash=? WHERE submission_id=?",
                    (expected, row["submission_id"]),
                )
        Database._execute_sql_script(connection, MIGRATIONS[59])

    @staticmethod
    def _upgrade_wallet_payment_policy_hashes(
        connection: sqlite3.Connection, *, approved: bool = False
    ) -> None:
        """Upgrade creation-anchored defaults or explicitly authorized legacy state."""
        rows = connection.execute(
            "SELECT p.*, s.created_at AS identity_created_at "
            "FROM wallet_payment_policies p JOIN subject_identity s USING(subject_id) "
            "ORDER BY p.subject_id"
        )
        for row in rows:
            expected = wallet_payment_policy_state_hash(
                subject_id=str(row["subject_id"]),
                mode=str(row["mode"]),
                allowed_network_ids_json=str(row["allowed_network_ids_json"]),
                allowed_asset_ids_json=str(row["allowed_asset_ids_json"]),
                per_order_limit=str(row["per_order_limit"]),
                daily_limit=str(row["daily_limit"]),
                monthly_limit=str(row["monthly_limit"]),
                daily_order_limit=strict_int(row["daily_order_limit"]),
                monthly_order_limit=strict_int(row["monthly_order_limit"]),
                min_balance=str(row["min_balance"]),
                max_observation_age_seconds=strict_int(row["max_observation_age_seconds"]),
                automatic_max_amount=str(row["automatic_max_amount"]),
                anomaly_block=int(row["anomaly_block"]),
                emergency_paused=int(row["emergency_paused"]),
                policy_version=strict_int(row["policy_version"]),
                updated_at=str(row["updated_at"]),
            )
            if row["state_hash"] == expected:
                continue
            is_default_bootstrap = (
                row["state_hash"] == "bootstrap"
                and str(row["mode"]) == "disabled"
                and str(row["allowed_network_ids_json"]) == "[]"
                and str(row["allowed_asset_ids_json"]) == "[]"
                and str(row["per_order_limit"]) == "0"
                and str(row["daily_limit"]) == "0"
                and str(row["monthly_limit"]) == "0"
                and int(row["daily_order_limit"]) == 0
                and int(row["monthly_order_limit"]) == 0
                and str(row["min_balance"]) == "0"
                and int(row["max_observation_age_seconds"]) == 0
                and str(row["automatic_max_amount"]) == "0"
                and int(row["anomaly_block"]) == 1
                and int(row["emergency_paused"]) == 0
                and int(row["policy_version"]) == 1
                and row["updated_at"] == row["identity_created_at"]
            )
            if row["state_hash"] == "bootstrap":
                if not is_default_bootstrap and not approved:
                    raise RuntimeError(
                        f"wallet payment policy bootstrap state mismatch: {row['subject_id']}"
                    )
            elif not approved:
                raise RuntimeError(
                    "wallet payment policy legacy hash cannot be upgraded safely: "
                    f"{row['subject_id']}"
                )
            elif row["state_hash"] != content_hash(
                {
                    "subject_id": row["subject_id"],
                    "version": row["policy_version"],
                    "mode": row["mode"],
                    "updated_at": row["updated_at"],
                }
            ):
                raise RuntimeError("wallet payment policy legacy hash mismatch")
            connection.execute(
                "UPDATE wallet_payment_policies SET state_hash=? WHERE subject_id=?",
                (expected, row["subject_id"]),
            )

    @staticmethod
    def _upgrade_wallet_ledger_hashes(connection: sqlite3.Connection, *, approved: bool) -> None:
        for table, primary_key, trigger, hasher, legacy_hasher in (
            (
                "wallet_ledger_journals",
                "journal_id",
                "prevent_wallet_journal_update",
                wallet_journal_hash,
                legacy_journal_hash,
            ),
            (
                "wallet_ledger_entries",
                "entry_id",
                "prevent_wallet_entry_update",
                wallet_entry_hash,
                legacy_entry_hash,
            ),
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {primary_key}"):
                expected = hasher(row)
                if row["state_hash"] == expected:
                    continue
                if row["state_hash"] != legacy_hasher(row):
                    raise RuntimeError(f"wallet ledger legacy hash mismatch: {row[primary_key]}")
                if not approved:
                    raise RuntimeError("wallet ledger legacy timestamps require explicit approval")
                connection.execute(
                    f"UPDATE {table} SET state_hash=? WHERE {primary_key}=?",
                    (expected, row[primary_key]),
                )
            connection.execute(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT,'wallet ledger is append-only'); END"
            )

    @staticmethod
    def _upgrade_wallet_temporal_hashes(connection: sqlite3.Connection, *, approved: bool) -> None:
        """Bind payment chronology to hashes without trusting damaged rows.

        Schema 60/early-61 order and execution hashes did not cover their
        chronology fields.  A matching legacy hash proves only the fields it
        originally covered; it cannot prove that a timestamp was not edited
        offline.  Consequently those rows require the same explicit,
        fingerprint-bound operator approval as the other legacy wallet
        records.  Never silently re-sign a damaged history during migration.
        """
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(wallet_payment_orders)")
        }
        if "authorized_at" not in columns:
            connection.execute("ALTER TABLE wallet_payment_orders ADD COLUMN authorized_at TEXT")
        connection.execute("DROP TRIGGER IF EXISTS prevent_wallet_order_identity_update")
        connection.execute("DROP TRIGGER IF EXISTS validate_wallet_order_transition")
        connection.execute("DROP TRIGGER IF EXISTS validate_wallet_execution_transition")

        def old_order(row: sqlite3.Row) -> str:
            return content_hash(
                {
                    "order_id": row["order_id"],
                    "subject_id": row["subject_id"],
                    "bounty_id": row["bounty_id"],
                    "submission_id": row["submission_id"],
                    "network_id": row["network_id"],
                    "asset_id": row["asset_id"],
                    "recipient_address": row["recipient_address"],
                    "amount": row["amount"],
                    "payment_mode": row["payment_mode"],
                    "policy_version": int(row["policy_version"]),
                    "idempotency_key": row["idempotency_key"],
                    "status": row["status"],
                }
            )

        for row in connection.execute(
            "SELECT * FROM wallet_payment_orders ORDER BY order_id"
        ).fetchall():
            authorized_at = row["authorized_at"]
            if authorized_at is None and row["status"] in {
                "reserved",
                "signing",
                "broadcast",
                "unknown",
                "confirmed",
                "failed",
                "refunded",
            }:
                authorized_at = row["updated_at"]
            temporal = content_hash(
                {
                    "order_id": row["order_id"],
                    "subject_id": row["subject_id"],
                    "bounty_id": row["bounty_id"],
                    "submission_id": row["submission_id"],
                    "network_id": row["network_id"],
                    "asset_id": row["asset_id"],
                    "recipient_address": row["recipient_address"],
                    "amount": row["amount"],
                    "payment_mode": row["payment_mode"],
                    "policy_version": int(row["policy_version"]),
                    "idempotency_key": row["idempotency_key"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "authorized_at": authorized_at,
                    "updated_at": row["updated_at"],
                }
            )
            if row["state_hash"] != temporal and row["state_hash"] != old_order(row):
                raise RuntimeError(f"wallet payment order legacy hash mismatch: {row['order_id']}")
            if row["state_hash"] == temporal:
                continue
            if not approved:
                raise RuntimeError(
                    "wallet payment order legacy timestamps require explicit approval"
                )
            connection.execute(
                "UPDATE wallet_payment_orders SET authorized_at=?,state_hash=? WHERE order_id=?",
                (authorized_at, temporal, row["order_id"]),
            )

        def old_execution(row: sqlite3.Row) -> str:
            keys = (
                "execution_id",
                "subject_id",
                "order_id",
                "network_id",
                "asset_id",
                "source_address",
                "recipient_address",
                "asset_type",
                "contract_address",
                "amount",
                "chain_id",
                "nonce",
                "gas_limit",
                "max_fee_per_gas",
                "request_id",
                "request_hash",
                "signer_id",
                "status",
                "tx_hash",
                "error_code",
                "receipt_status",
                "receipt_block_number",
                "attempt_count",
            )
            return content_hash({key: row[key] for key in keys})

        execution_rows = connection.execute(
            "SELECT * FROM wallet_payment_executions ORDER BY execution_id"
        ).fetchall()
        for row in execution_rows:
            temporal = content_hash(
                {
                    **{
                        key: row[key]
                        for key in (
                            "execution_id",
                            "subject_id",
                            "order_id",
                            "network_id",
                            "asset_id",
                            "source_address",
                            "recipient_address",
                            "asset_type",
                            "contract_address",
                            "amount",
                            "chain_id",
                            "nonce",
                            "gas_limit",
                            "max_fee_per_gas",
                            "request_id",
                            "request_hash",
                            "signer_id",
                            "status",
                            "tx_hash",
                            "error_code",
                            "receipt_status",
                            "receipt_block_number",
                            "attempt_count",
                        )
                    },
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
            if row["state_hash"] == temporal:
                continue
            if row["state_hash"] != old_execution(row):
                raise RuntimeError(
                    f"wallet payment execution legacy hash mismatch: {row['execution_id']}"
                )
            if not approved:
                raise RuntimeError(
                    "wallet payment execution legacy timestamps require explicit approval"
                )
            connection.execute(
                "UPDATE wallet_payment_executions SET state_hash=? WHERE execution_id=?",
                (temporal, row["execution_id"]),
            )
        # Restore the canonical execution transition guards after the
        # timestamp backfill temporarily disabled them.
        Database._execute_sql_script(connection, MIGRATIONS[56])

    @staticmethod
    def _upgrade_wallet_receipt_evidence(connection: sqlite3.Connection) -> None:
        """Add durable block/effect evidence without trusting old hashes."""
        existing_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(wallet_payment_executions)")
        }
        evidence_preexisted = {
            name
            for name in ("receipt_block_hash", "receipt_confirmations", "receipt_effect_hash")
            if name in existing_columns
        }
        for name, declaration in (
            ("receipt_block_hash", "TEXT"),
            ("receipt_confirmations", "INTEGER"),
            ("receipt_effect_hash", "TEXT"),
        ):
            if name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE wallet_payment_executions ADD COLUMN {name} {declaration}"
                )

        base_keys = (
            "execution_id",
            "subject_id",
            "order_id",
            "network_id",
            "asset_id",
            "source_address",
            "recipient_address",
            "asset_type",
            "contract_address",
            "amount",
            "chain_id",
            "nonce",
            "gas_limit",
            "max_fee_per_gas",
            "request_id",
            "request_hash",
            "signer_id",
            "status",
            "tx_hash",
            "error_code",
            "receipt_status",
            "receipt_block_number",
            "attempt_count",
            "created_at",
            "updated_at",
        )
        for row in connection.execute(
            "SELECT * FROM wallet_payment_executions ORDER BY execution_id"
        ).fetchall():
            old_hash = content_hash({key: row[key] for key in base_keys})
            new_values = {key: row[key] for key in base_keys[:-2]}
            new_values.update(
                {
                    "receipt_block_hash": row["receipt_block_hash"],
                    "receipt_confirmations": row["receipt_confirmations"],
                    "receipt_effect_hash": row["receipt_effect_hash"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
            new_hash = content_hash(new_values)
            if row["state_hash"] == new_hash:
                continue
            # Schema 61 did not define these columns.  If a hand-modified or
            # vendor-specific database already had them, an old state hash
            # cannot authenticate their values; refuse to launder them into a
            # trusted history during migration.
            if evidence_preexisted and any(
                row[name] is not None
                for name in ("receipt_block_hash", "receipt_confirmations", "receipt_effect_hash")
            ):
                raise RuntimeError(
                    f"wallet payment execution receipt evidence is unauthenticated: {row['execution_id']}"
                )
            if row["state_hash"] != old_hash:
                raise RuntimeError(
                    f"wallet payment execution receipt evidence hash mismatch: {row['execution_id']}"
                )
            connection.execute(
                "UPDATE wallet_payment_executions SET receipt_confirmations=?,state_hash=? "
                "WHERE execution_id=?",
                (new_values["receipt_confirmations"], new_hash, row["execution_id"]),
            )
        Database._execute_sql_script(connection, MIGRATIONS[62])

    @staticmethod
    def _ensure_public_post_moderation_history(connection: sqlite3.Connection) -> None:
        """Backfill an auditable baseline for posts created before history existed."""
        rows = connection.execute(
            """SELECT p.* FROM public_posts p
               WHERE p.status IN ('published', 'rejected', 'archived')
                 AND NOT EXISTS (
                     SELECT 1 FROM public_post_moderation_events h
                     WHERE h.subject_id = p.subject_id AND h.post_id = p.post_id
                 )
               ORDER BY p.created_at, p.post_id"""
        ).fetchall()
        for row in rows:
            post_id = str(row["post_id"])
            subject_id = str(row["subject_id"])
            actor = "migration"
            reason = "legacy moderation baseline"
            baseline_created_at = str(row["created_at"])
            initial_status = (
                ("published" if row["published_at"] is not None else "rejected")
                if row["status"] == "archived"
                else str(row["status"])
            )
            baseline = {
                "event_id": f"post-moderation-legacy-{post_id}-0-baseline",
                "subject_id": subject_id,
                "post_id": post_id,
                "from_status": "pending_review",
                "to_status": initial_status,
                "actor": actor,
                "reason": reason,
                "created_at": baseline_created_at,
            }
            connection.execute(
                """INSERT INTO public_post_moderation_events(
                    event_id, subject_id, post_id, from_status, to_status,
                    actor, reason, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    baseline["event_id"],
                    subject_id,
                    post_id,
                    baseline["from_status"],
                    baseline["to_status"],
                    actor,
                    reason,
                    content_hash(baseline),
                    baseline_created_at,
                ),
            )
            if row["status"] != "archived":
                continue
            archive_created_at = str(row["updated_at"])
            # Keep the persisted timestamp shape valid even when an older
            # database recorded equal or reversed created/updated values.  The
            # event-id suffix gives the deterministic order for equal values.
            if archive_created_at < baseline_created_at:
                archive_created_at = baseline_created_at
            archive = {
                "event_id": f"post-moderation-legacy-{post_id}-1-archive",
                "subject_id": subject_id,
                "post_id": post_id,
                "from_status": initial_status,
                "to_status": "archived",
                "actor": actor,
                "reason": reason,
                "created_at": archive_created_at,
            }
            connection.execute(
                """INSERT INTO public_post_moderation_events(
                    event_id, subject_id, post_id, from_status, to_status,
                    actor, reason, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    archive["event_id"],
                    subject_id,
                    post_id,
                    archive["from_status"],
                    archive["to_status"],
                    actor,
                    reason,
                    content_hash(archive),
                    archive_created_at,
                ),
            )

    @staticmethod
    def _upgrade_public_post_evidence(connection: sqlite3.Connection) -> None:
        """Upgrade legacy moderation rows into a deterministic revision chain."""
        posts = connection.execute(
            "SELECT * FROM public_posts ORDER BY subject_id, created_at, post_id"
        ).fetchall()
        for post in posts:
            if content_hash(post["content"]) != post["content_hash"]:
                raise RuntimeError(f"public post content integrity mismatch: {post['post_id']}")
            connection.execute(
                "UPDATE public_posts SET identity_hash = ? WHERE post_id = ?",
                (public_post_identity_hash(post), post["post_id"]),
            )
            events = connection.execute(
                "SELECT * FROM public_post_moderation_events "
                "WHERE subject_id = ? AND post_id = ? ORDER BY created_at, event_id",
                (post["subject_id"], post["post_id"]),
            ).fetchall()
            for event in events:
                legacy_state = {
                    key: event[key]
                    for key in (
                        "event_id",
                        "subject_id",
                        "post_id",
                        "from_status",
                        "to_status",
                        "actor",
                        "reason",
                        "created_at",
                    )
                }
                if event["state_hash"] != content_hash(legacy_state):
                    raise RuntimeError(
                        f"public post moderation integrity mismatch: {event['event_id']}"
                    )
            remaining = list(events)
            chain: list[sqlite3.Row] = []
            expected_status = "pending_review"
            allowed = {
                "pending_review": {"published", "rejected"},
                "published": {"archived"},
                "rejected": {"archived"},
            }
            while remaining:
                candidates = [
                    event
                    for event in remaining
                    if event["from_status"] == expected_status
                    and event["to_status"] in allowed.get(expected_status, set())
                ]
                if len(candidates) != 1:
                    raise RuntimeError(
                        f"public post moderation migration is ambiguous: {post['post_id']}"
                    )
                event = candidates[0]
                chain.append(event)
                remaining.remove(event)
                expected_status = str(event["to_status"])
            if expected_status != post["status"] and not (
                not chain and post["status"] in {"draft", "pending_review"}
            ):
                raise RuntimeError(
                    f"public post moderation migration is incomplete: {post['post_id']}"
                )
            previous_event_id: str | None = None
            for revision, event in enumerate(chain, start=1):
                idempotency_key = str(event["event_id"])
                state = {
                    "event_id": event["event_id"],
                    "subject_id": event["subject_id"],
                    "post_id": event["post_id"],
                    "from_status": event["from_status"],
                    "to_status": event["to_status"],
                    "actor": event["actor"],
                    "reason": event["reason"],
                    "revision": revision,
                    "previous_event_id": previous_event_id,
                    "idempotency_key": idempotency_key,
                    "created_at": event["created_at"],
                }
                connection.execute(
                    "UPDATE public_post_moderation_events SET revision = ?, "
                    "previous_event_id = ?, idempotency_key = ?, state_hash = ? "
                    "WHERE event_id = ?",
                    (
                        revision,
                        previous_event_id,
                        idempotency_key,
                        content_hash(state),
                        event["event_id"],
                    ),
                )
                previous_event_id = str(event["event_id"])

        controls = connection.execute("SELECT * FROM public_post_controls").fetchall()
        for row in controls:
            legacy_state = {
                key: row[key]
                for key in (
                    "subject_id",
                    "rate_limit_per_hour",
                    "queue_cap",
                    "captcha_ttl_seconds",
                    "captcha_max_attempts",
                    "captcha_mode",
                    "updated_at",
                    "updated_by",
                )
            }
            if row["state_hash"] != content_hash(legacy_state):
                raise RuntimeError(f"public post controls integrity mismatch: {row['subject_id']}")
            state = {
                key: row[key]
                for key in (
                    "subject_id",
                    "rate_limit_per_hour",
                    "queue_cap",
                    "captcha_ttl_seconds",
                    "captcha_max_attempts",
                    "captcha_mode",
                    "storage_cap_bytes",
                    "captcha_issue_limit_per_hour",
                    "captcha_global_rate_per_minute",
                    "updated_at",
                    "updated_by",
                )
            }
            connection.execute(
                "UPDATE public_post_controls SET state_hash = ? WHERE subject_id = ?",
                (content_hash(state), row["subject_id"]),
            )

        Database._execute_sql_script(
            connection,
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_public_post_moderation_revision
    ON public_post_moderation_events(subject_id, post_id, revision);
CREATE UNIQUE INDEX IF NOT EXISTS uq_public_post_moderation_idempotency
    ON public_post_moderation_events(subject_id, idempotency_key);
CREATE TRIGGER prevent_public_post_moderation_update
BEFORE UPDATE ON public_post_moderation_events
BEGIN
    SELECT RAISE(ABORT, 'public post moderation history is append-only');
END;
""",
        )

    @staticmethod
    def _ensure_audit_record_triggers(connection: sqlite3.Connection) -> None:
        Database._execute_sql_script(
            connection,
            """
CREATE TRIGGER IF NOT EXISTS prevent_audit_record_update
BEFORE UPDATE ON audit_records BEGIN
    SELECT RAISE(ABORT, 'audit records are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_audit_record_delete
BEFORE DELETE ON audit_records BEGIN
    SELECT RAISE(ABORT, 'audit records cannot be deleted');
END;
""",
        )

    @staticmethod
    def _ensure_behavior_log_revision_triggers(connection: sqlite3.Connection) -> None:
        Database._execute_sql_script(
            connection,
            """
DROP TRIGGER IF EXISTS validate_behavior_log_revision_insert;
CREATE TRIGGER validate_behavior_log_revision_insert
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
DROP TRIGGER IF EXISTS prevent_behavior_log_revision_update;
CREATE TRIGGER prevent_behavior_log_revision_update
BEFORE UPDATE ON behavior_log_revisions BEGIN
    SELECT RAISE(ABORT, 'behavior log revisions are append-only');
END;
DROP TRIGGER IF EXISTS prevent_behavior_log_revision_delete;
CREATE TRIGGER prevent_behavior_log_revision_delete
BEFORE DELETE ON behavior_log_revisions BEGIN
    SELECT RAISE(ABORT, 'behavior log revisions cannot be deleted');
END;
DROP TRIGGER IF EXISTS prevent_behavior_log_update;
CREATE TRIGGER prevent_behavior_log_update
BEFORE UPDATE ON behavior_logs BEGIN
    SELECT RAISE(ABORT, 'behavior logs are append-only');
END;
DROP TRIGGER IF EXISTS prevent_behavior_log_delete;
CREATE TRIGGER prevent_behavior_log_delete
BEFORE DELETE ON behavior_logs BEGIN
    SELECT RAISE(ABORT, 'behavior logs cannot be deleted');
END;
""",
        )

    @staticmethod
    def _backfill_behavior_log_revisions(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT l.* FROM behavior_logs l "
            "LEFT JOIN behavior_log_revisions r ON r.log_id = l.log_id "
            "WHERE r.log_id IS NULL ORDER BY l.occurred_at, l.log_id"
        ).fetchall()
        for row in rows:
            revision = {
                **dict(row),
                "revision_number": 1,
                "reason": "legacy baseline",
            }
            connection.execute(
                """INSERT INTO behavior_log_revisions(
                    revision_id, log_id, action_id, subject_id, revision_number,
                    occurred_at, action_type, public_goal_reference, tool, public_target,
                    result_status, side_effect_summary, resource_summary, public_explanation,
                    redaction_reason, state_hash, reason, created_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("brev"),
                    row["log_id"],
                    row["action_id"],
                    row["subject_id"],
                    row["occurred_at"],
                    row["action_type"],
                    row["public_goal_reference"],
                    row["tool"],
                    row["public_target"],
                    row["result_status"],
                    row["side_effect_summary"],
                    row["resource_summary"],
                    row["public_explanation"],
                    row["redaction_reason"],
                    behavior_log_state_hash(revision),
                    revision["reason"],
                    row["occurred_at"],
                ),
            )

    @staticmethod
    def _assert_behavior_log_revisions_complete(connection: sqlite3.Connection) -> None:
        missing = connection.execute(
            "SELECT l.log_id FROM behavior_logs l "
            "LEFT JOIN behavior_log_revisions r ON r.log_id = l.log_id "
            "WHERE r.log_id IS NULL LIMIT 1"
        ).fetchone()
        if missing is not None:
            raise RuntimeError(
                "behavior log revision history is incomplete: " + str(missing["log_id"])
            )

    @staticmethod
    def _ensure_action_revisions(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT a.* FROM actions a "
            "LEFT JOIN action_revisions r ON r.action_id = a.action_id "
            "WHERE r.action_id IS NULL ORDER BY a.prepared_at, a.action_id"
        ).fetchall()
        for row in rows:
            connection.execute(
                """INSERT INTO action_revisions(
                    revision_id, action_id, subject_id, revision_number, status,
                    state_hash, reason, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
                (
                    new_id("arev"),
                    row["action_id"],
                    row["subject_id"],
                    row["status"],
                    action_state_hash(row),
                    "legacy baseline",
                    utc_now(),
                ),
            )

    def _ensure_training_policies(self) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO training_policies(subject_id, updated_at)
                   SELECT subject_id, updated_at FROM subject_identity"""
            )
            connection.commit()

    def _migrate(self, *, wallet_legacy_approval: WalletLegacyApproval | None = None) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                raise RuntimeError("database schema version is missing")
            version = int(row["value"])
            # A few early development databases only contained schema_meta and
            # a historical marker. Re-run additive migrations from the first
            # version when their anchor tables are absent instead of letting a
            # later optional trigger reference missing tables.
            memories_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
            ).fetchone()
            if memories_exists is None:
                version = 1
            if version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than runtime {CURRENT_SCHEMA_VERSION}"
                )
            if (
                wallet_legacy_approval is not None
                and version == 60
                and wallet_upgrade_fingerprint(connection)
                != wallet_legacy_approval.expected_fingerprint
            ):
                raise RuntimeError("wallet upgrade approval fingerprint mismatch")
            # Migration tests may open a current database with an older marker.
            # Remove reward provenance triggers that reference the v46 inbound
            # table before that table is rebuilt; repair recreates them later.
            if version < 46:
                for trigger in (
                    "validate_wallet_reward_submission_links_inbound_event_id_subject_insert",
                    "validate_wallet_reward_submission_links_inbound_event_id_subject_update",
                ):
                    connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for target_version in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
                migration = MIGRATIONS[target_version]
                if target_version == 21:
                    self._ensure_project_execution_columns(connection)
                if target_version == 34:
                    self._ensure_embedding_resource_budget_columns(connection)
                if target_version == 43:
                    self._ensure_archive_transfer_claim_columns(connection)
                if target_version == 59:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._upgrade_wallet_submission_consent(connection)
                        connection.execute(
                            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                            (str(target_version),),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 60:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        if (
                            wallet_legacy_approval is not None
                            and wallet_upgrade_fingerprint(connection)
                            != wallet_legacy_approval.expected_fingerprint
                        ):
                            raise RuntimeError("wallet upgrade approval fingerprint mismatch")
                        self._upgrade_wallet_payment_policy_hashes(
                            connection, approved=wallet_legacy_approval is not None
                        )
                        self._upgrade_wallet_ledger_hashes(
                            connection, approved=wallet_legacy_approval is not None
                        )
                        if wallet_legacy_approval is not None:
                            from noyra.wallet.economy import WalletEconomyStore

                            economy = WalletEconomyStore(self)
                            for subject in connection.execute(
                                "SELECT subject_id FROM subject_identity"
                            ):
                                subject_id = str(subject["subject_id"])
                                economy.verify_integrity(subject_id, _connection=connection)
                                connection.execute(
                                    "INSERT INTO audit_records(audit_id,subject_id,action,actor,payload_json,occurred_at) "
                                    "VALUES(?,?,?,?,?,?)",
                                    (
                                        new_id("audit"),
                                        subject_id,
                                        "wallet_legacy_state_authorized",
                                        wallet_legacy_approval.actor,
                                        canonical_json(
                                            {
                                                "expected_fingerprint": wallet_legacy_approval.expected_fingerprint,
                                                "reason": wallet_legacy_approval.reason,
                                                "target_schema": 60,
                                                "historical_authenticity_proven": False,
                                            }
                                        ),
                                        utc_now(),
                                    ),
                                )
                        self._execute_sql_script(connection, migration)
                        connection.execute(
                            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                            (str(target_version),),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 61:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._upgrade_wallet_temporal_hashes(
                            connection, approved=wallet_legacy_approval is not None
                        )
                        self._execute_sql_script(connection, migration)
                        connection.execute(
                            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                            (str(target_version),),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 62:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._upgrade_wallet_receipt_evidence(connection)
                        connection.execute(
                            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                            (str(target_version),),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version in {33, 48, 50, 51, 52}:
                    try:
                        connection.executescript("BEGIN IMMEDIATE;\n" + migration)
                        if target_version == 33:
                            self._backfill_behavior_log_revisions(connection)
                        elif target_version == 48:
                            # This is the only legitimate point at which
                            # historical public-post moderation evidence can
                            # be synthesized.  Reopening a current database
                            # must never launder missing evidence.
                            self._ensure_public_post_moderation_history(connection)
                        elif target_version == 50:
                            self._ensure_transport_endpoint_columns(connection)
                        elif target_version == 51:
                            self._ensure_inbound_reply_columns(connection)
                        else:
                            self._ensure_public_post_evidence_columns(connection)
                            self._upgrade_public_post_evidence(connection)
                        connection.execute(
                            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                            (str(target_version),),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                script = (
                    "BEGIN IMMEDIATE;\n"
                    f"{migration}\n"
                    "UPDATE schema_meta SET value = "
                    f"'{target_version}' WHERE key = 'schema_version';\n"
                    "COMMIT;"
                )
                try:
                    connection.executescript(script)
                except Exception:
                    connection.rollback()
                    raise

    @staticmethod
    def _ensure_project_execution_columns(connection: sqlite3.Connection) -> None:
        columns_by_table = {
            "research_search_runs": ("project_id", "phase_id"),
            "action_deliberation_runs": ("project_id", "phase_id"),
            "actions": ("project_id", "phase_id"),
        }
        references = {
            ("research_search_runs", "project_id"): "REFERENCES autonomous_projects(project_id)",
            ("research_search_runs", "phase_id"): "REFERENCES autonomous_project_phases(phase_id)",
            (
                "action_deliberation_runs",
                "project_id",
            ): "REFERENCES autonomous_projects(project_id)",
            (
                "action_deliberation_runs",
                "phase_id",
            ): "REFERENCES autonomous_project_phases(phase_id)",
            ("actions", "project_id"): "REFERENCES autonomous_projects(project_id)",
            ("actions", "phase_id"): "REFERENCES autonomous_project_phases(phase_id)",
        }
        for table, columns in columns_by_table.items():
            existing = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for column in columns:
                if column in existing:
                    continue
                reference = references[(table, column)]
                connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" TEXT {reference}')

    @staticmethod
    def _ensure_embedding_resource_budget_columns(connection: sqlite3.Connection) -> None:
        existing = {
            str(row[1]) for row in connection.execute('PRAGMA table_info("embedding_resources")')
        }
        columns = {
            "daily_call_limit": ("INTEGER NOT NULL DEFAULT 1000 CHECK (daily_call_limit >= 0)"),
            "daily_token_limit": (
                "INTEGER NOT NULL DEFAULT 5000000 CHECK (daily_token_limit >= 0)"
            ),
            "daily_cost_limit_microusd": (
                "INTEGER NOT NULL DEFAULT 1000000 CHECK (daily_cost_limit_microusd >= 0)"
            ),
            "input_cost_microusd_per_million": (
                "INTEGER NOT NULL DEFAULT 20000 CHECK (input_cost_microusd_per_million >= 0)"
            ),
            "circuit_failure_threshold": (
                "INTEGER NOT NULL DEFAULT 3 CHECK (circuit_failure_threshold BETWEEN 1 AND 20)"
            ),
            "circuit_cooldown_seconds": (
                "REAL NOT NULL DEFAULT 60 "
                "CHECK (circuit_cooldown_seconds >= 1 AND circuit_cooldown_seconds <= 3600)"
            ),
        }
        for column, declaration in columns.items():
            if column not in existing:
                connection.execute(
                    f'ALTER TABLE "embedding_resources" ADD COLUMN "{column}" {declaration}'
                )
