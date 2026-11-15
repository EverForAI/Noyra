"""Publisher-scoped trust bindings for signed common-knowledge packages."""

COMMON_KNOWLEDGE_TRUSTED_PUBLISHERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS common_knowledge_trusted_publishers (
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    publisher_subject_id TEXT NOT NULL,
    key_id TEXT NOT NULL REFERENCES common_knowledge_trusted_keys(key_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK ((status='active' AND revoked_at IS NULL)
        OR (status='revoked' AND revoked_at IS NOT NULL)),
    PRIMARY KEY(subject_id, key_id, publisher_subject_id)
);
CREATE TRIGGER IF NOT EXISTS validate_common_knowledge_trusted_publisher_insert
BEFORE INSERT ON common_knowledge_trusted_publishers
WHEN NEW.subject_id = NEW.publisher_subject_id
BEGIN
    SELECT RAISE(ABORT, 'a subject cannot trust an external key as its own publisher');
END;
CREATE TRIGGER IF NOT EXISTS prevent_common_knowledge_trusted_publisher_update
BEFORE UPDATE ON common_knowledge_trusted_publishers
WHEN NEW.subject_id IS NOT OLD.subject_id
  OR NEW.publisher_subject_id IS NOT OLD.publisher_subject_id
  OR NEW.key_id IS NOT OLD.key_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NOT (
      (OLD.status='active' AND NEW.status='revoked' AND NEW.revoked_at IS NOT NULL)
      OR (OLD.status='revoked' AND NEW.status='active' AND NEW.revoked_at IS NULL)
  )
BEGIN
    SELECT RAISE(ABORT, 'common knowledge publisher trust transition is invalid');
END;

INSERT OR IGNORE INTO common_knowledge_trusted_publishers(
    subject_id, publisher_subject_id, key_id, status, created_at, revoked_at
)
SELECT peer.subject_id, peer.publisher_subject_id, peer.key_id,
    trusted.status, peer.created_at,
    CASE WHEN trusted.status='revoked'
        THEN COALESCE(trusted.revoked_at, peer.created_at) ELSE NULL END
FROM common_knowledge_peers peer
JOIN common_knowledge_trusted_keys trusted ON trusted.key_id=peer.key_id
WHERE peer.subject_id != peer.publisher_subject_id;
"""
