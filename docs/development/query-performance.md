# Query performance and complete evidence

Schema 114 adds three indexes through an additive migration. Historical migration
SQL and version fingerprints remain frozen; the new full/structural fingerprints
include the new objects. The migration changes no business rows or status hashes.
The existing migration backup/rollback and schema drift checks remain in force.

`MemoryStore.search` keeps its literal Unicode casefold match before LIMIT,
subject/status/type filters, descending salience/confidence/update-time order and
existing 1..200 return bound. The subject-wide and type-specific rank indexes
cover the complete ordering, so no temporary sorting B-tree is required. Existing
indexes remain available for other retrieval paths. No arbitrary candidate prefix
is introduced. As before, exact ties in all three ranking fields have no promised
relative order. Selected records still undergo the same content/state validation.

The workflow subject/history index orders checkpoints by subject, workflow and
version. It adds a storage/write cost; it does not introduce a new source of
ownership or authorize dropping historical evidence. Tests verify fresh creation,
real version-113 upgrade, interrupted migration recovery, idempotent reopening and
structural drift rejection. Old runtimes continue to reject newer databases;
rolling an application back is not a license to downgrade a migrated database.

## Workflow ownership and complete verification

The integrity reader first enumerates distinct owned workflow keys through the
subject/history index. It probes each key for another subject using the workflow
primary-key prefix. The conflict query's final `LIMIT 1` limits the diagnostic
report, never the workflows considered. It detects an early or late foreign row
and does not assume that the newest checkpoint attests ownership of the past.

After the ownership check, every checkpoint is streamed in workflow/version
order. Version continuity, legal state transitions, state hashes and clock
receipts retain their existing validation. There is no candidate limit, latest-
only shortcut or skipped history. Memory holds per-workflow verification state,
not a fetched copy of every checkpoint. A damaged middle/last row remains an
error, including a valid-looking current row above it.

Regression tests use SQLite virtual-machine work and query plans rather than a
wall-clock latency threshold. Growth fixtures include one long workflow,
multiple independent histories, empty subjects, foreign ownership and bad hashes.
The speedup is specific to repeated SQL work; it does not turn full evidence
verification into a constant-time operation.
