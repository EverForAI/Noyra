"""Versioned retention settings, separate from protected evidence."""

MIGRATION_V83 = """
CREATE TABLE IF NOT EXISTS retention_policies (
 subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
 settings_json TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision >= 1),
 updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, state_hash TEXT NOT NULL
);
"""
