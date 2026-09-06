from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.runtime_export import (
    _PARENT_TABLES_V33,
    RuntimeExportArtifact,
    RuntimeLogExporter,
)
from noyra.core.types import content_hash, utc_now
from support.historical import (
    HistoricalFixture,
    load_historical_fixtures,
    materialize_historical_database,
)

HISTORICAL_FIXTURES = load_historical_fixtures(
    Path(__file__).parent / "fixtures" / "historical" / "manifest.json"
)

# This contract is intentionally duplicated in the test rather than derived from
# the implementation.  The runtime-export graph must keep every revision/child
# table attached to its directly subject-owned parent, even when tables happen to
# share an ID column.
EXPECTED_PARENT_GRAPH_V33: dict[str, tuple[str, str, str]] = {
    "autonomous_project_execution_revisions": (
        "autonomous_project_executions",
        "execution_id",
        "execution_id",
    ),
    "autonomous_project_phase_revisions": (
        "autonomous_project_phases",
        "phase_id",
        "phase_id",
    ),
    "autonomous_project_revisions": ("autonomous_projects", "project_id", "project_id"),
    "belief_revisions": ("beliefs", "belief_id", "belief_id"),
    "cognitive_resource_group_revisions": (
        "cognitive_resource_groups",
        "group_id",
        "group_id",
    ),
    "cognitive_strategy_profile_revisions": (
        "cognitive_strategy_profiles",
        "profile_id",
        "profile_id",
    ),
    "genesis_cycles": ("genesis_runs", "run_id", "run_id"),
    "genesis_transitions": ("genesis_runs", "run_id", "run_id"),
    "goal_revisions": ("goals", "goal_id", "goal_id"),
    "interaction_decisions": ("interactions", "interaction_id", "interaction_id"),
    "memory_block_revisions": ("memory_blocks", "block_id", "block_id"),
    "memory_consolidation_members": (
        "memory_consolidation_runs",
        "consolidation_id",
        "consolidation_id",
    ),
    "memory_integration_revisions": (
        "memory_integrations",
        "integration_id",
        "integration_id",
    ),
    "memory_revisions": ("memories", "memory_id", "memory_id"),
    "mission_candidate_revisions": ("mission_candidates", "mission_id", "mission_id"),
    "observation_status_transitions": ("observations", "observation_id", "observation_id"),
    "prediction_reviews": ("predictions", "prediction_id", "prediction_id"),
    "relationship_revisions": ("relationships", "relationship_id", "relationship_id"),
    "search_provider_revisions": ("search_provider_configs", "config_id", "config_id"),
    "sleep_integrations": ("sleep_runs", "sleep_id", "sleep_id"),
    "sleep_reflections": ("sleep_runs", "sleep_id", "sleep_id"),
    "sleep_transitions": ("sleep_runs", "sleep_id", "sleep_id"),
    "strategy_profile_revisions": ("strategy_profiles", "profile_id", "profile_id"),
    "thought_agenda_revisions": ("thought_agenda_items", "agenda_id", "agenda_id"),
    "value_profile_revisions": ("value_profiles", "value_id", "value_id"),
    "waiting_cognitive_task_revisions": (
        "waiting_cognitive_tasks",
        "task_id",
        "task_id",
    ),
    "world_claim_revisions": ("world_claims", "claim_id", "claim_id"),
    "world_source_revisions": ("world_sources", "source_id", "source_id"),
}


def _insert_goal_history(connection: sqlite3.Connection, subject_id: str, suffix: str) -> None:
    now = utc_now()
    goal_id = f"goal-{suffix}"
    title = f"Goal {suffix}"
    state_hash = content_hash({"goal": goal_id, "status": "candidate"})
    connection.execute(
        """INSERT INTO goals(
            goal_id, subject_id, title, description, origin, status, priority,
            commitment, progress, emotional_pressure, state_hash, current_revision,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'self', 'candidate', 0.5, 0.5, 0, 0, ?, 1, ?, ?)""",
        (goal_id, subject_id, title, "bounded export fixture", state_hash, now, now),
    )
    connection.execute(
        """INSERT INTO goal_revisions(
            revision_id, goal_id, revision_number, title, description, status,
            priority, commitment, progress, emotional_pressure, state_hash, reason,
            causal_source_ids_json, created_at
        ) VALUES (?, ?, 1, ?, ?, 'candidate', 0.5, 0.5, 0, 0, ?, ?, '[]', ?)""",
        (
            f"goal-revision-{suffix}",
            goal_id,
            title,
            "bounded export fixture",
            state_hash,
            "fixture",
            now,
        ),
    )


def _insert_self_modification_revision(
    connection: sqlite3.Connection, subject_id: str, suffix: str
) -> None:
    now = utc_now()
    proposal_id = f"proposal-{suffix}"
    connection.execute(
        """INSERT INTO self_modification_proposals(
            proposal_id, subject_id, setting_key, old_value_json, proposed_value_json,
            reason, evidence_ids_json, risk_score, status, validation_json,
            simulation_json, state_hash, created_at, updated_at
        ) VALUES (?, ?, 'fixture.setting', '0', '1', 'fixture', '[]', 0.1,
                  'proposed', '{}', '{}', ?, ?, ?)""",
        (proposal_id, subject_id, "f" * 64, now, now),
    )
    connection.execute(
        """INSERT INTO self_modification_revisions(
            revision_id, subject_id, proposal_id, setting_key, old_value_json,
            new_value_json, action, reason, state_hash, created_at
        ) VALUES (?, ?, ?, 'fixture.setting', '0', '1', 'apply', 'fixture', ?, ?)""",
        (f"self-revision-{suffix}", subject_id, proposal_id, "f" * 64, now),
    )


def _insert_memory_graph(connection: sqlite3.Connection, subject_id: str, suffix: str) -> None:
    now = utc_now()
    memory_id = f"memory-{suffix}"
    consolidation_id = f"consolidation-{suffix}"
    memory_hash = content_hash({"memory": memory_id})
    connection.execute(
        """INSERT INTO memories(
            memory_id, subject_id, memory_type, content, content_hash, state_hash,
            salience, confidence, privacy_level, status, current_revision,
            created_at, updated_at
        ) VALUES (?, ?, 'episodic', ?, ?, ?, 0.5, 0.5, 'private', 'active', 1, ?, ?)""",
        (memory_id, subject_id, "fixture memory", memory_hash, "m" * 64, now, now),
    )
    connection.execute(
        """INSERT INTO memory_revisions(
            revision_id, memory_id, revision_number, content, content_hash,
            state_hash, salience, confidence, status, reason, source_event_ids_json,
            created_at
        ) VALUES (?, ?, 1, ?, ?, ?, 0.5, 0.5, 'active', 'fixture', '[]', ?)""",
        (f"memory-revision-{suffix}", memory_id, "fixture memory", memory_hash, "m" * 64, now),
    )
    connection.execute(
        """INSERT INTO memory_consolidation_runs(
            consolidation_id, subject_id, idempotency_key, status, reviewed_count,
            archived_memory_ids_json, strengthened_memory_ids_json,
            summary_memory_ids_json, summary, state_hash, created_at
        ) VALUES (?, ?, ?, 'committed', 1, '[]', '[]', '[]', 'fixture', ?, ?)""",
        (consolidation_id, subject_id, f"consolidate-{suffix}", "c" * 64, now),
    )
    connection.execute(
        """INSERT INTO memory_consolidation_members(
            member_id, consolidation_id, source_memory_id, result_memory_id,
            disposition, reason, state_hash, created_at
        ) VALUES (?, ?, ?, NULL, 'retained', 'fixture', ?, ?)""",
        (f"member-{suffix}", consolidation_id, memory_id, "n" * 64, now),
    )


def _insert_audit_named_graph(
    connection: sqlite3.Connection, subject_id: str, suffix: str
) -> dict[str, str]:
    """Insert one row in every parent family called out by the P1-04 audit.

    The fixture deliberately uses direct SQL.  Store-level helpers often omit
    historical rows or normalize IDs, which would make this contract unable to
    catch a future regression in the ownership predicates themselves.
    """

    now = utc_now()
    goal_suffix = f"project-{suffix}"
    _insert_goal_history(connection, subject_id, goal_suffix)
    goal_id = f"goal-{goal_suffix}"
    mission_id = f"mission-{suffix}"
    connection.execute(
        """INSERT INTO mission_candidates(
            mission_id, subject_id, title, statement, horizon, commitment,
            confidence, status, source_value_ids_json, source_event_ids_json,
            source_memory_ids_json, source_belief_ids_json, source_goal_ids_json,
            state_hash, current_revision, created_at, updated_at
        ) VALUES (?, ?, 'fixture mission', 'fixture statement', 'long_term',
                  0.5, 0.5, 'candidate', '[]', '[]', '[]', '[]', ?, ?, 1, ?, ?)""",
        (mission_id, subject_id, json.dumps([goal_id]), "m" * 64, now, now),
    )
    mission_revision_id = f"mission-revision-{suffix}"
    connection.execute(
        """INSERT INTO mission_candidate_revisions(
            revision_id, mission_id, revision_number, title, statement, horizon,
            commitment, confidence, status, source_value_ids_json,
            source_event_ids_json, source_memory_ids_json, source_belief_ids_json,
            source_goal_ids_json, reason, state_hash, created_at
        ) VALUES (?, ?, 1, 'fixture mission', 'fixture statement', 'long_term',
                  0.5, 0.5, 'candidate', '[]', '[]', '[]', '[]', ?, 'fixture', ?, ?)""",
        (mission_revision_id, mission_id, json.dumps([goal_id]), "m" * 64, now),
    )

    call_id = f"formation-call-{suffix}"
    connection.execute(
        """INSERT INTO model_calls(
            call_id, subject_id, provider, model, purpose, request_hash,
            idempotency_key, status, response_json, response_hash, usage_estimated,
            error_code, created_at, completed_at
        ) VALUES (?, ?, 'fixture', 'fixture-model', 'fixture-formation', ?, ?,
                  'prepared', NULL, NULL, 0, NULL, ?, NULL)""",
        (call_id, subject_id, "q" * 64, f"formation-{suffix}", now),
    )

    project_id = f"project-{suffix}"
    connection.execute(
        """INSERT INTO autonomous_projects(
            project_id, subject_id, goal_id, formation_call_id, project_key,
            project_type, title, purpose, deliverable, acceptance_criteria_json,
            size_class, estimated_duration_hours, status, progress, current_phase_id,
            max_cycles, max_model_calls, max_searches, max_external_actions,
            max_storage_bytes, source_event_ids_json, source_value_ids_json,
            source_mission_id, state_hash, current_revision, created_at, updated_at,
            completed_at
        ) VALUES (?, ?, ?, ?, ?, 'research', 'fixture project', 'fixture purpose',
                  'fixture deliverable', '{}', 'small', 1.0, 'planned', 0, NULL,
                  1, 1, 0, 0, 0, '[]', '[]', ?, ?, 1, ?, ?, NULL)""",
        (
            project_id,
            subject_id,
            goal_id,
            call_id,
            f"project-key-{suffix}",
            mission_id,
            "p" * 64,
            now,
            now,
        ),
    )
    project_revision_id = f"project-revision-{suffix}"
    connection.execute(
        """INSERT INTO autonomous_project_revisions(
            revision_id, project_id, revision_number, status, progress,
            current_phase_id, max_cycles, max_model_calls, max_searches,
            max_external_actions, max_storage_bytes, reason, evidence_event_ids_json,
            state_hash, created_at
        ) VALUES (?, ?, 1, 'planned', 0, NULL, 1, 1, 0, 0, 0, 'fixture', '[]', ?, ?)""",
        (project_revision_id, project_id, "r" * 64, now),
    )

    phase_id = f"phase-{suffix}"
    connection.execute(
        """INSERT INTO autonomous_project_phases(
            phase_id, project_id, subject_id, phase_key, position, title, objective,
            output_type, acceptance_criteria_json, dependency_keys_json, status,
            attempt_count, no_progress_count, state_hash, current_revision,
            created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, ?, 1, 'fixture phase', 'fixture objective', 'research_note',
                  '{}', '[]', 'pending', 0, 0, ?, 1, ?, ?, NULL)""",
        (phase_id, project_id, subject_id, f"phase-key-{suffix}", "h" * 64, now, now),
    )
    phase_revision_id = f"phase-revision-{suffix}"
    connection.execute(
        """INSERT INTO autonomous_project_phase_revisions(
            revision_id, phase_id, revision_number, status, attempt_count,
            no_progress_count, reason, evidence_event_ids_json, state_hash, created_at
        ) VALUES (?, ?, 1, 'pending', 0, 0, 'fixture', '[]', ?, ?)""",
        (phase_revision_id, phase_id, "i" * 64, now),
    )

    execution_id = f"execution-{suffix}"
    connection.execute(
        """INSERT INTO autonomous_project_executions(
            execution_id, subject_id, project_id, phase_id, execution_key,
            execution_type, workflow, status, research_id, action_id, model_call_id,
            artifact_path, artifact_hash, result_hash, acceptance_json, error_code,
            created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, 'research_note', 'fixture-workflow', 'prepared',
                  NULL, NULL, NULL, NULL, NULL, NULL, '{}', NULL, ?, ?, NULL)""",
        (execution_id, subject_id, project_id, phase_id, f"execution-key-{suffix}", now, now),
    )
    execution_revision_id = f"execution-revision-{suffix}"
    connection.execute(
        """INSERT INTO autonomous_project_execution_revisions(
            revision_id, execution_id, status, result_hash, reason, state_hash, created_at
        ) VALUES (?, ?, 'prepared', NULL, 'fixture', ?, ?)""",
        (execution_revision_id, execution_id, "e" * 64, now),
    )

    interaction_id = f"interaction-{suffix}"
    connection.execute(
        """INSERT INTO interactions(
            interaction_id, subject_id, direction, kind, channel, counterparty,
            content, content_hash, related_interaction_id, idempotency_key, status,
            rationale, state_hash, created_at, decided_at
        ) VALUES (?, ?, 'incoming', 'human_message', 'fixture', 'fixture-user',
                  'fixture interaction', ?, NULL, ?, 'offered', NULL, ?, ?, NULL)""",
        (interaction_id, subject_id, "a" * 64, f"interaction-{suffix}", "b" * 64, now),
    )
    decision_id = f"interaction-decision-{suffix}"
    connection.execute(
        """INSERT INTO interaction_decisions(
            decision_id, interaction_id, from_status, to_status, rationale, actor,
            state_hash, created_at
        ) VALUES (?, ?, 'offered', 'accepted', 'fixture', 'fixture', ?, ?)""",
        (decision_id, interaction_id, "d" * 64, now),
    )

    belief_id = f"belief-{suffix}"
    connection.execute(
        """INSERT INTO beliefs(
            belief_id, subject_id, proposition, proposition_hash, state_hash,
            confidence, scope, status, current_revision, created_at, reviewed_at
        ) VALUES (?, ?, 'fixture belief', ?, ?, 0.5, 'fixture', 'active', 1, ?, ?)""",
        (belief_id, subject_id, "b" * 64, "c" * 64, now, now),
    )
    belief_revision_id = f"belief-revision-{suffix}"
    connection.execute(
        """INSERT INTO belief_revisions(
            revision_id, belief_id, revision_number, proposition, proposition_hash,
            state_hash, confidence, status, supporting_event_ids_json,
            counter_event_ids_json, reason, created_at
        ) VALUES (?, ?, 1, 'fixture belief', ?, ?, 0.5, 'active', '[]', '[]',
                  'fixture', ?)""",
        (belief_revision_id, belief_id, "b" * 64, "c" * 64, now),
    )

    source_id = f"source-{suffix}"
    connection.execute(
        """INSERT INTO world_sources(
            source_id, subject_id, name, url, source_type, trust_score, status,
            state_hash, current_revision, created_at, updated_at
        ) VALUES (?, ?, 'fixture source', ?, 'web', 0.5, 'active', ?, 1, ?, ?)""",
        (source_id, subject_id, f"https://fixture.invalid/{suffix}", "s" * 64, now, now),
    )
    source_revision_id = f"source-revision-{suffix}"
    connection.execute(
        """INSERT INTO world_source_revisions(
            revision_id, source_id, revision_number, trust_score, status, reason,
            state_hash, created_at
        ) VALUES (?, ?, 1, 0.5, 'active', 'fixture', ?, ?)""",
        (source_revision_id, source_id, "t" * 64, now),
    )

    prediction_id = f"prediction-{suffix}"
    connection.execute(
        """INSERT INTO predictions(
            prediction_id, subject_id, idempotency_key, statement, statement_hash,
            probability, target_at, resolution_criteria, status, outcome, brier_score,
            state_hash, created_at, resolved_at
        ) VALUES (?, ?, ?, 'fixture prediction', ?, 0.5, ?, 'fixture criteria',
                  'open', NULL, NULL, ?, ?, NULL)""",
        (prediction_id, subject_id, f"prediction-{suffix}", "y" * 64, now, "z" * 64, now),
    )
    prediction_review_id = f"prediction-review-{suffix}"
    connection.execute(
        """INSERT INTO prediction_reviews(
            review_id, prediction_id, outcome, evidence_observation_ids_json,
            rationale, resulting_status, brier_score, state_hash, created_at
        ) VALUES (?, ?, NULL, '[]', 'fixture', 'open', NULL, ?, ?)""",
        (prediction_review_id, prediction_id, "v" * 64, now),
    )

    relationship_id = f"relationship-{suffix}"
    connection.execute(
        """INSERT INTO relationships(
            relationship_id, subject_id, entity_type, entity_key, display_name,
            trust, affinity, conflict, familiarity, boundaries_json, boundaries_hash,
            state_hash, current_revision, created_at, updated_at
        ) VALUES (?, ?, 'person', ?, 'Fixture Contact', 0.1, 0.2, 0, 0.3,
                  '{}', ?, ?, 1, ?, ?)""",
        (relationship_id, subject_id, f"fixture-contact-{suffix}", "w" * 64, "x" * 64, now, now),
    )
    relationship_revision_id = f"relationship-revision-{suffix}"
    connection.execute(
        """INSERT INTO relationship_revisions(
            revision_id, relationship_id, revision_number, trust, affinity, conflict,
            familiarity, boundaries_json, boundaries_hash, state_hash, reason,
            source_event_ids_json, created_at
        ) VALUES (?, ?, 1, 0.1, 0.2, 0, 0.3, '{}', ?, ?, 'fixture', '[]', ?)""",
        (relationship_revision_id, relationship_id, "w" * 64, "x" * 64, now),
    )

    profile_id = f"strategy-profile-{suffix}"
    connection.execute(
        """INSERT INTO strategy_profiles(
            profile_id, subject_id, goal_id, strategy_id, strategy_kind, method,
            attempts, successes, failures, inconclusive, confidence, last_outcome,
            state_hash, current_revision, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'research', 'fixture method', 0, 0, 0, 0, 0.5,
                  'unknown', ?, 1, ?, ?)""",
        (profile_id, subject_id, goal_id, f"strategy-{suffix}", "g" * 64, now, now),
    )
    profile_revision_id = f"strategy-profile-revision-{suffix}"
    connection.execute(
        """INSERT INTO strategy_profile_revisions(
            revision_id, profile_id, revision_number, attempts, successes, failures,
            inconclusive, confidence, last_outcome, evidence_type, evidence_id,
            reason, state_hash, created_at
        ) VALUES (?, ?, 1, 0, 0, 0, 0, 0.5, 'unknown', 'fixture', ?, 'fixture', ?, ?)""",
        (profile_revision_id, profile_id, prediction_id, "j" * 64, now),
    )

    sleep_id = f"sleep-{suffix}"
    sleep_event_id = f"sleep-event-{suffix}"
    connection.execute(
        """INSERT INTO events(
            event_id, subject_id, event_type, source, occurred_at, observed_at,
            payload_json, payload_hash, privacy_level, causal_parent_ids_json,
            processing_status
        ) VALUES (?, ?, 'fixture_sleep', 'fixture', ?, ?, '{}', ?, 'private', '[]', 'recorded')""",
        (sleep_event_id, subject_id, now, now, "k" * 64),
    )
    connection.execute(
        """INSERT INTO sleep_runs(
            sleep_id, subject_id, status, trigger_type, trigger_reason, emergency,
            pre_sleep_fatigue, wake_after, reflection_event_id, checkpoint_id,
            state_hash, version, started_at, updated_at, completed_at
        ) VALUES (?, ?, 'complete', 'subject_choice', 'fixture', 0, 10, NULL, ?,
                  NULL, ?, 1, ?, ?, ?)""",
        (sleep_id, subject_id, sleep_event_id, "l" * 64, now, now, now),
    )
    sleep_transition_id = f"sleep-transition-{suffix}"
    connection.execute(
        """INSERT INTO sleep_transitions(
            transition_id, sleep_id, from_status, to_status, reason, state_hash, created_at
        ) VALUES (?, ?, 'winding_down', 'complete', 'fixture', ?, ?)""",
        (sleep_transition_id, sleep_id, "n" * 64, now),
    )
    sleep_reflection_id = f"sleep-reflection-{suffix}"
    connection.execute(
        """INSERT INTO sleep_reflections(
            reflection_id, sleep_id, event_id, summary, facts_json, contradictions_json,
            prediction_errors_json, unresolved_questions_json, public_diary_candidate,
            plan_json, plan_hash, created_at
        ) VALUES (?, ?, ?, 'fixture reflection', '[]', '[]', '[]', '[]', NULL,
                  '{}', ?, ?)""",
        (sleep_reflection_id, sleep_id, sleep_event_id, "o" * 64, now),
    )
    sleep_integration_id = f"sleep-integration-{suffix}"
    connection.execute(
        """INSERT INTO sleep_integrations(
            integration_id, sleep_id, integration_type, target_id, operation,
            source_ids_json, result_hash, created_at
        ) VALUES (?, ?, 'belief', ?, 'fixture', '[]', ?, ?)""",
        (sleep_integration_id, sleep_id, belief_id, "u" * 64, now),
    )

    return {
        "autonomous_projects": project_id,
        "autonomous_project_revisions": project_revision_id,
        "autonomous_project_phases": phase_id,
        "autonomous_project_phase_revisions": phase_revision_id,
        "autonomous_project_executions": execution_id,
        "autonomous_project_execution_revisions": execution_revision_id,
        "interactions": interaction_id,
        "interaction_decisions": decision_id,
        "beliefs": belief_id,
        "belief_revisions": belief_revision_id,
        "world_sources": source_id,
        "world_source_revisions": source_revision_id,
        "predictions": prediction_id,
        "prediction_reviews": prediction_review_id,
        "relationships": relationship_id,
        "relationship_revisions": relationship_revision_id,
        "strategy_profiles": profile_id,
        "strategy_profile_revisions": profile_revision_id,
        "mission_candidates": mission_id,
        "mission_candidate_revisions": mission_revision_id,
        "sleep_runs": sleep_id,
        "sleep_transitions": sleep_transition_id,
        "sleep_reflections": sleep_reflection_id,
        "sleep_integrations": sleep_integration_id,
    }


def _read_artifact(
    artifact: RuntimeExportArtifact,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    content = artifact.content
    with zipfile.ZipFile(BytesIO(content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        rows: dict[str, list[dict[str, Any]]] = {}
        for entry in manifest["tables"]:
            rows[entry["name"]] = [
                json.loads(line) for line in archive.read(entry["file"]).splitlines()
            ]
    return manifest, rows


def test_parent_graph_contract_is_explicit_for_every_revision_family() -> None:
    assert len(EXPECTED_PARENT_GRAPH_V33) == 28
    assert set(_PARENT_TABLES_V33) == set(EXPECTED_PARENT_GRAPH_V33)
    for table, expected in EXPECTED_PARENT_GRAPH_V33.items():
        rule = _PARENT_TABLES_V33[table]
        assert rule.mode == "parent"
        assert (rule.parent_table, rule.parent_key, rule.child_key) == expected


def test_audit_named_parent_families_and_common_knowledge_are_subject_isolated(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "audit-families.sqlite3")
    subject_a = "Noyra-families-a"
    subject_b = "Noyra-families-b"
    subject_unrelated = "Noyra-families-unrelated"
    identities = IdentityStore(database)
    for subject_id in (subject_a, subject_b, subject_unrelated):
        identities.ensure(subject_id, content_hash({"subject": subject_id}))

    with database.transaction() as connection:
        expected_a = _insert_audit_named_graph(connection, subject_a, "a")
        _insert_audit_named_graph(connection, subject_b, "b")
        _insert_audit_named_graph(connection, subject_unrelated, "unrelated")

        connection.executemany(
            """INSERT INTO common_knowledge_trusted_keys(
                key_id, public_key, label, status, created_at, revoked_at
            ) VALUES (?, ?, ?, 'active', ?, NULL)""",
            [
                ("key-a", "public-a", "fixture A", utc_now()),
                ("key-b", "public-b", "fixture B", utc_now()),
                ("key-unrelated", "public-unrelated", "fixture unrelated", utc_now()),
            ],
        )
        connection.executemany(
            """INSERT INTO common_knowledge_packages(
                package_id, publisher_subject_id, scope, title, summary, payload_json,
                payload_hash, signature, key_id, version, status, created_at, revoked_at
            ) VALUES (?, ?, 'reference', ?, 'fixture summary', '{}', ?, 'signature', ?, 1,
                      'published', ?, NULL)""",
            [
                ("package-published-a", subject_a, "published A", "h" * 64, "key-a", utc_now()),
                ("package-published-b", subject_b, "published B", "i" * 64, "key-b", utc_now()),
                (
                    "package-imported-a",
                    subject_b,
                    "imported by A",
                    "j" * 64,
                    "key-b",
                    utc_now(),
                ),
                (
                    "package-unrelated",
                    subject_unrelated,
                    "unrelated",
                    "k" * 64,
                    "key-unrelated",
                    utc_now(),
                ),
            ],
        )
        connection.executemany(
            """INSERT INTO common_knowledge_imports(
                import_id, package_id, subject_id, status, reason, imported_at
            ) VALUES (?, ?, ?, 'accepted', 'fixture', ?)""",
            [
                ("import-a", "package-imported-a", subject_a, utc_now()),
                ("import-b", "package-published-b", subject_b, utc_now()),
                ("import-unrelated", "package-unrelated", subject_unrelated, utc_now()),
            ],
        )
        # schema_meta is global metadata, but only the schema version is part of
        # the portable runtime contract.  This marker must never cross the export
        # boundary even though it is unrelated to any subject.
        connection.execute(
            "INSERT INTO schema_meta(key, value) "
            "VALUES ('runtime_export_test_marker', 'must-not-export')"
        )

    manifest, rows = _read_artifact(RuntimeLogExporter(database).export(subject_a, actor="test"))
    primary_keys = {
        "autonomous_projects": "project_id",
        "autonomous_project_revisions": "revision_id",
        "autonomous_project_phases": "phase_id",
        "autonomous_project_phase_revisions": "revision_id",
        "autonomous_project_executions": "execution_id",
        "autonomous_project_execution_revisions": "revision_id",
        "interactions": "interaction_id",
        "interaction_decisions": "decision_id",
        "beliefs": "belief_id",
        "belief_revisions": "revision_id",
        "world_sources": "source_id",
        "world_source_revisions": "revision_id",
        "predictions": "prediction_id",
        "prediction_reviews": "review_id",
        "relationships": "relationship_id",
        "relationship_revisions": "revision_id",
        "strategy_profiles": "profile_id",
        "strategy_profile_revisions": "revision_id",
        "mission_candidates": "mission_id",
        "mission_candidate_revisions": "revision_id",
        "sleep_runs": "sleep_id",
        "sleep_transitions": "transition_id",
        "sleep_reflections": "reflection_id",
        "sleep_integrations": "integration_id",
    }
    for table, expected_id in expected_a.items():
        key = primary_keys[table]
        assert {row[key] for row in rows[table]} == {expected_id}
        assert expected_id.endswith("-a")
        serialized = json.dumps(rows[table], sort_keys=True).encode("utf-8")
        assert b"-b" not in serialized
        assert b"-unrelated" not in serialized

    assert {row["package_id"] for row in rows["common_knowledge_packages"]} == {
        "package-published-a",
        "package-imported-a",
    }
    assert {row["import_id"] for row in rows["common_knowledge_imports"]} == {"import-a"}
    assert {row["key_id"] for row in rows["common_knowledge_trusted_keys"]} == {"key-a", "key-b"}
    assert {row["key"] for row in rows["schema_meta"]} == {"schema_version"}
    assert all(row["value"] != "must-not-export" for row in rows["schema_meta"])
    assert manifest["ownership_graph"]["version"] == 1


def test_explicit_graph_reconciles_every_table_and_prevents_id_collision_leaks(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "runtime.sqlite3")
    subject_a = "Noyra-export-a"
    subject_b = "Noyra-export-b"
    identities = IdentityStore(database)
    identities.ensure(subject_a, content_hash({"subject": subject_a}))
    identities.ensure(subject_b, content_hash({"subject": subject_b}))
    with database.transaction() as connection:
        _insert_goal_history(connection, subject_a, "a")
        _insert_goal_history(connection, subject_b, "b")
        _insert_self_modification_revision(connection, subject_a, "a")
        _insert_self_modification_revision(connection, subject_b, "b")
        _insert_memory_graph(connection, subject_a, "a")
        _insert_memory_graph(connection, subject_b, "b")

    exporter = RuntimeLogExporter(database)
    artifact_a = exporter.export(subject_a, actor="test")
    manifest_a, rows_a = _read_artifact(artifact_a)
    with database.connection() as connection:
        actual_tables = set(exporter._tables(connection))

    inventory = {entry["name"]: entry for entry in manifest_a["table_inventory"]}
    assert set(inventory) == actual_tables
    assert manifest_a["schema_version"] == CURRENT_SCHEMA_VERSION
    assert manifest_a["ownership_graph"] == {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "version": 1,
    }
    assert {entry["name"] for entry in manifest_a["tables"]} == {
        name for name, entry in inventory.items() if entry["status"] == "exported"
    }
    assert {name for name, entry in inventory.items() if entry["status"] == "skipped"} == {
        "memory_fts",
        "memory_fts_config",
        "memory_fts_content",
        "memory_fts_data",
        "memory_fts_docsize",
        "memory_fts_idx",
    }
    for entry in manifest_a["tables"]:
        assert entry["expected_rows"] == entry["exported_rows"] == entry["rows"]
        assert entry["referential_gaps"] == 0
        assert inventory[entry["name"]]["reconciliation"] == "matched"
        assert inventory[entry["name"]]["referential_gaps"] == 0
    assert (
        manifest_a["expected_total_rows"]
        == manifest_a["total_rows"]
        == sum(entry["rows"] for entry in manifest_a["tables"])
    )

    assert {row["goal_id"] for row in rows_a["goal_revisions"]} == {"goal-a"}
    assert {row["revision_id"] for row in rows_a["self_modification_revisions"]} == {
        "self-revision-a"
    }
    assert {row["memory_id"] for row in rows_a["memory_revisions"]} == {"memory-a"}
    assert {row["consolidation_id"] for row in rows_a["memory_consolidation_members"]} == {
        "consolidation-a"
    }
    exported_bytes = json.dumps(rows_a, sort_keys=True).encode("utf-8")
    assert b"goal-b" not in exported_bytes
    assert b"self-revision-b" not in exported_bytes
    assert b"memory-b" not in exported_bytes


def test_cross_subject_parent_reference_fails_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "cross-subject.sqlite3")
    subject_a = "Noyra-cross-a"
    subject_b = "Noyra-cross-b"
    identities = IdentityStore(database)
    identities.ensure(subject_a, content_hash({"subject": subject_a}))
    identities.ensure(subject_b, content_hash({"subject": subject_b}))
    with database.transaction() as connection:
        _insert_memory_graph(connection, subject_b, "b")
        now = utc_now()
        connection.execute(
            """INSERT INTO memory_consolidation_runs(
                consolidation_id, subject_id, idempotency_key, status, reviewed_count,
                archived_memory_ids_json, strengthened_memory_ids_json,
                summary_memory_ids_json, summary, state_hash, created_at
            ) VALUES ('consolidation-a', ?, 'cross', 'committed', 1, '[]', '[]', '[]',
                      'cross', ?, ?)""",
            (subject_a, "c" * 64, now),
        )
        connection.execute(
            """INSERT INTO memory_consolidation_members(
                member_id, consolidation_id, source_memory_id, result_memory_id,
                disposition, reason, state_hash, created_at
            ) VALUES ('member-cross', 'consolidation-a', 'memory-b', NULL,
                      'retained', 'cross', ?, ?)""",
            ("n" * 64, now),
        )

    with pytest.raises(RuntimeError, match="foreign references leave"):
        RuntimeLogExporter(database).export(subject_a, actor="test")


def test_large_parent_history_exports_without_variable_sized_id_lists(tmp_path: Path) -> None:
    database = Database(tmp_path / "large.sqlite3")
    subject_id = "Noyra-export-large"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    count = 1_100
    with database.transaction() as connection:
        for index in range(count):
            _insert_goal_history(connection, subject_id, f"large-{index:04d}")

    manifest, rows = _read_artifact(RuntimeLogExporter(database).export(subject_id, actor="test"))
    assert len(rows["goals"]) == count
    assert len(rows["goal_revisions"]) == count
    entry = next(item for item in manifest["tables"] if item["name"] == "goal_revisions")
    assert entry["expected_rows"] == entry["exported_rows"] == count


def test_unregistered_schema_table_fails_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "unregistered.sqlite3")
    with database.transaction() as connection:
        connection.execute(
            "CREATE TABLE unregistered_history(subject_id TEXT NOT NULL, entry_id TEXT PRIMARY KEY)"
        )

    with pytest.raises(RuntimeError, match="does not cover schema tables: unregistered_history"):
        RuntimeLogExporter(database).export("Noyra-unregistered", actor="test")


@pytest.mark.parametrize(
    "fixture", HISTORICAL_FIXTURES, ids=lambda item: f"schema-{item.schema_version}"
)
def test_historical_fixture_export_keeps_anchor_and_reconciles_inventory(
    tmp_path: Path, fixture: HistoricalFixture
) -> None:
    path = materialize_historical_database(fixture, tmp_path)
    suffix = f"historical-{fixture.schema_version}"
    interaction_id = f"interaction-{suffix}"
    decision_id = f"interaction-decision-{suffix}"
    now = utc_now()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_goal_history(connection, fixture.subject_id, suffix)
        connection.execute(
            """INSERT INTO interactions(
                interaction_id, subject_id, direction, kind, channel, counterparty,
                content, content_hash, related_interaction_id, idempotency_key, status,
                rationale, state_hash, created_at, decided_at
            ) VALUES (?, ?, 'incoming', 'human_message', 'fixture', 'fixture-user',
                      'historical fixture', ?, NULL, ?, 'offered', NULL, ?, ?, NULL)""",
            (
                interaction_id,
                fixture.subject_id,
                content_hash({"interaction": suffix}),
                f"interaction-key-{suffix}",
                content_hash({"state": suffix}),
                now,
            ),
        )
        connection.execute(
            """INSERT INTO interaction_decisions(
                decision_id, interaction_id, from_status, to_status, rationale,
                actor, state_hash, created_at
            ) VALUES (?, ?, 'offered', 'accepted', 'fixture', 'fixture', ?, ?)""",
            (decision_id, interaction_id, content_hash({"decision": suffix}), now),
        )
        connection.commit()

    database = Database(path)
    manifest, rows = _read_artifact(
        RuntimeLogExporter(database).export(fixture.subject_id, actor="test")
    )
    assert manifest["ownership_graph"]["schema_version"] == CURRENT_SCHEMA_VERSION
    assert any(row["event_id"] == fixture.anchor_event_id for row in rows["events"])
    assert {row["goal_id"] for row in rows["goal_revisions"]} == {f"goal-{suffix}"}
    assert {row["decision_id"] for row in rows["interaction_decisions"]} == {decision_id}
    assert all(entry["expected_rows"] == entry["exported_rows"] for entry in manifest["tables"])
