# Workflow export publication directory

Schema 115 adds a source-bound publication directory. Logical identity is derived
from subject/workflow/genesis/schema and complete row/receipt digests, counts,
final-row hash and time high-water. Advancing unrelated event heads does not
create a second business export of the same source. An immutable preparation
event separately binds the exact source JSON, original event prefix and key
metadata. Caller request keys are digested and bound to the original version.

States are `prepared`, `published_durability_unknown` and `accepted`. A staged
record is not a trusted archive. Publication records retain their source and
key metadata; an accepted row is immutable. A validated manifest descriptor and
private acceptance event, appended in the same database transaction, establish
the independent accepted root. Private operator audit is part of that final
transaction. A rolled-back acceptance leaves the preparation retryable. Neither
randomized ciphertext hashes nor an artifact's self-declared hash define logical
business idempotency.

Directory reads verify the row hash, original preparation event, source identity
and acceptance receipt, including supported encrypted event payload archiving.
They return only bounded publication metadata and explicitly do not probe current
artifact availability. Catalog acceptance is not a fresh storage-health claim.
A missing accepted file must not cause automatic overwrite or duplicate export.

Archive key validation and health counts include prepared, published and accepted
workflow exports. Referenced key IDs/fingerprints cannot be reused or silently
removed; retained retired keys can decrypt earlier publications. The existing
keyring history remains authoritative. Legacy databases lacking this additive
table keep their old reference checks.

The new table is classified as subject-owned evidence for runtime exports and
as preserved lifecycle inventory. Historical privacy compatibility recovery
requires separate review when these artifacts exist; it cannot claim that a
workflow copy was removed with ordinary managed conversation exports. No source
history is deleted and no privacy permission is inferred from export acceptance.

Migration 115 preserves prior SQL/fingerprints, adds immutable-source/transition/
accepted-row guards, and uses the established backup/rollback and schema-contract
checks. This directory is not an artifact restore endpoint or a hot/cold cutoff.
Schema 116 adds a [separate increment metadata catalog](workflow-increment-metadata.md).
It retains these base records unchanged, uses distinct publication identities,
and adds no incremental file writer or base-reader compatibility shortcut.

New export/increment file publication is admitted only on platforms with the
implemented directory-fsync capability. Windows reports unsupported before new
or prepared file publication; catalog reads and retries of already published
requests retain their historical evidence semantics. See the API capability
fields for this distinction. This admission change does not migrate the catalogs
or revise any historical acceptance receipt.

## Manifest descriptors and catalog retries

Base and incremental catalogs use the same v1 manifest descriptor contract:
exactly `name`, `stored_bytes`, `stored_hash` and `plaintext_hash`; the fixed
`manifest.enc` name; an integer encrypted byte count within the manifest budget;
and two lowercase SHA-256 digests. Boolean byte counts are rejected. Descriptor
validation does not prove that an object exists or that storage is durable.

A catalog confirmation requires a Boolean durability observation and the original
frozen source/key binding. Every retry of a published record must name its original
manifest, including retries that do not advance status. Matching retries return
the existing record without an UPDATE or another acceptance event. Promotion of
an unknown publication preserves the original `manifest_json` text, including
valid historical whitespace and key order. New prepared publications still use
canonical JSON. Malformed historical descriptors fail validation without rewriting
the row or synthesizing a receipt. Existing artifact readback, ownership, privacy
and transaction fences remain required by the controlled publication interface.
These application checks do not change schema versions or the v1 archive format.

## Existing request recovery across reviewed schema changes

The controlled base and increment writers reconstruct the live history under its
actual strict schema before resuming an unfinished publication. An existing request
is recognized only through its original, validated request intent, before any new
preparation. A pure comparison checks every reconstructed business field against
the frozen source. It never edits either source or returns a version-normalized copy.
The writers and archive verifiers continue to use the original frozen source and
manifest; the original event-prefix check remains separate and required.

Cross-schema recovery additionally requires a directed, explicitly reviewed pair
of exact old/new DDL fingerprints and a shared, registered structure fingerprint.
Adjacent version numbers, unchanged tables alone and transitive pair composition
are insufficient. No operator parameter or environment setting enables a pair.
New requests keep their normal identity and same-schema parent checks. In
particular, a new increment cannot inherit permission to extend an old-schema
parent from another request's recovery approval. Historical accepted roots remain
readable under their original supported schema contract.

The comparison path is installed before its migration. The reviewed pair registry
is currently empty: no cross-schema exception is enabled and schema116 remains
unchanged. A future trigger-only migration must register its actual reviewed pair
and measured fingerprints in the same release. Test-only schema117 contracts
exercise real migration, file readback and process recovery without enabling that
schema in production. Schema115 and unreviewed future versions gain no implicit
compatibility. Business changes or an unverified actual schema still fail closed.
