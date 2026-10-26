"""Migrations v006 v017 definitions; execution remains in core.database."""

from __future__ import annotations

MIGRATIONS_V006_V017: dict[int, str] = {
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
}
