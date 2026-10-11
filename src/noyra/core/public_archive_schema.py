"""Explicit public summaries; never backfill private runtime content."""

MIGRATION_V93 = """
CREATE TABLE IF NOT EXISTS public_publications (
    publication_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    kind TEXT NOT NULL CHECK (kind IN ('goal','project','prediction','research','sleep','update')),
    source_id TEXT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    body TEXT NOT NULL,
    details_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('published','withdrawn')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    UNIQUE(subject_id, publication_id)
);
CREATE INDEX IF NOT EXISTS idx_public_publications_page
    ON public_publications(subject_id, status, kind, published_at DESC, publication_id DESC);
CREATE TABLE IF NOT EXISTS public_publication_revisions (
    publication_id TEXT NOT NULL REFERENCES public_publications(publication_id),
    revision INTEGER NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('published','withdrawn')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(publication_id, revision)
);
CREATE TRIGGER IF NOT EXISTS prevent_public_publication_revision_update
    BEFORE UPDATE ON public_publication_revisions
    BEGIN SELECT RAISE(ABORT, 'public publication revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_public_publication_revision_delete
    BEFORE DELETE ON public_publication_revisions
    BEGIN SELECT RAISE(ABORT, 'public publication revisions are immutable'); END;
CREATE INDEX IF NOT EXISTS idx_public_lifecycle_page
    ON events(subject_id, event_type, privacy_level, occurred_at DESC, event_id DESC);
"""
