# Verify an accepted workflow chain

`POST /api/v1/admin/workflow-increments/verify` (also `/api/admin/...`) verifies
an explicitly selected accepted tip, reconstructs its complete evidence in memory
and returns bounded verification metadata. It requires operator permission and,
for cookie authentication, the normal CSRF header. Read-only admin tokens are
insufficient. Publication and directory endpoints keep their existing contracts.

The JSON body has exactly one field:

```json
{"tip_id":"wdelta_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}
```

`tip_id` is an accepted `wdelta_` increment or `wexport_` base identity. The control
never guesses the latest chain from timestamps or filenames. A historical tip
keeps its original version even if the live workflow progresses or a sibling
branch is published. A base alone is a one-node chain. The existing base-only
`workflow-exports/verify` route and request shape remain unchanged.

## Admission and independent roots

The instance must be manually paused, owned and drained, with current admission,
migration, protected-storage and integrity controls available. The verifier
checks the current schema contract, subject/genesis, independently accepted
catalog and original preparation/acceptance receipts for every node. Each parent
reference, range, schema, key binding and source event anchor is validated.
Current event-chain metadata is verified before and after reconstruction.

Every selected dependency must be accepted. Prepared or durability-unknown tips
cannot be read or promoted through this endpoint. Keys must remain present under
the existing keyring generation/reference rules; retired retained keys work for
their original links. A missing file, key, parent or receipt fails the complete
operation. Files are opened with creation disabled; no repair, re-encoding,
acceptance update, keyring revision or audit row is written by verification.

Existing schema-115 base artifacts remain readable through a current runtime
that supports their historical source fingerprint. Old base readers still reject
increment identifiers. I3 changes neither schema 116 nor the base/increment
formats, and does not silently translate an unsupported source schema.

## Complete reconstruction and access

The internal reader reconstructs all ordered records into a new in-memory SQLite
evidence schema. It verifies the complete selected history and re-reads the
retained SQLite records at every ancestor boundary. State JSON, original reasons,
checkpoint timestamps and clock-exception receipts are preserved exactly. The
source event chain is provenance, not a claim that a workflow-only artifact can
reproduce the entire instance. Partial evidence is never exposed on failure.

After the full proof passes, the evidence database is query-only. Its internal
context API, `open_workflow_chain`, provides summary, bounded pages and the latest
original checkpoint. It takes no destination path or SQL from a caller. The view
expires when the context exits and creates no persistent reconstruction file.
The source database and original encrypted files remain unchanged.

Each summary/latest/page access rechecks accepted root metadata, current keys,
ownership and privacy. Pages check these before and after building their result;
row iteration also checks deadline and admission. A new withdrawal, deletion
intent or revoked lease prevents subsequent exposure. Current source health is
checked when opening; full file proofs run on open, not for every page. An
already verified memory view is evidence from that completed read, not a
continuous assertion about later physical file health. Content already returned
to an authorized in-process caller cannot be recalled by a later withdrawal.

## HTTP response and budgets

HTTP returns summary only: `format=workflow-chain-evidence/v1`, tip/base/workflow
IDs, complete record/state-byte/receipt counts, source schema, node count, depth,
and `ancestor_boundaries_verified`. It explicitly reports
`complete_history_verified=true`, `isolated_reconstruction_verified=true`,
`storage=ephemeral_memory_sqlite`, `source_history_preserved=true` and
`restore_or_dispatch_authorized=false`.

No checkpoint record, reason, receipt payload, path, key or manifest descriptor is
returned by HTTP. Internal pages remain limited to 100 records and 16 MiB of
canonical record text. The chain retains I2 limits: 16 nodes, 4,096 cumulative
segments including the base, 512 MiB canonical reconstruction text and a 768-MiB
SQLite page ceiling. These are not operating-system RSS guarantees.

The cooperative 120-second deadline covers opening, full proof and context
access. Long root/schema SQL queries have an interrupt handler that preserves the
original privacy/admission/timeout exception. A blocked OS call cannot be forcibly
preempted; expired work is rejected after it returns. Complete prefix proofs can
cost chain depth times history size; reaching a bound fails instead of returning
an incomplete green status.

Responses use the existing increment error contract: malformed body/type 400,
permission/CSRF 401, invalid identity/evidence/schema or unaccepted tip 409,
non-JSON 415, and lifecycle/storage/key/deadline unavailability 503. Existing
transport body/rate limits apply.

This provides workflow evidence access and verification. Full-instance restore,
runtime activation, dispatch, fee settlement, hot/cold read switching, parent
pruning and deletion of hot history require separate authority and design. I1,
I2 and I3 are complete as this bounded incremental archive/evidence capability.
