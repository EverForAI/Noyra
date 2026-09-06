from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from noyra.world.store import ObservationStore

from .database import CURRENT_SCHEMA_VERSION, Database
from .events import EventStore
from .identity import validate_subject_id
from .payload_codec import decompress_text
from .redaction import is_sensitive_key, redact_secret_text
from .types import canonical_json, new_id, utc_now

if TYPE_CHECKING:
    from .export_jobs import ExportControl

RUNTIME_EXPORT_OWNERSHIP_GRAPH_VERSION = 1


@dataclass(frozen=True)
class _ExportOwnershipRule:
    mode: Literal["subject", "parent", "global", "custom", "skipped"]
    detail: str
    parent_table: str | None = None
    parent_key: str | None = None
    child_key: str | None = None
    predicate: str | None = None
    parameter_count: int = 0
    reason: str | None = None


def _subject_rule() -> _ExportOwnershipRule:
    return _ExportOwnershipRule("subject", "subject_id")


def _parent_rule(parent_table: str, parent_key: str, child_key: str) -> _ExportOwnershipRule:
    return _ExportOwnershipRule(
        "parent",
        f"parent:{parent_table}.{parent_key}<-{child_key}",
        parent_table=parent_table,
        parent_key=parent_key,
        child_key=child_key,
    )


_SUBJECT_TABLES_V33 = frozenset(
    {
        "action_deliberation_runs",
        "action_revisions",
        "actions",
        "affect_components",
        "affect_transitions",
        "appraisals",
        "archive_transfer_queue",
        "audit_records",
        "autonomous_project_assistance_requests",
        "autonomous_project_executions",
        "autonomous_project_phases",
        "autonomous_project_resource_uses",
        "autonomous_project_reviews",
        "autonomous_project_sleep_reflections",
        "autonomous_projects",
        "autonomy_loop_state",
        "behavior_log_revisions",
        "behavior_logs",
        "beliefs",
        "browser_search_reservations",
        "capability_grants",
        "capability_uses",
        "causal_links",
        "cognitive_resource_groups",
        "cognitive_resource_key_events",
        "cognitive_resource_keys",
        "cognitive_route_attempts",
        "cognitive_route_decisions",
        "cognitive_route_outcomes",
        "cognitive_strategy_profiles",
        "common_knowledge_imports",
        "consciousness_frames",
        "embedding_resources",
        "entities",
        "entity_evidence_links",
        "entity_relations",
        "epistemic_review_runs",
        "event_chain_roots",
        "event_payload_segments",
        "events",
        "export_jobs",
        "fatigue_states",
        "fatigue_transitions",
        "genesis_runs",
        "goal_governance_runs",
        "goals",
        "interaction_deliveries",
        "interaction_transports",
        "interactions",
        "memories",
        "memory_accesses",
        "memory_blocks",
        "memory_consolidation_runs",
        "memory_embeddings",
        "memory_integrations",
        "metacognitive_decisions",
        "metacognitive_outcomes",
        "mission_candidates",
        "model_attempts",
        "model_calls",
        "mood_states",
        "motivation_reviews",
        "observation_content_segments",
        "observations",
        "outcome_evaluations",
        "personality_candidates",
        "predictions",
        "psychological_snapshots",
        "public_diary_entries",
        "relationship_social_runs",
        "relationships",
        "research_search_runs",
        "resource_pool_pressures",
        "retry_blocks",
        "runtime_state",
        "search_provider_configs",
        "search_provider_uses",
        "secret_cleanup_queue",
        "self_models",
        "self_modification_proposals",
        "self_modification_revisions",
        "self_modification_settings",
        "sleep_runs",
        "snapshot_archives",
        "state_snapshots",
        "storage_archives",
        "strategy_profiles",
        "subject_identity",
        "thought_agenda_items",
        "thought_episodes",
        "training_exports",
        "training_policies",
        "training_records",
        "value_profiles",
        "waiting_cognitive_tasks",
        "workflow_checkpoints",
        "world_claims",
        "world_sources",
    }
)

_PARENT_TABLES_V33 = {
    "autonomous_project_execution_revisions": _parent_rule(
        "autonomous_project_executions", "execution_id", "execution_id"
    ),
    "autonomous_project_phase_revisions": _parent_rule(
        "autonomous_project_phases", "phase_id", "phase_id"
    ),
    "autonomous_project_revisions": _parent_rule("autonomous_projects", "project_id", "project_id"),
    "belief_revisions": _parent_rule("beliefs", "belief_id", "belief_id"),
    "cognitive_resource_group_revisions": _parent_rule(
        "cognitive_resource_groups", "group_id", "group_id"
    ),
    "cognitive_strategy_profile_revisions": _parent_rule(
        "cognitive_strategy_profiles", "profile_id", "profile_id"
    ),
    "genesis_cycles": _parent_rule("genesis_runs", "run_id", "run_id"),
    "genesis_transitions": _parent_rule("genesis_runs", "run_id", "run_id"),
    "goal_revisions": _parent_rule("goals", "goal_id", "goal_id"),
    "interaction_decisions": _parent_rule("interactions", "interaction_id", "interaction_id"),
    "memory_block_revisions": _parent_rule("memory_blocks", "block_id", "block_id"),
    "memory_consolidation_members": _parent_rule(
        "memory_consolidation_runs", "consolidation_id", "consolidation_id"
    ),
    "memory_integration_revisions": _parent_rule(
        "memory_integrations", "integration_id", "integration_id"
    ),
    "memory_revisions": _parent_rule("memories", "memory_id", "memory_id"),
    "mission_candidate_revisions": _parent_rule("mission_candidates", "mission_id", "mission_id"),
    "observation_status_transitions": _parent_rule(
        "observations", "observation_id", "observation_id"
    ),
    "prediction_reviews": _parent_rule("predictions", "prediction_id", "prediction_id"),
    "relationship_revisions": _parent_rule("relationships", "relationship_id", "relationship_id"),
    "search_provider_revisions": _parent_rule("search_provider_configs", "config_id", "config_id"),
    "sleep_integrations": _parent_rule("sleep_runs", "sleep_id", "sleep_id"),
    "sleep_reflections": _parent_rule("sleep_runs", "sleep_id", "sleep_id"),
    "sleep_transitions": _parent_rule("sleep_runs", "sleep_id", "sleep_id"),
    "strategy_profile_revisions": _parent_rule("strategy_profiles", "profile_id", "profile_id"),
    "thought_agenda_revisions": _parent_rule("thought_agenda_items", "agenda_id", "agenda_id"),
    "value_profile_revisions": _parent_rule("value_profiles", "value_id", "value_id"),
    "waiting_cognitive_task_revisions": _parent_rule(
        "waiting_cognitive_tasks", "task_id", "task_id"
    ),
    "world_claim_revisions": _parent_rule("world_claims", "claim_id", "claim_id"),
    "world_source_revisions": _parent_rule("world_sources", "source_id", "source_id"),
}

_DERIVED_FTS_TABLES_V33 = frozenset(
    {
        "memory_fts",
        "memory_fts_config",
        "memory_fts_content",
        "memory_fts_data",
        "memory_fts_docsize",
        "memory_fts_idx",
    }
)

_OWNERSHIP_GRAPH_V33: dict[str, _ExportOwnershipRule] = {
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V33},
    **_PARENT_TABLES_V33,
    "schema_meta": _ExportOwnershipRule(
        "custom",
        "global:schema metadata allowlist:schema_version",
        predicate="{alias}.\"key\" = 'schema_version'",
        parameter_count=0,
    ),
    "common_knowledge_packages": _ExportOwnershipRule(
        "custom",
        "subject publisher or import",
        predicate=(
            '({alias}."publisher_subject_id" = ? OR EXISTS ('
            'SELECT 1 FROM "common_knowledge_imports" AS owner_import '
            'WHERE owner_import."package_id" = {alias}."package_id" '
            'AND owner_import."subject_id" = ?))'
        ),
        parameter_count=2,
    ),
    "common_knowledge_trusted_keys": _ExportOwnershipRule(
        "custom",
        "key referenced by subject package",
        predicate=(
            'EXISTS (SELECT 1 FROM "common_knowledge_packages" AS owner_package '
            'WHERE owner_package."key_id" = {alias}."key_id" AND '
            '(owner_package."publisher_subject_id" = ? OR EXISTS ('
            'SELECT 1 FROM "common_knowledge_imports" AS owner_import '
            'WHERE owner_import."package_id" = owner_package."package_id" '
            'AND owner_import."subject_id" = ?)))'
        ),
        parameter_count=2,
    ),
    **{
        table: _ExportOwnershipRule(
            "skipped",
            "derived:memory search index",
            reason="derived FTS storage is rebuildable from exported memories",
        )
        for table in _DERIVED_FTS_TABLES_V33
    },
}

_SUBJECT_TABLES_V34 = frozenset(
    {
        "embedding_circuit_states",
        "embedding_circuit_transitions",
        "embedding_usage_entries",
    }
)
_OWNERSHIP_GRAPH_V34 = {
    **_OWNERSHIP_GRAPH_V33,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V34},
}

_SUBJECT_TABLES_V35 = frozenset(
    {
        "archive_keyring_revisions",
        "archive_object_replica_revisions",
        "archive_object_replicas",
        "storage_usage_samples",
    }
)
_OWNERSHIP_GRAPH_V35 = {
    **_OWNERSHIP_GRAPH_V34,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V35},
}

_SUBJECT_TABLES_V36 = frozenset({"interaction_delivery_reconciliations"})
_OWNERSHIP_GRAPH_V36 = {
    **_OWNERSHIP_GRAPH_V35,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V36},
}

_SUBJECT_TABLES_V37 = frozenset({"common_knowledge_import_provenance"})
_OWNERSHIP_GRAPH_V37 = {
    **_OWNERSHIP_GRAPH_V36,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V37},
}

_SUBJECT_TABLES_V38 = frozenset({"autonomous_project_execution_clock_events"})
_OWNERSHIP_GRAPH_V38 = {
    **_OWNERSHIP_GRAPH_V37,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V38},
}

_OWNERSHIP_GRAPH_V39 = _OWNERSHIP_GRAPH_V38

_SUBJECT_TABLES_V40 = frozenset({"subject_storage_keys"})
_OWNERSHIP_GRAPH_V40 = {
    **_OWNERSHIP_GRAPH_V39,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V40},
}

_SUBJECT_TABLES_V41 = frozenset(
    {
        "common_knowledge_versions",
        "common_knowledge_sync_events",
        "common_knowledge_peers",
        "common_knowledge_remote_events",
        "common_knowledge_sync_runs",
        "common_knowledge_evaluation_events",
    }
)
_OWNERSHIP_GRAPH_V41 = {
    **_OWNERSHIP_GRAPH_V40,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V41},
}
_SUBJECT_TABLES_V42 = frozenset({"secret_file_intents"})
_OWNERSHIP_GRAPH_V42 = {
    **_OWNERSHIP_GRAPH_V41,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V42},
}
_SUBJECT_TABLES_V43 = frozenset({"archive_staging_manifests"})
_OWNERSHIP_GRAPH_V43 = {
    **_OWNERSHIP_GRAPH_V42,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V43},
}
_SUBJECT_TABLES_V44 = frozenset({"public_posts"})
_OWNERSHIP_GRAPH_V44 = {
    **_OWNERSHIP_GRAPH_V43,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V44},
}
_SUBJECT_TABLES_V45 = frozenset(
    {"interaction_bindings", "interaction_inbound_events", "interaction_threads"}
)
_OWNERSHIP_GRAPH_V45 = {
    **_OWNERSHIP_GRAPH_V44,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V45},
}
_OWNERSHIP_GRAPH_V46 = _OWNERSHIP_GRAPH_V45
_SUBJECT_TABLES_V47 = frozenset({"public_post_rate_events", "public_post_captcha_challenges"})
_OWNERSHIP_GRAPH_V47 = {
    **_OWNERSHIP_GRAPH_V46,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V47},
}
_SUBJECT_TABLES_V48 = frozenset({"public_post_controls", "public_post_moderation_events"})
_OWNERSHIP_GRAPH_V48 = {
    **_OWNERSHIP_GRAPH_V47,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V48},
}
_SUBJECT_TABLES_V49 = frozenset({"public_post_captcha_issue_events"})
_OWNERSHIP_GRAPH_V49 = {
    **_OWNERSHIP_GRAPH_V48,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V49},
}
_OWNERSHIP_GRAPH_V50 = _OWNERSHIP_GRAPH_V49
_OWNERSHIP_GRAPH_V51 = _OWNERSHIP_GRAPH_V50
_OWNERSHIP_GRAPH_V52 = _OWNERSHIP_GRAPH_V51
_SUBJECT_TABLES_V53 = frozenset(
    {
        "wallet_networks",
        "wallet_network_revisions",
        "wallet_assets",
        "wallet_asset_revisions",
        "wallet_addresses",
        "wallet_address_revisions",
        "wallet_balance_snapshots",
    }
)
_OWNERSHIP_GRAPH_V53 = {
    **_OWNERSHIP_GRAPH_V52,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V53},
}
_SUBJECT_TABLES_V54 = frozenset(
    {
        "wallet_balance_acquisition_runs",
        "wallet_balance_acquisition_attempts",
    }
)
_OWNERSHIP_GRAPH_V54 = {
    **_OWNERSHIP_GRAPH_V53,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V54},
}

_SUBJECT_TABLES_V55 = frozenset(
    {
        "wallet_bounties",
        "wallet_bounty_submissions",
        "wallet_payment_policies",
        "wallet_payment_orders",
        "wallet_ledger_journals",
        "wallet_ledger_entries",
    }
)
_OWNERSHIP_GRAPH_V55 = {
    **_OWNERSHIP_GRAPH_V54,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V55},
}

_SUBJECT_TABLES_V56 = frozenset(
    {
        "wallet_payment_executions",
        "wallet_payment_execution_attempts",
    }
)
_OWNERSHIP_GRAPH_V56 = {
    **_OWNERSHIP_GRAPH_V55,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V56},
}
_SUBJECT_TABLES_V57 = frozenset(
    {
        "wallet_reward_workflows",
        "wallet_reward_submission_links",
        "wallet_reward_incidents",
    }
)
_OWNERSHIP_GRAPH_V57 = {
    **_OWNERSHIP_GRAPH_V56,
    **{table: _subject_rule() for table in _SUBJECT_TABLES_V57},
}
# Schema 58 only widens the reward-incident kind CHECK constraint; ownership
# semantics and subject scoping are unchanged from schema 57.
_OWNERSHIP_GRAPH_V58 = _OWNERSHIP_GRAPH_V57
# Schema 59 adds immutable consent provenance to an existing subject-owned
# table; ownership semantics remain unchanged.
_OWNERSHIP_GRAPH_V59 = _OWNERSHIP_GRAPH_V58
# Schema 60 strengthens policy state hashes without changing table ownership.
_OWNERSHIP_GRAPH_V60 = _OWNERSHIP_GRAPH_V59
# Schema 61 binds payment chronology to durable state hashes without changing
# subject ownership.
_OWNERSHIP_GRAPH_V61 = _OWNERSHIP_GRAPH_V60
# Schema 62 only adds receipt evidence columns to an existing subject-owned
# execution table; export ownership is unchanged.
_OWNERSHIP_GRAPH_V62 = _OWNERSHIP_GRAPH_V61

_OWNERSHIP_GRAPHS: dict[int, dict[str, _ExportOwnershipRule]] = {
    33: _OWNERSHIP_GRAPH_V33,
    34: _OWNERSHIP_GRAPH_V34,
    35: _OWNERSHIP_GRAPH_V35,
    36: _OWNERSHIP_GRAPH_V36,
    37: _OWNERSHIP_GRAPH_V37,
    38: _OWNERSHIP_GRAPH_V38,
    39: _OWNERSHIP_GRAPH_V39,
    40: _OWNERSHIP_GRAPH_V40,
    41: _OWNERSHIP_GRAPH_V41,
    42: _OWNERSHIP_GRAPH_V42,
    43: _OWNERSHIP_GRAPH_V43,
    44: _OWNERSHIP_GRAPH_V44,
    45: _OWNERSHIP_GRAPH_V45,
    46: _OWNERSHIP_GRAPH_V46,
    47: _OWNERSHIP_GRAPH_V47,
    48: _OWNERSHIP_GRAPH_V48,
    49: _OWNERSHIP_GRAPH_V49,
    50: _OWNERSHIP_GRAPH_V50,
    51: _OWNERSHIP_GRAPH_V51,
    52: _OWNERSHIP_GRAPH_V52,
    53: _OWNERSHIP_GRAPH_V53,
    54: _OWNERSHIP_GRAPH_V54,
    55: _OWNERSHIP_GRAPH_V55,
    56: _OWNERSHIP_GRAPH_V56,
    57: _OWNERSHIP_GRAPH_V57,
    58: _OWNERSHIP_GRAPH_V58,
    59: _OWNERSHIP_GRAPH_V59,
    60: _OWNERSHIP_GRAPH_V60,
    61: _OWNERSHIP_GRAPH_V61,
    62: _OWNERSHIP_GRAPH_V62,
}


@dataclass(frozen=True)
class RuntimeExportArtifact:
    filename: str
    content: bytes
    sha256: str
    table_count: int
    row_count: int


class RuntimeLogExporter:
    """Builds an authenticated diagnostic export without reading secret files."""

    def __init__(self, database: Database):
        self.database = database

    def export(self, subject_id: str, *, actor: str) -> RuntimeExportArtifact:
        validate_subject_id(subject_id)
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.export_to_path(
                subject_id,
                actor=actor,
                target=Path(directory) / "runtime.zip",
            )
            content = (Path(directory) / "runtime.zip").read_bytes()
        return RuntimeExportArtifact(
            artifact.filename,
            content,
            artifact.sha256,
            artifact.table_count,
            artifact.row_count,
        )

    def export_to_path(
        self,
        subject_id: str,
        *,
        actor: str,
        target: Path | str,
        control: ExportControl | None = None,
    ) -> RuntimeExportArtifact:
        validate_subject_id(subject_id)
        export_id = new_id("export")
        created_at = utc_now()
        tables: list[dict[str, Any]] = []
        table_inventory: list[dict[str, Any]] = []
        total_rows = 0
        total_expected_rows = 0
        events = EventStore(self.database)
        observations = ObservationStore(self.database)
        output = Path(control.target) if control is not None else Path(target).resolve()
        if control is not None:
            control.validate_target(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{export_id}.tmp")
        timestamp = created_at.replace(":", "").replace("-", "").replace("+00:00", "Z")
        filename = f"noyra-runtime-{subject_id}-{timestamp}.zip"
        published = False
        try:
            # The export itself may take minutes or hours.  Read from an
            # isolated SQLite backup so serialising/compressing rows never
            # keeps a live WAL snapshot open and does not delay checkpoints.
            snapshot = (
                self.database.read_snapshot(checkpoint=control.checkpoint)
                if control is not None
                else self.database.read_snapshot()
            )
            temporary.unlink(missing_ok=True)
            with temporary.open("xb") as temporary_stream:
                with (
                    snapshot as connection,
                    zipfile.ZipFile(
                        temporary_stream, "w", compression=zipfile.ZIP_DEFLATED
                    ) as archive,
                ):
                    version_row = connection.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()
                    if version_row is None:
                        raise RuntimeError("database schema version is missing")
                    schema_version = int(version_row["value"])
                    tables_all = self._tables(connection)
                    ownership = self._ownership_graph(connection, schema_version, tables_all)
                    for table in tables_all:
                        if control is not None:
                            control.checkpoint()
                        rule = ownership[table]
                        if rule.mode == "skipped":
                            table_inventory.append(
                                {
                                    "name": table,
                                    "status": "skipped",
                                    "ownership": rule.detail,
                                    "expected_rows": None,
                                    "exported_rows": 0,
                                    "reconciliation": "not_applicable",
                                    "reason": rule.reason,
                                }
                            )
                            continue
                        expected_rows = self._expected_rows(connection, table, subject_id, rule)
                        referential_gaps = self._referential_gap_count(
                            connection,
                            table,
                            subject_id,
                            rule,
                            ownership,
                        )
                        if referential_gaps:
                            raise RuntimeError(
                                f"runtime export ownership mismatch for {table}: "
                                f"{referential_gaps} foreign references leave the subject graph"
                            )
                        cursor = self._cursor(connection, table, subject_id, rule)
                        name = f"tables/{table}.jsonl"
                        info = self._zip_info(name)
                        digest = hashlib.sha256()
                        row_count = 0
                        with archive.open(info, "w") as stream:
                            while batch := cursor.fetchmany(512):
                                if control is not None:
                                    control.checkpoint()
                                for row in batch:
                                    raw_row = dict(row)
                                    if table == "events":
                                        raw_row["payload_json"] = canonical_json(
                                            events.payload_from_row(row, connection=connection)
                                        )
                                    elif table == "observations":
                                        raw_row = dict(
                                            observations._materialize_row(
                                                row, connection=connection
                                            )
                                        )
                                    elif table == "model_calls":
                                        raw_row["request_json"] = decompress_text(
                                            raw_row.get("request_json")
                                        )
                                        raw_row["response_json"] = decompress_text(
                                            raw_row.get("response_json")
                                        )
                                    payload = (
                                        canonical_json(self._sanitize(raw_row)) + "\n"
                                    ).encode("utf-8")
                                    stream.write(payload)
                                    digest.update(payload)
                                    row_count += 1
                        if row_count != expected_rows:
                            raise RuntimeError(
                                f"runtime export row-count mismatch for {table}: "
                                f"expected {expected_rows}, exported {row_count}"
                            )
                        total_rows += row_count
                        total_expected_rows += expected_rows
                        tables.append(
                            {
                                "name": table,
                                "file": name,
                                "rows": row_count,
                                "status": "exported",
                                "ownership": rule.detail,
                                "expected_rows": expected_rows,
                                "exported_rows": row_count,
                                "referential_gaps": 0,
                                "sha256": digest.hexdigest(),
                            }
                        )
                        table_inventory.append(
                            {
                                "name": table,
                                "status": "exported",
                                "ownership": rule.detail,
                                "expected_rows": expected_rows,
                                "exported_rows": row_count,
                                "reconciliation": "matched",
                                "referential_gaps": 0,
                            }
                        )
                    manifest = {
                        "format": "noyra-runtime-export-v1",
                        "export_id": export_id,
                        "subject_id": subject_id,
                        "created_at": created_at,
                        "schema_version": schema_version,
                        "runtime_schema_version": CURRENT_SCHEMA_VERSION,
                        "ownership_graph": {
                            "version": RUNTIME_EXPORT_OWNERSHIP_GRAPH_VERSION,
                            "schema_version": schema_version,
                        },
                        "privacy": (
                            "developer export; contains private subject state and communications"
                        ),
                        "secret_policy": (
                            "environment variables and secret files are excluded; "
                            "cognitive resource "
                            "key material is excluded; suspicious fields and credential-like text "
                            "are redacted"
                        ),
                        "tables": tables,
                        "table_inventory": table_inventory,
                        "expected_total_rows": total_expected_rows,
                        "total_rows": total_rows,
                    }
                    if control is not None:
                        control.checkpoint()
                    archive.writestr(
                        self._zip_info("manifest.json"),
                        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode(
                            "utf-8"
                        ),
                    )
                temporary_stream.flush()
                os.fsync(temporary_stream.fileno())
            archive_digest = self._file_hash(
                temporary,
                checkpoint=None if control is None else control.checkpoint,
            )
            byte_size = temporary.stat().st_size

            def record_export(connection: Any) -> None:
                connection.execute(
                    """INSERT INTO audit_records(
                        audit_id, subject_id, action, actor, payload_json, occurred_at
                    ) VALUES (?, ?, 'runtime_exported', ?, ?, ?)""",
                    (
                        new_id("audit"),
                        subject_id,
                        actor,
                        canonical_json(
                            {
                                "export_id": export_id,
                                "archive_sha256": archive_digest,
                                "table_count": len(tables),
                                "row_count": total_rows,
                            }
                        ),
                        created_at,
                    ),
                )

            if control is None:
                temporary.replace(output)
                published = True
                with self.database.transaction() as connection:
                    record_export(connection)
            else:
                control.checkpoint()
                if byte_size > control.max_artifact_bytes:
                    raise RuntimeError("export_artifact_too_large")
                with control.publication() as connection:
                    control.validate_target(output)
                    temporary.replace(output)
                    published = True
                    record_export(connection)
                    control.complete(
                        connection,
                        artifact_path=output,
                        filename=filename,
                        sha256=archive_digest,
                        byte_size=byte_size,
                    )
        except Exception:
            temporary.unlink(missing_ok=True)
            if published:
                output.unlink(missing_ok=True)
            raise
        return RuntimeExportArtifact(
            filename=filename,
            content=b"",
            sha256=archive_digest,
            table_count=len(tables),
            row_count=total_rows,
        )

    @staticmethod
    def _zip_info(name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        return info

    @staticmethod
    def _file_hash(path: Path, *, checkpoint: Callable[[], None] | None = None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if checkpoint is not None:
                    checkpoint()
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _tables(connection: Any) -> tuple[str, ...]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        return tuple(str(row["name"]) for row in rows)

    @staticmethod
    def _identifier(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    @classmethod
    def _ownership_graph(
        cls, connection: Any, schema_version: int, tables: tuple[str, ...]
    ) -> dict[str, _ExportOwnershipRule]:
        graph = _OWNERSHIP_GRAPHS.get(schema_version)
        if graph is None:
            raise RuntimeError(
                f"runtime export ownership graph is unavailable for schema {schema_version}"
            )
        missing = sorted(set(tables) - set(graph))
        if missing:
            raise RuntimeError(
                "runtime export ownership graph does not cover schema tables: " + ", ".join(missing)
            )
        unexpected = sorted(set(graph) - set(tables))
        if unexpected:
            raise RuntimeError(
                "runtime export ownership graph references absent schema tables: "
                + ", ".join(unexpected)
            )
        for table in tables:
            rule = graph[table]
            columns = cls._columns(connection, table)
            if "subject_id" in columns and rule.mode not in {"subject", "skipped"}:
                raise RuntimeError(f"ownership graph does not classify {table} as subject-owned")
            if rule.mode == "subject":
                if "subject_id" not in columns:
                    raise RuntimeError(f"ownership graph misclassifies {table} as subject-owned")
            elif rule.mode == "parent":
                if not rule.parent_table or not rule.parent_key or not rule.child_key:
                    raise RuntimeError(f"ownership graph has incomplete parent rule for {table}")
                parent_rule = graph.get(rule.parent_table)
                if parent_rule is None or parent_rule.mode != "subject":
                    raise RuntimeError(
                        f"ownership graph parent is not directly subject-owned: "
                        f"{table}->{rule.parent_table}"
                    )
                foreign_keys = connection.execute(
                    f"PRAGMA foreign_key_list({cls._identifier(table)})"
                ).fetchall()
                if not any(
                    str(item["table"]) == rule.parent_table
                    and str(item["from"]) == rule.child_key
                    and str(item["to"]) == rule.parent_key
                    for item in foreign_keys
                ):
                    raise RuntimeError(
                        f"ownership graph parent edge is not declared: {table}->{rule.parent_table}"
                    )
            elif rule.mode == "custom" and not rule.predicate:
                raise RuntimeError(f"ownership graph has no predicate for {table}")
        return graph

    @staticmethod
    def _columns(connection: Any, table: str) -> tuple[str, ...]:
        rows = (
            connection.execute(
                f"SELECT * FROM {RuntimeLogExporter._identifier(table)} LIMIT 0"
            ).description
            or ()
        )
        return tuple(str(item[0]) for item in rows)

    @classmethod
    def _where_clause(
        cls, rule: _ExportOwnershipRule, subject_id: str, alias: str
    ) -> tuple[str, tuple[str, ...]]:
        quoted_alias = cls._identifier(alias)
        if rule.mode == "subject":
            return f'{quoted_alias}."subject_id" = ?', (subject_id,)
        if rule.mode == "global":
            return "1", ()
        if rule.mode == "parent":
            assert rule.parent_table is not None
            assert rule.parent_key is not None
            assert rule.child_key is not None
            owner_alias = cls._identifier("ownership_parent")
            return (
                f"EXISTS (SELECT 1 FROM {cls._identifier(rule.parent_table)} AS {owner_alias} "
                f"WHERE {owner_alias}.{cls._identifier(rule.parent_key)} = "
                f"{quoted_alias}.{cls._identifier(rule.child_key)} "
                f'AND {owner_alias}."subject_id" = ?)',
                (subject_id,),
            )
        if rule.mode == "custom":
            assert rule.predicate is not None
            return rule.predicate.format(alias=quoted_alias), (subject_id,) * rule.parameter_count
        raise RuntimeError(f"cannot build a selection for {rule.mode} ownership")

    @classmethod
    def _expected_rows(
        cls, connection: Any, table: str, subject_id: str, rule: _ExportOwnershipRule
    ) -> int:
        where, parameters = cls._where_clause(rule, subject_id, "exported")
        row = connection.execute(
            f"SELECT COUNT(*) AS row_count FROM {cls._identifier(table)} AS "
            f"{cls._identifier('exported')} WHERE ({where})",
            parameters,
        ).fetchone()
        return int(row["row_count"])

    @classmethod
    def _cursor(
        cls,
        connection: Any,
        table: str,
        subject_id: str,
        rule: _ExportOwnershipRule,
    ) -> Any:
        where, parameters = cls._where_clause(rule, subject_id, "exported")
        return connection.execute(
            f"SELECT * FROM {cls._identifier(table)} AS {cls._identifier('exported')} "
            f"WHERE ({where}) ORDER BY {cls._identifier('exported')}.rowid",
            parameters,
        )

    @classmethod
    def _referential_gap_count(
        cls,
        connection: Any,
        table: str,
        subject_id: str,
        rule: _ExportOwnershipRule,
        ownership: dict[str, _ExportOwnershipRule],
    ) -> int:
        child_where, child_parameters = cls._where_clause(rule, subject_id, "exported")
        foreign_keys = connection.execute(
            f"PRAGMA foreign_key_list({cls._identifier(table)})"
        ).fetchall()
        gaps = 0
        for foreign_key in foreign_keys:
            parent_table = str(foreign_key["table"])
            parent_rule = ownership.get(parent_table)
            if parent_rule is None or parent_rule.mode == "skipped":
                raise RuntimeError(
                    f"runtime export foreign-key parent is not exportable: {table}->{parent_table}"
                )
            parent_where, parent_parameters = cls._where_clause(
                parent_rule, subject_id, "parent_row"
            )
            child_column = cls._identifier(str(foreign_key["from"]))
            parent_column = cls._identifier(str(foreign_key["to"]))
            row = connection.execute(
                f"SELECT COUNT(*) AS row_count FROM {cls._identifier(table)} AS "
                f"{cls._identifier('exported')} WHERE ({child_where}) "
                f"AND {cls._identifier('exported')}.{child_column} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {cls._identifier(parent_table)} AS "
                f"{cls._identifier('parent_row')} WHERE "
                f"{cls._identifier('parent_row')}.{parent_column} = "
                f"{cls._identifier('exported')}.{child_column} AND ({parent_where}))",
                child_parameters + parent_parameters,
            ).fetchone()
            gaps += int(row["row_count"])
        return gaps

    @classmethod
    def _sanitize(cls, value: Any, key: str = "") -> Any:
        if is_sensitive_key(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                item_key: cls._sanitize(item, str(item_key)) for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize(item, key) for item in value]
        if isinstance(value, str):
            if key.endswith("_json"):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    pass
                else:
                    return canonical_json(cls._sanitize(parsed, key.removesuffix("_json")))
            return redact_secret_text(value)
        if isinstance(value, bytes):
            return {"binary_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
        return value
