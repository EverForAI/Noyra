# Lifecycle maintenance and verified workflow snapshots

Status: staged design with D2a advisory diagnostics implemented. D2a separates
preservation coverage from table-name drift and explains oversized workflow
recovery as `maintenance_required`. It does not enable a cleanup worker, a
checkpoint trust cache, a restore endpoint or a new deletion policy. Full
integrity and acknowledgement checks remain authoritative. Raw later-stage
protocol experiments and private evidence are kept outside the public tree.

## Separate structural coverage from lifecycle authority

A schema inventory answers whether an installation's tables match its reviewed
schema. It cannot establish that each table has an approved preserve, archive,
compact or delete policy. Refreshing the structural baseline must never grant
permission to erase a newly discovered table.

The lifecycle catalog should enumerate reviewed object names explicitly, with:

- Logical retention action and a separately stated physical representation action.
- Domain owner, scope resolution, primary and idempotency keys.
- Incoming/outgoing foreign keys and domain references held in JSON, purposes,
  deterministic event IDs, revision links and encrypted objects.
- Immutable evidence, unresolved-work and privacy constraints.
- Existing implementation, publication/retry protocol, reader compatibility and
  recovery prerequisites; planned behavior is not described as implemented.

Unknown objects remain unreviewed and preserved. FTS shadows are managed only
through their owning virtual table's rebuild contract. Session expiration,
credential revocation, deduplication eviction and privacy erasure retain their
own reviewed domain boundaries; they are not interchangeable with generic
age-based retention. Existing safe deletion windows do not expand by default.
Policy coverage is a separate advisory diagnostic; introducing it must not turn
every preserved legacy table into a new P1 finding or halt ordinary cognition.
Existing structural-drift failures retain their own security meaning. A policy
gap blocks cleanup eligibility for that object, not normal autonomous activity.

The first policy groups are kernel/schema, events/audit, archives/storage,
action/capability, memory, belief/goal, affect/identity, cognition/control,
projects, sleep, model/resources, research/world, common knowledge,
communication, privacy, participants, public content, authentication,
health/retention, migration, training and wallet. Per-table membership and
reference sign-off must precede executable coverage. Group membership alone
cannot authorize deletion.

## Workflow preservation contract

Checkpoint versions, original state JSON, state hashes, status transitions,
subject/workflow ownership and clock-exception receipts form one history. A
latest-row hash cannot attest all preceding rows. A workflow-only artifact is
not a full subject backup and must not be used to start a runtime or recover
unrelated actions, calls, wallet state, privacy state or budgets.

The D2a maintenance catalog returns a metadata response with
`maintenance_required` when ordinary recovery bounds are exceeded. It states
record/byte counts and supported limits without shortening history or presenting
a partial fingerprint as complete. It explicitly reports that no production
maintenance execution route is available yet. The existing acknowledgement
operation retains its bounds and cannot consume this unverified observation.
See `lifecycle-diagnostics.md` and `../api/workflow-maintenance-status.md`.

A supported export must capture a coherent SQLite backup, including committed
WAL data, and verify the complete source history and relevant event-chain
metadata in that same snapshot. Live databases are never opened as immutable
merely because a WAL file happens to be absent.

The artifact contract binds:

- Format, artifact scope, schema version/fingerprint and subject genesis identity.
- Subject, workflow, complete version range, record count and UTF-8 state bytes.
- A streaming digest identical to the canonical original complete row-array hash.
- Clock receipt content/identity, receipt digest and resulting time high-water.
- Ordered bounded segments with exact names, lengths, ranges and digests.
- Last-row hash, source event-chain boundary and the preceding accepted manifest.

Segments and the manifest require authenticated encryption. Keys are supplied
through the existing protected key/storage boundary, not embedded in artifacts.
The accepted manifest digest must be retained independently in an owned,
audited catalog. A digest copied from an untrusted artifact is not source
attestation; a self-consistent rewritten database is not independent evidence.

## Base, increment and recovery

A base contains the complete verified prefix. An increment starts at exactly the
next version, identifies its accepted parent manifest and preserves original
rows and receipts. Prefix changes, including rehashed changes with an unchanged
last row, must reject increment publication. A missing/reordered parent, missing
segment, corrupt receipt, changed identity/schema or untrusted manifest also
fails closed.

The isolated prototype intentionally rechecks the full current prefix before
producing an increment. It demonstrates equivalence, not a production speedup.
A future latest-state-plus-increment reader needs an enforceable immutable
prefix and independently trusted publication root. Without that boundary it
must perform full verification. Periodic full audit remains independent from
routine observations and cannot be replaced by a cached green status.

Recovery first reconstructs a new disposable database, checks original hashes,
sequence, status transitions, source ownership and clock evidence, and compares
the full result to the accepted manifest. A reconstructed event chain for the
exported receipts does not claim to reproduce the source's entire event chain;
the source head is retained as provenance, while artifact scope stays explicit.
The prototype has no production activation or source-row deletion operation.

A published workflow bundle cannot authorize model dispatch, settle an unknown
call, release reserved fees, clear a migration epoch, resume a paused subject or
revive withdrawn private inputs. Full-instance recovery must replay current
privacy and ownership controls before any separately authorized activation.

## Publication and failure behavior

Production maintenance requires exclusive runtime ownership, a paused and
drained control operation, usable protected storage, a clear migration fence
and a version-bound source snapshot. Expensive capture/serialization must not
hold a database writer transaction across disk or remote I/O.

Publication proceeds through staged encrypted segments, complete read-back
verification, a manifest and an independently protected acceptance receipt. Any
source cutoff or physical representation change would be a later atomic,
compare-and-swap operation against the accepted complete source fingerprint.
No prefix is removed while only a staged or durability-unknown object exists.
Retries are idempotent; cancellation preserves recoverable staging evidence.
Acceptance idempotency is keyed to the verified source identity/range/digests
and parent publication, not merely a randomized ciphertext hash. A second
encoding of the same source cannot create a second logical maintenance action.

A process exit after any segment, before/after manifest publication, during
acceptance or before/after source cutoff must leave either the prior valid
representation or a completely verifiable new one. Directory durability,
Windows ACL behavior, key rotation, encrypted archive dependencies and remote
replica availability require explicit platform validation. Local file fsync and
rename tests alone do not establish crash or power-loss durability.

## Staged implementation gates

| Stage | Scope | Risk | Exit requirement |
|---|---|---|---|
| D2a | Separate policy coverage from structural drift; expose maintenance metadata | Low to medium | New/unreviewed objects remain preserved; no cleanup eligibility changes; bounded private diagnostics |
| D2b | Owned maintenance control and encrypted export/acceptance catalog | Medium | Snapshot, source binding, key lifecycle, drain/fencing, interrupted publication and original evidence tests pass |
| D2c | Immutable archive reader and independently verified base/increment restore | Medium to high | Full-history equivalence, old-library compatibility, privacy/receipt references and missing-key/object behavior pass |
| D2d | Optional hot/cold representation change and incremental ordinary reads | High | Enforceable immutable-prefix contract, atomic source CAS, deep independent audit and cross-platform failure drills pass |

Do not bundle these stages with a generic full-database purge. Introduce each
family separately. Keep original history while the archive/read/restore contract
is incomplete. Migration, wallet, privacy and unresolved paid-work evidence need
separate domain review before any future storage representation change.