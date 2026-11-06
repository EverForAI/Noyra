# Incremental workflow archive protocol: reviewed design

Status: the design and isolated fault prototype are complete. I1 now supplies
[schema-116 metadata and dependency protection](workflow-increment-metadata.md).
Incremental file publication (I2) and chain file reading (I3) are not implemented.
Existing v1 complete bases and their accepted evidence reader remain available.
No incremental HTTP control, source cutoff or runtime restore is enabled. Raw
prototype code and validation evidence remain outside the public source tree.

## Scope and compatibility

An increment carries only the next contiguous checkpoint records and their
original clock receipts. Its source commitment covers the complete resulting
history, including the accepted parent prefix. Base plus increments must equal
the original canonical complete-history array, receipt array, byte counts,
last-row hash and clock high-water. A workflow artifact remains narrower than
a full subject backup. It grants no dispatch, activation or financial authority.

Existing v1 bases and schema-115 catalog rows stay immutable and retain their
current readers. An experimental delta uses a separate format discriminator and
logical identity domain. Schema 116 adds a separate,
versioned metadata catalog. File publication and reading still need explicit
base/delta dispatch; a delta cannot be inserted into the base catalog or
interpreted as a v1 base. Unsupported
formats fail closed. There is no silent schema translation across a chain.

## Trust root and exact parent reference

The operator selects an explicit independently accepted tip. The reader resolves
parents through protected catalog records and their preparation/acceptance
receipts, never through file paths or trust roots supplied by a manifest.
`prepared` and `published_durability_unknown` are ineligible parents. Verifying a
file cannot promote an unconfirmed parent.

A child's frozen preparation binds this exact parent reference:

- Parent logical publication identity.
- Canonical hash of the parent's frozen source and key binding.
- Canonical hash of the full parent source JSON, including its event provenance.
- Exact encrypted manifest descriptor hash and independent acceptance receipt hash.
- Accepted final checkpoint version and explicit chain depth.

The parent reference is included both in protected preparation metadata and in
the encrypted child manifest. The reader compares them exactly. A parent with the
same last row, record count or apparent filename cannot substitute for it.
Changing ciphertext or an acceptance receipt cannot redefine an existing parent.

Logical child identity is derived from a versioned domain, parent logical ID and
the complete resulting source identity. It excludes randomized ciphertext hashes,
request timestamps and unrelated advancing event heads. An independent receipt
binds the exact encoding, full frozen source JSON and key metadata. Thus business
idempotency and byte-for-byte evidence binding are distinct requirements.

The production request contract must also pin a private request-key digest to
parent ID, workflow and expected resulting version. Reusing a key for another
intent fails. Retrying an accepted request verifies the originally selected
artifact even after later live progress; retrying a preparation cannot silently
retarget a newer source. The isolated prototype exercises source-bound logical
idempotency; I1 now persists request intents and receipts; operator retry dispatch remains
an I2 control gate.

## Creating a candidate increment

1. Require owned, manually paused, drained maintenance admission with a current
   lease, clear migration/shutdown/quarantine fences, usable protected storage,
   retained keys and no unresolved privacy derivative review. Keep unknown paid
   calls and their reservations unchanged.
2. Resolve and fully verify the accepted parent chain. The tip must match the
   current subject, workflow, genesis and supported schema contract.
3. Capture one coherent SQLite backup including committed WAL state. Verify the
   complete current source, not merely the proposed suffix. Recheck the parent's
   original event-prefix anchor in this snapshot.
4. Recompute the complete current prefix through the parent's final version and
   compare all original row and receipt digests, byte/record totals, last-row hash
   and clock high-water to the accepted parent. Rehashed older content must fail
   even if the final parent row is unchanged and the current source is internally
   self-consistent. Until prefix immutability is enforceable, no cached green
   report may replace this scan.
5. Require at least one new checkpoint. The suffix starts exactly at
   `parent.end_version + 1` and ends at the frozen source version. Freeze the
   parent reference, source, range, depth and new object's key ID/fingerprint in
   protected preparation before writing any candidate segment.
6. Write encrypted bounded segments, then the encrypted manifest. AAD binds each
   object's logical publication and canonical name. The suffix preserves original
   state JSON, reasons, timestamps, receipt metadata and payloads. It never
   reconstructs or invents the source's complete event chain.
7. Read the whole candidate chain and reconstruct an isolated evidence view.
   Verify its final complete proof and every intermediate accepted prefix before
   exposing any record. A valid final proof alone is insufficient to establish
   all ancestor commitments.
8. Confirm directory durability where supported, then recheck source freshness,
   lease, ownership, privacy, key references and deadline immediately before the
   short final acceptance transaction. Store accepted state, receipt and operator
   audit atomically. All artifact I/O stays outside that writer transaction.

The isolated prototype's metadata authority is a separate synthetic SQLite file.
It models receipt/state atomicity without modifying production schema 115. It
is not a proposed two-database production commit protocol. Production acceptance
must participate in the owned source database transaction, with its existing
source-version fencing and audit contract.

## Chain reader and isolated reconstruction

Resolve the selected tip backwards to a base while checking accepted status,
canonical parent references, uniqueness and a depth bound. Reverse the resulting
chain for reading. Never infer the tip from timestamps, filenames or the largest
observed count. Siblings can both be valid historical branches; they must not be
implicitly concatenated or selected by "newest" wall time.

Each link must preserve subject, workflow, genesis and supported schema. Versions
are positive exact integers; booleans and numeric lookalikes are not ranges.
There are no gaps, overlaps or reordered segments. Event provenance cannot move
backwards. Clock ordering follows checkpoint versions and original rollback
receipts, not a sort by wall-clock time. Each new link carries its own encryption
key binding; parent keys are never inferred from the newest key.

Before exposing a view, verify the ordered complete row stream and original
receipts against the selected tip, then read back the isolated SQLite records
and verify the complete proof at each ancestor boundary. Reconstruction stays
in memory, has a non-runtime evidence schema, becomes query-only and is discarded
on cancellation or exit. Partial reconstruction is never a successful read.
Current privacy/lease/key controls must be rechecked before exposure and during
any production context-bound reads.

The prototype bounds a chain to 16 nodes (one base and up to 15 deltas), 4,096
segments across the whole chain, existing per-object/per-source bundle limits,
512 MiB of canonical reconstructed record text and a 768-MiB SQLite page ceiling.
Limits are cumulative, including the base. They are conservative experimental
bounds, not a process-RSS guarantee or an established production capacity SLA.
A production deadline and SQL progress/cancellation checks remain mandatory.

## Publication, retries and failure outcomes

| Boundary | Required outcome |
|---|---|
| Before or during segment/manifest write | Preserve the prior accepted chain and a retryable preparation; no acceptance. |
| Complete readback fails | Discard isolated output; retain source and preparation; no promotion. |
| Directory durability is unknown | Record unconfirmed publication; it cannot become a parent. |
| Source, privacy, key or lease changes | Refuse acceptance/exposure; retain original financial and source evidence. |
| Acceptance receipt or row write fails | Roll back receipt, catalog transition and audit together. |
| Process exits after manifest | A new process resolves the original intent and fully verifies before accepting. |
| Accepted object is missing/damaged | Refuse readback; no automatic overwrite or silent rebasing. |
| Repeated verified source/parent | Same logical publication; no extra accepted receipt or file rewrite. |

A published but unconfirmed object may be promoted only after rechecking its
frozen source, exact parent chain, complete readback and actual durability. It
cannot use a new encoding to repair an already accepted object. A preparation
with a superseded source remains evidence for explicit recovery; it cannot be
silently reset to a different source or key.

## Key lifecycle, privacy and retention

All prepared, unconfirmed and accepted children retain their encryption keys and
parent references. Parent objects and receipts remain live dependencies as long
as a child depends on them. The keyring/health/runtime-export/lifecycle inventories
must include the new catalog before any production incremental write is enabled.
Retained retired keys can decrypt their own links; a missing parent key rejects
the entire chain. Rotation does not rewrite accepted parents or change a frozen
preparation's key. Every staged and accepted derivative must be included in
privacy review; inability to prove retirement cannot be reported as erasure.

Current source deletion/withdrawal, admission and migration controls remain
authoritative over old copied content. An archive does not revive permission.
No age-based chain deletion, parent pruning, hot-prefix cutoff or automatic
rebasing is authorized by this design. A future refreshed base would need its
own complete proof and independent acceptance before any separately reviewed
retirement policy.

## Validation result and implementation gates

The isolated prototype exercises original v1 bases, two-level increments,
clock rollback receipts, branch selection, exact parent dependencies, prefix
changes, encrypted object/receipt failures, key rotation, cancellation, cumulative
budgets and interrupted acceptance. Separate non-root POSIX checks use real
directory fsync and actual process exit/restart. Windows acceptance fault tests
simulate successful flush; they do not establish Windows directory durability.
These checks support the protocol's data/failure model, not production readiness.
The synthetic registry does not implement source-catalog migration, request-key
recovery routing, full keyring generation/anti-rollback dependency SQL, persistent
privacy retirement or the production final lease/transaction lock order. Its
metadata and file integrity assume the synthetic authority itself is protected;
rewriting the entire authority cannot be detected using hashes stored within it.
These boundaries must not be represented as completed production integration.

The reader prototype performs a separate complete prefix pass for each chain
node. With the explicit depth cap this is bounded, but its worst-case work grows
with chain depth times complete history size. Production capacity/deadline tests
must set an operational chain bound or adopt a separately verified one-pass
multi-boundary proof. A performance shortcut cannot skip ancestor evidence.

I1 has completed the additive metadata catalog/migration, frozen request intents
and parent/key/runtime-export/lifecycle/integrity/privacy dependency integration.
Before incremental file publication and reading can ship, independently complete:

1. Integrate I1 metadata with I2 coherent source/prefix verification and request
   dispatch; metadata primitives alone do not prove file publication.
2. Existing owned control and same-source-transaction acceptance/audit fencing,
   deadline/revocation checks, actual platform storage and key lifecycle validation.
3. Production reader/HTTP compatibility, old-library rejection, bounded chain
   controls, cross-platform regression and source preservation review.

Keep hot history throughout these gates. This design adds no default network
allowlists, per-message human approval or restrictions on ordinary autonomy.
