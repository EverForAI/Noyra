"""Derived public routing slots; bodies are always verified against current source rows."""

_SOURCES = (
    (
        "public_publications",
        "publication_id",
        "published_at",
        "NEW.kind",
        "kind",
        "status='published'",
    ),
    ("public_diary_entries", "entry_id", "created_at", "'diary'", "'diary'", "1=1"),
    ("public_posts", "post_id", "published_at", "'post'", "'post'", "status='published'"),
    (
        "interactions",
        "interaction_id",
        "created_at",
        "'interaction'",
        "'interaction'",
        "direction='outgoing' AND substr(channel,1,7)='public:' COLLATE BINARY",
    ),
    (
        "events",
        "event_id",
        "occurred_at",
        "'lifecycle'",
        "'lifecycle'",
        "event_type IN ('lifecycle_transition','lifecycle_recovery') "
        "AND privacy_level='public_status'",
    ),
    (
        "behavior_log_revisions",
        "log_id",
        "occurred_at",
        "'behavior'",
        "'behavior'",
        "revision_number=(SELECT MAX(r2.revision_number) FROM behavior_log_revisions r2 "
        "WHERE r2.log_id=behavior_log_revisions.log_id)",
    ),
)

MIGRATION_V94 = """
CREATE TABLE IF NOT EXISTS public_sitemap_slots (
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    kind TEXT NOT NULL CHECK(kind IN ('goal','project','prediction','research','sleep','update',
                                     'diary','post','interaction','lifecycle','behavior')),
    slot INTEGER NOT NULL CHECK(slot >= 1),
    item_id TEXT NOT NULL,
    PRIMARY KEY(subject_id,kind,slot),
    UNIQUE(subject_id,kind,item_id)
);
CREATE TRIGGER IF NOT EXISTS public_sitemap_slots_no_update
BEFORE UPDATE ON public_sitemap_slots
BEGIN SELECT RAISE(ABORT,'public sitemap slots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS public_sitemap_slots_no_delete
BEFORE DELETE ON public_sitemap_slots
BEGIN SELECT RAISE(ABORT,'public sitemap slots are immutable'); END;
"""
for table, key, timestamp, new_kind, kind, where in _SOURCES:
    MIGRATION_V94 += f"""
WITH eligible AS (
    SELECT subject_id,{kind} AS kind,{key} AS item_id,{timestamp} AS item_time
    FROM {table} WHERE {where}
), missing AS (
    SELECT * FROM eligible e WHERE NOT EXISTS (
        SELECT 1 FROM public_sitemap_slots s
        WHERE s.subject_id=e.subject_id AND s.kind=e.kind AND s.item_id=e.item_id
    )
), ranked AS (
    SELECT *,ROW_NUMBER() OVER(PARTITION BY subject_id,kind ORDER BY item_time,item_id) AS offset
    FROM missing
), assigned AS MATERIALIZED (
    SELECT r.subject_id,r.kind,r.offset+COALESCE((
    SELECT MAX(s.slot) FROM public_sitemap_slots s
    WHERE s.subject_id=r.subject_id AND s.kind=r.kind
    ),0) AS slot,r.item_id FROM ranked r
)
INSERT OR IGNORE INTO public_sitemap_slots(subject_id,kind,slot,item_id)
SELECT subject_id,kind,slot,item_id FROM assigned;
"""
    for event in ("INSERT", "UPDATE"):
        MIGRATION_V94 += f"""
CREATE TRIGGER IF NOT EXISTS public_sitemap_{table}_{event.lower()}
AFTER {event} ON {table}
BEGIN
    INSERT OR IGNORE INTO public_sitemap_slots(subject_id,kind,slot,item_id)
    SELECT NEW.subject_id,{new_kind},
           1+COALESCE((SELECT MAX(slot) FROM public_sitemap_slots
                      WHERE subject_id=NEW.subject_id AND kind={new_kind}),0),NEW.{key}
    WHERE EXISTS(SELECT 1 FROM {table} WHERE {key}=NEW.{key} AND {where});
END;
"""
