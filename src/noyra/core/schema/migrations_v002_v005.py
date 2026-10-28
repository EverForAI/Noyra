"""Migrations v002 v005 definitions; execution remains in core.database."""

from __future__ import annotations

MIGRATIONS_V002_V005: dict[int, str] = {
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
}
