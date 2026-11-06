# Workflow recovery capacity status

The operator-only `GET /api/v1/admin/workflow-clock?workflow_id=<id>` and its
`/api/admin/...` alias now distinguish capacity observation from verified clock
history. The same response retains existing fields for histories within the
supported recovery bounds.

`DurableWorkflowStore.clock_catalog` checks the complete workflow's subject
ownership before exposing capacity. A missing workflow or foreign/mixed-owner
history is rejected. Within 50,000 records and 16 MiB of UTF-8 state JSON, the
existing complete history validation and original fingerprint are used. Status
is `verified` when clock evidence is complete, or `clock_evidence_required` for
otherwise valid historical inversions awaiting explicit evidence. Neither status
changes original rows, resumes a subject or contacts a provider.

When either existing bound is exceeded, the response is metadata only:

- `status` is `maintenance_required`.
- `history_verified` and `structure_verified` are false.
- `capacity` contains complete record/byte totals and the unchanged limits.
- `expected_fingerprint`, `unexplained_versions`, `unexplained_count` and
  `high_water_at` are null. An unknown inversion count is not shown as zero.
- `maintenance` explains the exceeded bounds and requires preservation.
  `execution_available` is false and `execution_route` is null because a
  supported production export/maintenance action is not yet available.

The oversized response does not claim hashes, transitions or receipts were
verified. The capacity metadata cannot authorize acknowledgement. The original
`clock_history` and `acknowledge_clock_history` retain full validation and the
same bounds; an attempted acknowledgement of an oversized history is rejected
without receipts or audit writes. No partial history fingerprint is generated.

Under-bound malformed state, version gaps, changed types and bad receipts remain
errors. Large histories remain eligible for the existing independent full audit;
this observation does not accept corruption or replace that audit. The response
contains no checkpoint state or private reason. Operator permissions, at-rest,
CSRF on writes, lifecycle/drain and migration controls retain their contracts.

This stage provides an honest maintenance explanation, not compaction, archive
acceptance, recovery or an incremental trust cache. Source histories, unknown
paid calls and fee reservations remain unchanged. The next stage must separately
implement and validate an owned export/acceptance protocol before any source
representation change is considered.
