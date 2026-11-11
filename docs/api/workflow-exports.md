# Controlled workflow export

Schema 115 provides an operator-only encrypted complete-history base export for
one workflow. This is a maintenance copy, not a full-instance backup or restore
endpoint. Source checkpoints, clock receipts, unknown calls and fee reservations
are preserved. It does not acknowledge clock exceptions or enable bounded normal
recovery of an oversized history. No provider request is made.

## Routes and permission

- `GET /api/v1/admin/workflow-exports?limit=50` lists the latest publications.
- `POST /api/v1/admin/workflow-exports` publishes or verifies a frozen export.
- The existing `/api/admin/...` aliases have the same contract.

Both routes require operator credentials; a read-only administrative token is
insufficient. Cookie-authenticated POST requests also require the normal CSRF
token. GET accepts only one `limit` integer from 1 through 100 (default 50).
The response marks truncation; it is not a total publication count.

POST requires exactly these JSON fields:

```json
{
  "workflow_id": "example-workflow",
  "expected_version": 12,
  "request_key": "maintenance-export-001",
  "reason": "Create a preserved workflow maintenance copy"
}
```

`expected_version` is the complete final checkpoint version and must be a
positive integer, not a boolean. `request_key` is 8 to 128 ASCII letters, digits,
underscores or hyphens. Retain it for retries. Reusing it for another workflow or
version fails. Different keys for the same complete source resolve to one logical
publication. A key digest and frozen source binding are kept in private receipts.

## Admission and consistency

The instance must be manually paused and own its runtime lock. The export is a
tracked operation and requires all other admitted work to be drained. Quarantine,
shutdown, migration fencing, unavailable protected storage or blocking integrity
health prevent publication. A privacy deletion or historical erasure intent
requires derivative review before making a new copy. Unrelated unknown paid
calls remain unresolved and reserved; exporting does not replay them.

Source capture uses a coherent SQLite backup, including committed WAL data, with
complete schema, subject/genesis, history, clock-receipt and event-prefix checks.
Before publication or promotion, the current complete source must match the
frozen record. File serialization, encryption and full readback happen outside
the writer transaction. The final short transaction rechecks lease, ownership,
storage/key state and the same connection's SQLite data version. Any intervening
writer, including an unrelated diagnostic writer, causes a conservative conflict.
Retry the original key after concurrent work has drained. There is no automatic
retry storm. If accepted already, retry verifies the original artifact; it does
not require the current workflow to remain frozen forever.

The work deadline is 120 seconds, with SQL progress and row/chunk checkpoints;
it is not a hard operating-system interruption of a blocked filesystem call.
Format budgets are documented in
[the bundle contract](../development/workflow-export-format.md). A timeout,
limit, invalidation or storage error leaves the original history and recoverable
preparation intact, without acceptance.

## Publication states

| State | Meaning |
|---|---|
| `prepared` | Source and key binding are durable; files or readback may be incomplete. |
| `published_durability_unknown` | Complete encrypted files were verified; directory durability was not confirmed. |
| `accepted` | Complete readback and directory durability checks passed; the independent acceptance receipt and private operator audit committed together. |

New publications and incomplete `prepared` retries require POSIX directory-fsync
capability. Windows rejects them with HTTP 503 `workflow_publication_unsupported`
before allocating an intent, recording a new archive key revision or creating files.
GET returns `creation_capability` with the reason and keeps historical metadata readable.
Existing published requests retain idempotent verification: an unknown publication is not
promoted without confirmed durability, and an already accepted publication keeps its original
receipt. Starting a new request key still requires creation capability. This does not implement
native Windows directory durability. POSIX support permits an attempt; actual fsync/readback
must still pass before acceptance. No status authorizes restore or source cutoff.

POST returns the public publication metadata plus `idempotent`,
`readback_verified` and `durability_confirmed`. First publication or promotion has
`idempotent=false`; a verified unchanged publication has `idempotent=true`.
`durability_confirmed` is true only for `accepted`.

GET validates catalog state and independent receipts, including supported
archived event payloads. It returns metadata, never checkpoint state, private
reasons, keys, manifest descriptors or local paths. Its
`artifact_observation=not_checked_by_catalog_read` explicitly distinguishes
catalog acceptance from a fresh file-health check. An accepted missing or damaged
object fails POST readback and is never automatically replaced.

## Errors and operational boundaries

- 400: malformed field types/shape or invalid directory query.
- 401: operator permission or cookie CSRF validation failed.
- 409: source/version/request conflict or invalid export evidence.
- 415: POST content type is not JSON.
- 503: ownership, lifecycle, migration, protected storage, key, deadline or storage
  control unavailable. Existing transport rate/body limits also apply.

Schema 115 is additive and retains the existing migration backup/rollback
contract. Historical migration SQL/fingerprints remain frozen. A runtime that
supports only an older schema must refuse the newer database; do not downgrade
its schema version manually. Keep referenced archive keys, including retained
retired keys, and include encrypted artifacts in any separately planned backup.
This API does not download objects, delete artifacts, rotate keys or restore a
runtime. D2c now provides a separate
[accepted archive verification endpoint](workflow-archive-verification.md) and
context-bound internal reader with ephemeral isolated reconstruction. Increment
chains, persistent/full-instance restore and runtime activation remain separate
work.
