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