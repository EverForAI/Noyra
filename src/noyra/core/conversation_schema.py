"""Logical conversation ownership schema."""

MIGRATION_V84 = """
CREATE TABLE IF NOT EXISTS private_conversations (
 conversation_id TEXT PRIMARY KEY,
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 principal_id TEXT NOT NULL, title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
 pinned INTEGER NOT NULL CHECK(pinned IN (0,1)),
 status TEXT NOT NULL CHECK(status IN ('active','archived','deleting','deleted')),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, state_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_private_conversations_subject
 ON private_conversations(subject_id, principal_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS private_conversation_messages (
 interaction_id TEXT PRIMARY KEY REFERENCES interactions(interaction_id),
 conversation_id TEXT NOT NULL REFERENCES private_conversations(conversation_id),
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id)
);
CREATE INDEX IF NOT EXISTS idx_private_conversation_messages_window
 ON private_conversation_messages(subject_id, conversation_id, interaction_id);
"""

MIGRATION_V85 = """
ALTER TABLE private_conversation_messages ADD COLUMN author_verified INTEGER NOT NULL
 DEFAULT 0 CHECK(author_verified IN (0,1));
"""
