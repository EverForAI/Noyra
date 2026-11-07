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
  `execution_available` is true and `execution_route` is
  `/api/v1/admin/workflow-exports`. This advertises the supported D2b control,
  not permission to execute immediately. `execution_readiness` requires a
  manually paused, owned, drained runtime and a usable archive key.
  `next_action` directs controlled export while preserving source history.

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

This observation remains metadata only. The separate
[controlled export API](workflow-exports.md) performs complete source and artifact
verification before recording publication or acceptance; the capacity response
cannot substitute for that verification. Source histories, unknown paid calls
and fee reservations remain unchanged. Export acceptance does not remove these
ordinary recovery bounds or authorize compaction, acknowledgement, recovery,
resume or a source representation change.
