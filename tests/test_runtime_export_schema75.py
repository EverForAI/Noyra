from pathlib import Path

from noyra.core import Database, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import content_hash


def test_current_schema_runtime_export_has_complete_ownership_graph(tmp_path: Path) -> None:
    database = Database(tmp_path / "current-schema.sqlite3")
    subject_id = "Noyra-current-schema"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))

    artifact = RuntimeLogExporter(database).export(subject_id, actor="test")

    assert artifact.table_count > 0
    assert artifact.row_count > 0


def test_current_schema_ownership_graph_classifies_migration_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "current-schema-graph.sqlite3")
    with database.read_snapshot() as connection:
        tables = RuntimeLogExporter._tables(connection)
        graph = RuntimeLogExporter._ownership_graph(connection, CURRENT_SCHEMA_VERSION, tables)
        target_columns = RuntimeLogExporter._columns(connection, "migration_targets")

    for table in (
        "migration_audit_events",
        "migration_epochs",
        "migration_policies",
        "migration_proposals",
        "migration_rejections",
        "migration_targets",
        "migration_tasks",
    ):
        assert graph[table].mode == "subject"
    assert graph["migration_target_challenges"].mode == "parent"
    assert graph["persistent_features"].mode == "skipped"
    assert {"recipient_public_key", "recipient_key_fingerprint"} <= set(target_columns)
