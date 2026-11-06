# Increment metadata and dependency catalog

I1 adds schema 116 metadata for the reviewed incremental parent-chain protocol.
It does not publish incremental files or expose incremental HTTP controls. The
existing v1 base catalog, format, accepted reader and maintenance routes retain
their contracts. `wdelta_` identities are distinct from `wexport_` bases and cannot
be passed to the existing base artifact reader.

## Additive storage

`workflow_increment_publications` freezes subject/workflow, either a base parent
FK or an increment parent FK, the complete exact parent reference, full resulting
source commitment, first/last versions, depth and encryption key metadata.
`workflow_increment_requests` binds a request-key digest to parent, workflow,
expected resulting version, publication and private request receipt.

SQLite guards require an existing accepted compatible parent, the next
contiguous version range and a chain depth from 1 through 15. Inserts always
start prepared. Source/parent/key fields and requests are immutable; publications
and requests cannot be deleted. Published manifest descriptors stay frozen on
promotion, accepted publications are immutable, and invalid status transitions
are rejected. Foreign keys and dependency indexes retain parent and receipt
references; they do not authorize cleanup.

The schema-115 SQL and fingerprints remain unchanged. Migration 116 uses the
existing durable backup, transactional migration, schema verification and
rollback contract. Existing rows and base receipts are not rewritten. Old
runtimes refuse schema 116 before mutation. Reverting code alone is not a
schema downgrade; use a separately verified matching backup when planning a
rollout rollback.

An accepted source commits its exact schema. Old schema-115 bases remain
readable after migration, but a fresh source captured under schema 116 cannot
append to a schema-115 parent. Create a new complete base for the current schema
before an incremental chain. No source conversion or automatic rebasing occurs.

## Transaction primitives and trust

`WorkflowIncrementCatalog` is internal metadata infrastructure. Its caller owns
the source database transaction. Preparation requires a supported complete
source description, an independently accepted parent and a recorded, validated
keyring generation. It freezes a `noyra-workflow-increment/v1` artifact binding
and private preparation event. It does not prove source-prefix equivalence by
itself; the separate I2 writer must verify the coherent source and complete
accepted prefix before preparing an increment.

`get` verifies state hashes, original preparation/acceptance events and exact
parent references all the way to the base, with a 16-node total bound. Parent
reference fields bind the logical ID, source and key binding, full frozen source
JSON, exact manifest descriptor, acceptance event and payload, version and depth.
Parent status, scope, genesis, supported source schema and event provenance are
checked. These are catalog/evidence checks, not fresh file-health observations.
Archived event payloads use the existing authenticated event archive reader.

Logical identity uses a versioned domain, parent logical identity and complete
resulting source identity. Randomized ciphertext and unrelated advancing event
heads do not create another logical increment. Preparation binds the exact source
JSON and original key; duplicate logical sources retain them, including after
key rotation. Same-version content changes cannot retarget an existing request.
The `requested` lookup resolves the original request independently of later live
progress; using the key with another parent, workflow or version fails.

`finish` is a metadata transition primitive, not a publication control. Before
calling it, I2 must fully verify files outside the writer transaction, establish
actual directory durability, then recheck source/lease/ownership/migration,
privacy, keys and deadline in the final source transaction. The caller must
commit operator audit in that same transaction. This module neither performs
those file proofs nor asserts them on behalf of a runtime route. Tests of this
primitive use synthetic descriptors and do not claim real incremental artifacts
or platform durability. I1 has no caller that publishes incremental files.

A complete, independently verified publication can be unconfirmed or accepted.
A prepared/unconfirmed increment is never an eligible parent. Repeated terminal
metadata must present the same manifest; it cannot silently replace a missing or
damaged accepted file. A final transaction failure rolls back the transition
and its private acceptance event together.

## Dependencies and privacy

All three increment states participate in archive-key reference validation and
health counts. Referenced parent and child keys cannot be removed or reused;
retained retired keys remain valid. A new keyring generation must be recorded
before preparation, and keyring rollback checks retain their existing meaning.
The new tables have explicit subject ownership in runtime exports and explicit
preservation entries in lifecycle diagnostics. They add no retention candidate,
cutoff or delete permission.

The existing `operations.retention` integrity check is versioned to include
increment catalog/parent proofs and immutable request links. Missing request
intent, changed receipts, rehashed mutable metadata or broken parent bindings
cannot be accepted as healthy merely because a row's hash was recomputed.
No new critical check ID is introduced.

A deletion/erasure intent blocks new preparation and terminal metadata changes.
Historical conversation-export erasure cannot claim these workflow derivatives
were retired. This is a conservative review boundary; I1 does not implement
artifact retirement, reconstruct withdrawn content or override current privacy.
Unknown calls, fee reservations, model execution and ordinary autonomy policies
are untouched.

## Remaining implementation gates

I2 must implement the owned, paused/drained incremental file writer and the
atomic publication/audit protocol described above, with real source-prefix,
interrupt/restart and platform storage tests. I3 must implement explicit-tip
chain file reading, complete and ancestor-boundary proofs, isolated reconstruction
and compatible operator interfaces. Until both exist, the new catalog is
metadata infrastructure only. Hot history remains authoritative and retained.
