# Verified workflow evidence reconstruction

D2c base reading consumes the existing version-1 complete workflow bundle. The
source identity and manifest must come from the protected accepted publication
catalog, never from a caller-supplied manifest. A frozen accepted history can
remain valid after its live workflow advances. This format does not imply a
full-instance backup or permission to activate a runtime.

The private record stream reuses complete row, transition, scope, original hash,
clock receipt and aggregate verification. It is tentative until fully exhausted.
The public verification wrapper still consumes the entire stream. The isolated
builder writes only into a new in-memory SQLite evidence database; neither a
live Database nor a caller-selected path is a reconstruction destination.

Before exposing an evidence view, the builder reads every retained SQLite record
back and recomputes the complete history and receipt proof against the accepted
source. It retains original state JSON, reasons, timestamps and receipt metadata
without inventing an event chain. The evidence schema lacks runtime identity,
lifecycle, wallet and dispatch tables. It is marked query-only and closed when
its context exits, including errors. No plaintext temporary file is created.

Reconstruction adds a 512-MiB canonical record byte budget (including reasons and
receipts) and a 768-MiB SQLite page ceiling, on top of bundle limits. These are
storage/serialized-data bounds, not an operating-system process RSS guarantee.
Internal pages contain at most 100 records and 16 MiB of canonical record text;
one record larger than that page budget fails. Pagination follows original
checkpoint versions, never wall-clock ordering. All work uses cancellation
checkpoints. A failure discards the evidence view, preserving the source and
accepted artifacts.

The base reader retains this complete-history proof and ephemeral isolation.
I3 separately adds [accepted chain evidence reading](../api/workflow-chain-verification.md)
with the same access and page limits. Persistent restore files, ordinary
hot/cold reader switching, source cutoff and runtime activation remain separate
work.

A separately reviewed [increment parent-chain design](workflow-increment-protocol.md)
and isolated fault prototype underpin I1/I2/I3. The base reader's schema and
accepted format remain unchanged.
