"""Migrations v018 v021 definitions; execution remains in core.database."""

from __future__ import annotations

MIGRATIONS_V018_V021: dict[int, str] = {
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
}
