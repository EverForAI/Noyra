"""Explicit preservation inventory; never an executable cleanup registry."""

from __future__ import annotations

from types import MappingProxyType

# New objects do not inherit review from their prefix or the live schema.
# Preservation baseline is not approval to delete/archive these objects.
_GROUPS: dict[str, str] = {
    "action_capability": """
    action_revisions actions capability_grants capability_uses
    """,
    "admin_auth": """
    admin_login_rate_events admin_sessions
    """,
    "affect_identity": """
    affect_components affect_transitions appraisals fatigue_states fatigue_transitions
    mood_states personality_candidates psychological_snapshots
    relationship_identity_bindings relationship_revisions relationships self_models
    self_modification_proposals self_modification_revisions self_modification_settings
    """,
    "archive_storage": """
    archive_keyring_revisions archive_object_replica_revisions archive_object_replicas
    archive_staging_manifests archive_transfer_queue event_payload_segments
    evidence_row_counts export_jobs route_evidence_row_counts snapshot_archives
    state_snapshots storage_archives storage_usage_samples subject_storage_keys
    """,
    "belief_goal": """
    belief_revisions beliefs entities entity_evidence_links entity_relations
    goal_governance_runs goal_revisions goals outcome_evaluations prediction_reviews
    predictions
    """,
    "cognition_control": """
    action_deliberation_runs autonomy_loop_state autonomy_schedule_state
    cognitive_strategy_profile_revisions cognitive_strategy_profiles
    consciousness_frames epistemic_review_runs genesis_cycles genesis_runs
    genesis_transitions metacognitive_decisions metacognitive_outcomes
    mission_candidate_revisions mission_candidates motivation_reviews
    relationship_social_runs strategy_profile_revisions strategy_profiles
    thought_agenda_items thought_agenda_revisions thought_episodes
    thought_round_closures value_profile_revisions value_profiles workflow_checkpoints
    """,
    "common_knowledge": """
    common_knowledge_evaluation_events common_knowledge_import_provenance
    common_knowledge_imports common_knowledge_packages common_knowledge_peers
    common_knowledge_remote_events common_knowledge_sync_events
    common_knowledge_sync_runs common_knowledge_trusted_keys common_knowledge_versions
    """,
    "communication": """
    email_ingress_evidence inbound_identity_challenges interaction_bindings
    interaction_decisions interaction_deliveries interaction_delivery_reconciliations
    interaction_inbound_events interaction_threads interaction_transports interactions
    private_channel_routes private_conversation_messages private_conversations
    private_message_sequences private_model_sources private_web_deliveries
    """,
    "event_audit": """
    audit_records causal_links event_chain_roots events
    """,
    "health_retention": """
    backup_retention_policies backup_retention_runs provider_health_buckets
    provider_health_state retention_policies retention_runs
    """,
    "kernel_schema": """
    persistent_features runtime_state schema_meta subject_identity
    """,
    "memory": """
    memories memory_accesses memory_block_revisions memory_blocks
    memory_consolidation_members memory_consolidation_runs memory_embedding_inputs
    memory_embeddings memory_fts memory_fts_config memory_fts_content memory_fts_data
    memory_fts_docsize memory_fts_idx memory_integration_revisions memory_integrations
    memory_revisions
    """,
    "migration": """
    migration_audit_events migration_backup_registry migration_epochs migration_policies
    migration_policy_review_modes migration_proposal_approvals migration_proposals
    migration_rejections migration_self_attestations migration_target_challenges
    migration_targets migration_tasks
    """,
    "model_resource": """
    cognitive_resource_group_revisions cognitive_resource_groups
    cognitive_resource_key_events cognitive_resource_keys
    cognitive_resource_limit_policies cognitive_resource_limit_revisions
    cognitive_route_attempts cognitive_route_decisions cognitive_route_outcomes
    embedding_circuit_states embedding_circuit_transitions embedding_resources
    embedding_usage_entries model_attempts model_calls model_capability_profiles
    resource_pool_pressures waiting_cognitive_task_revisions waiting_cognitive_tasks
    """,
    "participant": """
    participant_accounts participant_api_credentials participant_applications
    participant_auth_limits participant_credentials participant_entities
    participant_message_links participant_notification_reads participant_post_links
    participant_sessions participant_sources participant_submissions
    """,
    "privacy": """
    private_erasure_jobs private_erasure_receipts secret_cleanup_queue
    secret_file_intents
    """,
    "projects": """
    autonomous_project_assistance_requests autonomous_project_execution_clock_events
    autonomous_project_execution_revisions autonomous_project_executions
    autonomous_project_phase_revisions autonomous_project_phases
    autonomous_project_resource_uses autonomous_project_reviews
    autonomous_project_revisions autonomous_project_sleep_reflections
    autonomous_projects
    """,
    "public": """
    behavior_log_revisions behavior_logs public_diary_entries
    public_post_captcha_challenges public_post_captcha_issue_events public_post_controls
    public_post_length_policies public_post_moderation_events public_post_rate_events
    public_posts public_publication_revisions public_publications public_sitemap_slots
    """,
    "research_world": """
    browser_search_reservations observation_content_segments
    observation_status_transitions observations research_search_runs
    search_provider_configs search_provider_controls search_provider_revisions
    search_provider_routing search_provider_uses search_routing_settings
    world_claim_revisions world_claims world_feed_seen world_poll_state
    world_source_revisions world_sources
    """,
    "sleep": """
    retry_blocks sleep_integrations sleep_reflections sleep_runs sleep_transitions
    """,
    "training": """
    training_exports training_policies training_records
    """,
    "wallet": """
    wallet_address_revisions wallet_addresses wallet_asset_revisions wallet_assets
    wallet_balance_acquisition_attempts wallet_balance_acquisition_runs
    wallet_balance_snapshots wallet_bounties wallet_bounty_submissions
    wallet_ledger_entries wallet_ledger_journals wallet_network_revisions
    wallet_networks wallet_payment_execution_attempts wallet_payment_executions
    wallet_payment_orders wallet_payment_policies wallet_payment_reconciliation_events
    wallet_reward_incidents wallet_reward_submission_links wallet_reward_workflows
    """,
}

_domains: dict[str, str] = {}
for _domain, _tables in _GROUPS.items():
    for _table in _tables.split():
        if _table in _domains:
            raise RuntimeError("duplicate lifecycle preservation entry: " + _table)
        _domains[_table] = _domain
TABLE_DOMAINS = MappingProxyType(_domains)

# Existing domain-specific expiry/revocation is documented, not delegated to
# RetentionManager.run_batch. These entries cannot expand that delete list.
DOMAIN_CONTRACTS = MappingProxyType(
    {
        "admin_sessions": "authentication_session_expiry",
        "admin_login_rate_events": "authentication_rate_window",
        "participant_sessions": "participant_session_expiry",
        "participant_api_credentials": "explicit_credential_revocation",
        "participant_auth_limits": "participant_authentication_window",
        "public_post_captcha_challenges": "captcha_expiry_and_consumption",
        "public_post_captcha_issue_events": "captcha_rate_window",
        "public_post_rate_events": "public_post_rate_window",
        "world_feed_seen": "versioned_first_seen_eviction",
    }
)

# Physical payload handling retains logical evidence and original domain
# ownership. FTS shadows are always managed by the owning virtual table.
REPRESENTATION_CONTRACTS = MappingProxyType(
    {
        "events": "encrypted_event_payload_archive",
        "observations": "verified_observation_payload_archive",
        "state_snapshots": "verified_snapshot_compaction",
        "model_calls": "lossless_model_payload_compaction",
        "memory_fts": "derived_index_rebuild",
        "memory_fts_config": "virtual_table_owned_shadow",
        "memory_fts_content": "virtual_table_owned_shadow",
        "memory_fts_data": "virtual_table_owned_shadow",
        "memory_fts_docsize": "virtual_table_owned_shadow",
        "memory_fts_idx": "virtual_table_owned_shadow",
    }
)
