"""Derived, bounded acquisition state; immutable observations remain unchanged."""

WORLD_POLL_SCHEMA = """
CREATE TABLE IF NOT EXISTS world_poll_state (
    source_id TEXT PRIMARY KEY REFERENCES world_sources(source_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    fingerprint_version INTEGER NOT NULL CHECK(fingerprint_version >= 1),
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    last_change_at TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('changed','unchanged','failed')),
    checks INTEGER NOT NULL CHECK(checks >= 1),
    unchanged_checks INTEGER NOT NULL CHECK(unchanged_checks >= 0),
    new_item_versions INTEGER NOT NULL CHECK(new_item_versions >= 0),
    baseline TEXT NOT NULL CHECK(baseline IN ('fresh','legacy_unavailable')),
    state_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_world_poll_subject ON world_poll_state(subject_id,source_id);
CREATE TABLE IF NOT EXISTS world_feed_seen (
    source_id TEXT NOT NULL REFERENCES world_sources(source_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    fingerprint_version INTEGER NOT NULL CHECK(fingerprint_version >= 1),
    item_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY(source_id,fingerprint_version,item_key,fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_world_feed_seen_age
    ON world_feed_seen(source_id,fingerprint_version,first_seen_at,item_key,fingerprint);
CREATE TRIGGER IF NOT EXISTS validate_world_poll_subject_insert
BEFORE INSERT ON world_poll_state
WHEN NOT EXISTS(SELECT 1 FROM world_sources s WHERE s.source_id=NEW.source_id
    AND s.subject_id=NEW.subject_id)
BEGIN SELECT RAISE(ABORT,'world poll subject mismatch'); END;
CREATE TRIGGER IF NOT EXISTS validate_world_poll_subject_update
BEFORE UPDATE ON world_poll_state
WHEN NEW.source_id IS NOT OLD.source_id OR NEW.subject_id IS NOT OLD.subject_id
BEGIN SELECT RAISE(ABORT,'world poll identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_world_feed_seen_subject
BEFORE INSERT ON world_feed_seen
WHEN NOT EXISTS(SELECT 1 FROM world_sources s WHERE s.source_id=NEW.source_id
    AND s.subject_id=NEW.subject_id)
BEGIN SELECT RAISE(ABORT,'world feed subject mismatch'); END;
"""
