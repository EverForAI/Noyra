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

## Settings

- `NOYRA_MEMORY_CONSOLIDATION_INTERVAL_SECONDS`
- `NOYRA_MEMORY_STALE_AFTER_DAYS`
- `NOYRA_MEMORY_ARCHIVE_AFTER_DAYS`
- `NOYRA_MINIMUM_ACTIVE_MEMORIES`
