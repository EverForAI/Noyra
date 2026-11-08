# Verify an accepted workflow archive

`POST /api/v1/admin/workflow-exports/verify` (also `/api/admin/...`) performs a
complete read and an ephemeral isolated reconstruction of one accepted base.
The only JSON field is `publication_id`, the canonical `wexport_` identifier
returned by the publication catalog. Operator permission is required; read-only
admin tokens are insufficient. Cookie requests require the normal CSRF header.
No destination path, arbitrary manifest, key, SQL or restore action is accepted.

```json
{"publication_id":"wexport_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}
```

The instance must be manually paused, owned and drained, with clear migration
and quarantine fences, usable protected storage and no blocking integrity or
privacy review. The method checks the current schema contract, publication and
private preparation/acceptance receipts, source genesis, supported source-schema
fingerprint, key references and event-chain metadata before reading. A bundle's
own manifest cannot authorize itself. A `prepared` or
`published_durability_unknown` record is rejected; verification never promotes it.
Windows publication durability remains unknown, as documented by the export API.

Encrypted artifacts are opened with creation disabled. Missing/corrupt objects,
unavailable keys and unsupported source versions fail; no replacement or repair
is attempted. Retained retired keys and supported encrypted archival of receipt
events remain usable. Existing version-1 bases and schema-115 source identities
retain their format; no schema migration is introduced by this reader.

All records and original clock receipts are verified in order into an in-memory
SQLite evidence database. Nothing is exposed until the complete source-bound
file proof passes and all reconstructed SQLite records pass a second full
verification. The evidence schema is distinct from runtime storage and becomes
query-only. It is closed and discarded when the request ends. Original timestamps
and receipt metadata remain evidence; they do not recreate the source event chain.

After reconstruction, the control rechecks the independent root, privacy and key
state. The internal context reader also rechecks admission/privacy/root/key on
access. A new privacy withdrawal or revoked lease prevents later exposure. An
accepted frozen workflow need not match the latest live checkpoint: later
legitimate source progress does not rewrite historical evidence.

A successful response contains bounded metadata only: publication/workflow IDs,
record/byte/receipt counts, source schema version, explicit workflow-only scope,
`complete_history_verified=true`, `isolated_reconstruction_verified=true`,
`storage=ephemeral_memory_sqlite`, `source_history_preserved=true` and
`restore_or_dispatch_authorized=false`. Checkpoint state, private reasons,
receipts, keys, paths and manifest descriptors are not returned by HTTP. No source
catalog, acceptance, keyring revision or audit row is appended by this operation.

Reconstruction retains the export's existing format limits and adds a 512-MiB
canonical record-text limit and a 768-MiB SQLite page ceiling. These do not imply
an RSS cap for JSON decoding. Internal pages have at most 100 records and 16 MiB
of canonical record text. A 120-second cooperative deadline bounds checking;
SQLite progress callbacks cover long metadata scans. OS filesystem calls cannot
be forcibly preempted, but expired work cannot return a successful verification.

Response codes retain the export API's contract: malformed JSON fields 400,
operator/CSRF failure 401, rejected acceptance/schema/evidence or request conflict
409, non-JSON 415, and unavailable lifecycle/storage/key/deadline controls 503.
Existing rate and body limits also apply. A failed verification leaves source
history and artifacts untouched and releases its tracked operation.

This endpoint proves readability of the selected base at the time of checking.
It does not download data, create a persistent restore file, recover a complete
instance, activate work, settle unknown calls, release reservations, delete hot
history or switch production reads. Incremental parent chains and runtime restore
remain separate implementation gates.
