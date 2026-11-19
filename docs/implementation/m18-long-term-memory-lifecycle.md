# M18: Long-Term Memory Retrieval and Lifecycle

M18 turns the existing revisioned memory ledger into an operational long-term memory system. Raw
memory and every revision remain durable; forgetting means removing a weak memory from active
recall, never deleting its history.

## Contextual recall

`MemoryStore.recall` ranks active memories from lexical overlap, salience, confidence, recency and
past contextual access. Recall is deterministic and local, so it consumes no remote model token.
Every selected memory appends or advances an access ledger with a query hash, cognition context,
relevance score, first access, latest access and access count.

World cognition, equal interaction and goal governance now receive a small, typed set of relevant
memories rather than a globally recent slice. Human text remains untrusted and cannot rewrite a
memory; it is used only as a local retrieval query.

## Consolidation and forgetting

The awake autonomy cycle periodically runs deterministic memory consolidation after other durable
work. It can:

- strengthen the salience of memories repeatedly useful across contexts;
- archive exact duplicates while preserving the strongest active representative;
- move old, weak, unused episodic/reflection/prediction memories out of active recall;
- create a bounded reflection summary for clusters of weak stale memories before archiving them.

Semantic, procedural, autobiographical, emotional and relationship memories are not aged out by
the default policy. `NOYRA_MINIMUM_ACTIVE_MEMORIES` prevents consolidation from emptying the active
memory set.

Consolidation runs and membership decisions are append-only. Archived memories, their original
content, all revisions and their causal source events remain available in the developer runtime
export and integrity audit.

The configured interval is elapsed time since the last committed consolidation, including
`no_change` runs. It is not a UTC calendar-day limit: a subdaily interval can produce several
distinct runs on the same date, while crossing midnight does not shorten a longer interval.
Before the interval expires, repeated ticks and worker restarts make no consolidation writes.

New run keys use `memory-consolidation:v2:after:<predecessor-id>` (or `initial` when no run exists).
The predecessor is the latest committed run for that subject, selected by append order rather
than textual timestamp order. The key is independent of clock formatting, timezone or retry time.
Historical daily keys remain unchanged and can serve as predecessors for new runs; no schema
migration or historical rewrite is needed.

The write transaction rechecks both the predecessor identity and the elapsed interval before
creating an event or revising memory. Workers that selected the same predecessor can commit at
most one successor. An already-committed key returns `memory_consolidation_replayed`; other stale
work returns no result and waits for the next scheduling check. Events, memory revisions, members
and the run receipt commit together. A failure rolls back the complete round so it can be retried;
a restart after a successful commit does not apply that round twice.

This fixes scheduling identity only. Existing strengthening, duplicate archival, stale-memory
thresholds, minimum active memory policy and source/revision checks still govern each run. It does
not increase any remote-model budget; deterministic consolidation remains local work.

## Settings

- `NOYRA_MEMORY_CONSOLIDATION_INTERVAL_SECONDS`
- `NOYRA_MEMORY_STALE_AFTER_DAYS`
- `NOYRA_MEMORY_ARCHIVE_AFTER_DAYS`
- `NOYRA_MINIMUM_ACTIVE_MEMORIES`
