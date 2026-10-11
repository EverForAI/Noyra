"""Versioned counter migrations; no historical scans in runtime admission."""

TABLES_V80 = (
    "model_calls",
    "model_attempts",
    "actions",
    "behavior_logs",
    "research_search_runs",
    "action_deliberation_runs",
    "embedding_usage_entries",
    "embedding_circuit_transitions",
)

ROUTE_TABLES_V92 = (
    "cognitive_route_decisions",
    "cognitive_route_attempts",
    "cognitive_route_outcomes",
    "waiting_cognitive_tasks",
    "waiting_cognitive_task_revisions",
)
TABLES_V92 = TABLES_V80 + ROUTE_TABLES_V92
MAX_ROUTE_FAILOVERS = 32


def _route_owner(table: str, prefix: str) -> str:
    if table == "waiting_cognitive_task_revisions":
        return f"(SELECT subject_id FROM waiting_cognitive_tasks WHERE task_id={prefix}.task_id)"
    return f"{prefix}.subject_id"


def migration_v92() -> str:
    names = ",".join(f"'{name}'" for name in ROUTE_TABLES_V92)
    statements = [
        f"""
CREATE TABLE IF NOT EXISTS route_evidence_row_counts (
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 table_name TEXT NOT NULL CHECK(table_name IN ({names})),
 row_count INTEGER NOT NULL CHECK(typeof(row_count)='integer' AND row_count>=0),
 PRIMARY KEY(subject_id,table_name)
);
"""
    ]
    for table in ROUTE_TABLES_V92:
        parent = table == "waiting_cognitive_task_revisions"
        count = (
            "SELECT count(*) FROM waiting_cognitive_task_revisions r "
            "JOIN waiting_cognitive_tasks t "
            "ON t.task_id=r.task_id WHERE t.subject_id=s.subject_id"
            if parent
            else f"SELECT count(*) FROM {table} t WHERE t.subject_id=s.subject_id"
        )
        new_owner, old_owner = _route_owner(table, "NEW"), _route_owner(table, "OLD")
        statements.append(f"""
INSERT OR IGNORE INTO route_evidence_row_counts
SELECT s.subject_id,'{table}',({count}) FROM subject_identity s;
CREATE TRIGGER IF NOT EXISTS evidence_count_{table}_insert AFTER INSERT ON {table} BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM route_evidence_row_counts
   WHERE subject_id={new_owner} AND table_name='{table}')
   THEN RAISE(ABORT,'evidence counter missing') END;
 UPDATE route_evidence_row_counts SET row_count=row_count+1
   WHERE subject_id={new_owner} AND table_name='{table}';
END;
CREATE TRIGGER IF NOT EXISTS evidence_count_{table}_delete AFTER DELETE ON {table} BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM route_evidence_row_counts
   WHERE subject_id={old_owner} AND table_name='{table}')
   THEN RAISE(ABORT,'evidence counter missing') END;
 UPDATE route_evidence_row_counts SET row_count=row_count-1
   WHERE subject_id={old_owner} AND table_name='{table}';
END;
""")
        if not parent:
            statements.append(f"""
CREATE TRIGGER IF NOT EXISTS evidence_count_{table}_owner BEFORE UPDATE OF subject_id ON {table}
WHEN OLD.subject_id IS NOT NEW.subject_id BEGIN
 SELECT RAISE(ABORT,'routing evidence owner is immutable');
END;
""")
    seeds = "\n".join(
        f"INSERT INTO route_evidence_row_counts VALUES(NEW.subject_id,'{t}',0);"
        for t in ROUTE_TABLES_V92
    )
    statements.append(f"""
CREATE TRIGGER IF NOT EXISTS route_evidence_counts_new_subject
AFTER INSERT ON subject_identity BEGIN
{seeds}
END;
""")
    return "\n".join(statements)


def migration_v80() -> str:
    names = ",".join(f"'{name}'" for name in TABLES_V80)
    statements = [
        f"""
CREATE TABLE IF NOT EXISTS evidence_row_counts (
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    table_name TEXT NOT NULL CHECK(table_name IN ({names})),
    row_count INTEGER NOT NULL CHECK(typeof(row_count)='integer' AND row_count >= 0),
    PRIMARY KEY(subject_id, table_name)
);
"""
    ]
    for table in TABLES_V80:
        statements.append(f"""
INSERT OR IGNORE INTO evidence_row_counts(subject_id,table_name,row_count)
SELECT s.subject_id,'{table}',(SELECT count(*) FROM {table} t WHERE t.subject_id=s.subject_id)
FROM subject_identity s;
CREATE TRIGGER IF NOT EXISTS evidence_count_{table}_insert AFTER INSERT ON {table} BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM evidence_row_counts
        WHERE subject_id=NEW.subject_id AND table_name='{table}')
        THEN RAISE(ABORT,'evidence counter missing') END;
    UPDATE evidence_row_counts SET row_count=row_count+1
        WHERE subject_id=NEW.subject_id AND table_name='{table}';
END;
CREATE TRIGGER IF NOT EXISTS evidence_count_{table}_delete AFTER DELETE ON {table} BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM evidence_row_counts
        WHERE subject_id=OLD.subject_id AND table_name='{table}')
        THEN RAISE(ABORT,'evidence counter missing') END;
    UPDATE evidence_row_counts SET row_count=row_count-1
        WHERE subject_id=OLD.subject_id AND table_name='{table}';
END;
CREATE TRIGGER IF NOT EXISTS evidence_count_{table}_owner AFTER UPDATE OF subject_id ON {table}
WHEN OLD.subject_id != NEW.subject_id BEGIN
    SELECT CASE WHEN (SELECT count(*) FROM evidence_row_counts
        WHERE subject_id IN (OLD.subject_id,NEW.subject_id) AND table_name='{table}') != 2
        THEN RAISE(ABORT,'evidence counter missing') END;
    UPDATE evidence_row_counts SET row_count=row_count-1
        WHERE subject_id=OLD.subject_id AND table_name='{table}';
    UPDATE evidence_row_counts SET row_count=row_count+1
        WHERE subject_id=NEW.subject_id AND table_name='{table}';
END;
""")
    seeds = "\n".join(
        f"INSERT INTO evidence_row_counts VALUES(NEW.subject_id,'{table}',0);"
        for table in TABLES_V80
    )
    statements.append(f"""
CREATE TRIGGER IF NOT EXISTS evidence_counts_new_subject AFTER INSERT ON subject_identity BEGIN
{seeds}
END;
""")
    return "\n".join(statements)
