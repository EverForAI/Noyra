# Controlled incremental workflow publication

Schema 116 metadata now has a controlled file publisher. It preserves every
source checkpoint and original clock receipt. Increment publication does not
resume a subject, dispatch a model, settle unknown calls, release fees, switch
ordinary reads, remove hot history or restore a runtime.

## Routes and authorization

- `POST /api/v1/admin/workflow-increments` publishes or verifies a frozen increment.
- `GET /api/v1/admin/workflow-increments?limit=50` lists recent metadata.
- Existing `/api/admin/...` aliases behave identically.

Both routes require operator permission. Read-only admin tokens are insufficient;
cookie POST requests also require the normal CSRF header. GET accepts only one
`limit` integer from 1 through 100. It is a bounded latest directory, not a total
count or a fresh artifact-health check.

POST requires exactly these JSON fields:

```json
{
  "workflow_id": "example-workflow",
  "parent_id": "wexport_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "expected_version": 12,
  "request_key": "increment-maintenance-001",
  "reason": "Preserve the next workflow history range"
}
```

`parent_id` must identify an accepted `wexport_` base or `wdelta_` increment.
`expected_version` is the complete resulting final checkpoint version, an exact
integer of at least 2. `request_key` is 8 to 128 ASCII letters, digits, underscores
or hyphens. No target path, user-supplied manifest or key is accepted.

Request lookup precedes inspection of the live workflow. Repeating an accepted
request verifies its originally frozen chain even after legitimate later source
progress. A preparation or unconfirmed publication requires the current source
to match its frozen complete identity. An original request cannot retarget a
new parent, version or changed history. Another key naming the same logical
source/parent resolves to the same publication, preserving its original key and
frozen source. Keep the original request key for recovery.

## Admission and coherent source proof

Publication requires manual pause, exclusive runtime ownership, all other tracked
work drained, a current lease, clear migration/shutdown/quarantine fences and
usable at-rest/key/integrity state. Privacy deletion or historical erasure intent
blocks new copies and acceptance. Current source identity, exact parent schema
and subject genesis must agree. An old accepted schema-115 base is still readable
but cannot receive a suffix from a schema-116 source; create a new complete base
for that schema first. No automatic rebase is performed.

For a new request, the control captures a coherent SQLite backup including WAL
commits and verifies the complete source plus every accepted parent dependency.
It recomputes the complete original prefix through the selected parent version.
Rehashed earlier content fails even when the latest parent row is unchanged.
After freezing preparation and request metadata, a second source capture confirms
that the original complete identity still matches before encoding the suffix.

Only versions `parent.end + 1` through `expected_version` enter new encrypted
segments. AAD binds the distinct increment publication ID and canonical object
name. Existing base files stay in their original namespace; increments use a
separate private `workflow-increments` directory. Keys, permissions, path/reparse
and hard-link checks reuse the reviewed archive boundary.

## Complete readback and atomic acceptance

The publisher reads the actual base and increment files, verifies the complete
result and rechecks every accepted ancestor boundary using isolated in-memory
SQLite evidence. Nothing is returned from this internal record stream. This is
writer verification. I3 now adds a separate
[accepted chain evidence verifier](workflow-chain-verification.md) and internal
context-bound reader; these do not restore a runtime. The proof preserves original timestamps and rollback receipts and never
claims to reproduce the source's complete event chain.

All archive write/readback and directory fsync occur outside the final source
writer transaction. Archived preparation/request/parent receipts are verified
before writer scopes and retained in a bounded operation-local proof map. A
writer scope refuses unprimed or changed archived references rather than opening
files or treating the current archive as newly verified. The operation-local map
is not a persistent trust cache and cannot skip the initial complete proof.

The final short transaction acquires the established control/lease/ownership
scopes, compares `PRAGMA data_version` on the same connection retained from
before final capture, and rechecks privacy, request binding, key references and
deadline. Any intervening database writer, including an unrelated diagnostic,
causes a conservative conflict; retry the original request after concurrent work
drains. Accepted state, private acceptance event and operator audit commit or
roll back together. No file I/O is used to repair a conflict in that transaction.

## States and responses

| State | Meaning |
|---|---|
| `prepared` | Frozen source/parent/key and request evidence exist; files or readback may be incomplete. |
| `published_durability_unknown` | The complete files passed readback but directory durability was not confirmed. |
| `accepted` | Complete chain readback and directory durability passed; independent acceptance and private audit committed atomically. |

Windows currently produces `published_durability_unknown` for a new actual
publication because directory fsync is not confirmed. POSIX can reach `accepted`
when real directory fsync succeeds through the protected data root. An unconfirmed
publication cannot become a parent. Promotion requires an unchanged source,
complete readback and confirmed durability, without rewriting published files.
Mocked acceptance tests do not establish Windows physical durability.

Accepted retries never silently overwrite missing/damaged objects. The directory
can still show accepted metadata while a fresh POST reports file unavailability;
`artifact_observation=not_checked_by_catalog_read` makes that distinction explicit.

POST returns bounded public metadata: publication/workflow/parent IDs, status,
version range, depth, suffix and complete row counts, state byte total and times.
It adds `readback_verified`, `durability_confirmed` and `idempotent`.
`durability_confirmed` is true only for accepted; unchanged successful repeats
have `idempotent=true`. State JSON, original reasons/receipts, keys, descriptors
and local paths are not returned. `source_history_preserved=true` and
`restore_or_dispatch_authorized=false` remain explicit.

## Limits and failures

The existing source/chunk budgets apply. A chain contains at most 16 nodes and
4,096 total segments including its base. Reconstruction permits 512 MiB of
canonical record text and at most 768 MiB of SQLite pages; these are not an RSS
cap. Full prefix/ancestor verification is conservative and can cost depth times
complete history. Oversized work fails instead of truncating proof.

The cooperative deadline is 120 seconds, checked during backup, source/row/chunk
work, after file synchronization, after acquiring the writer and before commit.
Snapshot SQL scans have a progress deadline. A blocked OS file call cannot be
forcibly interrupted; once it returns expired work is refused. Any failure
releases admission, keeps original history and financial reservations, and leaves
reviewable preparation or unconfirmed metadata for the original intent.

- 400: malformed field types/shape or invalid directory query.
- 401: operator permission or cookie CSRF failure.
- 409: source/version/request conflict or invalid independent evidence/schema.
- 415: POST content type is not JSON.
- 503: ownership/lifecycle/migration/key/storage/deadline control unavailable.

Existing transport body/rate limits also apply. No automatic infinite retry,
implicit source change, parent pruning, full-instance restore or hot/cold cutoff
is introduced by this API.

The separate `POST /api/v1/admin/workflow-increments/verify` route accepts only
`tip_id`. Publication requests on this page retain their original fields.
