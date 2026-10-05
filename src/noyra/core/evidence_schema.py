"""Frozen schema-80 counter migration; no historical scans in runtime admission."""

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
