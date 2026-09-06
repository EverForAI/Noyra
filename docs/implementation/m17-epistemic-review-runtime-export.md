# M17: Epistemic Review and Developer Runtime Export

M17 adds an awake epistemic review cycle and a complete developer-facing runtime export. The
model remains a proposal component: it cannot directly overwrite beliefs, settle predictions, or
choose evidence outside the locally supplied analyzed-observation set.

## Belief revision

- Only existing non-retracted beliefs are eligible.
- A proposal must explicitly choose strengthen, weaken, qualify, contest, or retract.
- Every revision cites analyzed observations whose immutable events become belief evidence.
- Confidence can change by at most `NOYRA_MAX_BELIEF_CONFIDENCE_DELTA` per review.
- Strengthening requires supporting evidence; weakening, qualification, contesting, and
  retraction require counterevidence.
- Revisions are appended to `belief_revisions`; existing history is never overwritten.

## Prediction review

- Only open predictions whose target time has passed enter the review context.
- True or false settlement requires analyzed observation evidence.
- Insufficient evidence leaves the prediction open rather than guessing an outcome.
- Resolution uses the existing append-only prediction review and Brier scoring path.
- Recent calibrated predictions and deterministic strategy outcomes are included in future review
  context.

Epistemic review runs are append-only and idempotent per triggering analyzed observation. They run
after higher-priority outcome, goal-governance, research, and action work, so an empty model queue
cannot block established runtime stages.

## Complete runtime logs

`GET /api/runtime-logs` provides an authenticated, paginated operational timeline. It combines
events, model calls, actions, epistemic reviews, and audit entries without publishing the private
records through the public projection.

`POST /api/admin/export-jobs` is the canonical authenticated export API. Its `kind` is `runtime` or
`training`; clients poll `GET /api/admin/export-jobs/{job_id}` and download the completed artifact
from `/download`. The historical `GET /api/admin/runtime-export` and
`GET /api/admin/training-export` routes remain authenticated compatibility shims, but now return a
`202` JSON job record instead of doing a synchronous ZIP response. This keeps a large archive off
the bounded HTTP request workers.

The runtime archive contains one JSONL file per subject-owned runtime table plus a manifest with
schema version, table counts, row counts, file hashes, export identity, and creation time. Long
serialization reads from a same-volume SQLite backup image: the live database is held only for the
bounded backup operation, then closed before compression begins. The export action is appended to
`audit_records` after the archive is built. For background jobs, a shared publication decision lock
serializes cancellation against the transaction that renames the ZIP, appends this audit row, and
marks the job completed. Shutdown cooperatively cancels and joins every export worker before the
subject process lock is released. Abandoned backup images are conservatively scavenged
after a grace period when they are not locked, and storage accounting includes active or orphaned
images while they exist.

For schema 33, the manifest also identifies ownership graph version 1. The graph explicitly classifies
98 direct `subject_id` tables and 28 revision/child tables through their declared parent key; common
knowledge packages and trusted keys use two subject publisher/import predicates. `schema_meta` is reduced
to the `schema_version` key, while six rebuildable memory FTS tables are recorded as `skipped`. The
`table_inventory` reconciles expected and exported rows and records referential gaps. Unknown tables,
missing graph entries, undeclared parent edges, count mismatches, or cross-subject foreign-key references
fail closed before the archive is published.

The exporter never reads environment variables or the secret directory. Search key references,
tokens, credentials, password-like fields, Bearer values, and credential-shaped text are redacted.
Search-provider fingerprints may remain because they are non-secret diagnostic identifiers. The
archive still contains private psychology, communications, model outputs, observations, goals,
sleep records, and action results; operators must store it on encrypted media and restrict access.

Training export is a separate derived format (`noyra-training-dataset-v3`). Its long-history path
uses the managed data volume, disk-backed exact deduplication, bounded episodes, JSONL shards, and
per-shard row/byte/hash entries in the manifest. Work, archive, subject-quota, and free-space
budgets are enforced while streaming; compressed event/model/cold-archive payloads are bounded
before decoding. The compatibility byte API is capped before publication and does not read an
over-sized archive into memory. Training consent and export-worker cancellation still use the
publication and ownership boundaries described in the M42 remediation records.

Set `NOYRA_DEVELOPER_LOG_EXPORT_ENABLED=false` to remove the export endpoint when the runtime no
longer needs development-grade introspection.
