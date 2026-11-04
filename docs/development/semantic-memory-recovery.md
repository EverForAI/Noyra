# Semantic memory integration recovery

Semantic memory integration has a durable business round independent of the
number of model calls, provider route or calendar day. New rounds use
`semantic_memory_integration:round:<number>`. Existing purposes in other cognitive
lanes and old-format historical calls are unchanged.

Before dispatch, a checkpoint records the ordered source memory IDs, revisions
and fingerprints, selected event metadata, generation options, prompt/schema
hashes and full context hash. It does not store another copy of private memory
text. The exact physical model call is bound to this checkpoint in the same
transaction that prepares the call. Failure to persist the binding prevents
provider dispatch. The hook is opt-in; ordinary model preparation is unchanged.

## Recovering a paid response

On restart, the integrator checks the unfinished round before selecting new
memories, applying the normal integration interval or checking a new-call quota.
A verified successful response is replayed locally without asking a provider or
router to send anything. Changing route/model configuration does not create a
replacement paid request.

Recovery reconstructs the original prompt from its recorded source revisions
and frozen event metadata. A new high-priority memory or event cannot silently
replace the original context. If a source has changed or been withdrawn, or the
prompt/schema has changed, the old response is rejected for local application.
The successful call, attempts and costs remain unchanged. Missing or corrupt
binding/response evidence fails closed instead of opening a new paid request.

The supervisor retains its source ownership, evidence and confidence checks.
Integration output, source revisions, integration record and round completion
commit in one transaction. A failure at any local write rolls them all back,
leaving the paid response recoverable. A completed round cannot apply twice.
Later legitimate revisions or reversal of an output do not rewrite its original
successful round; verification uses the original output revision.

Unknown/executing logical calls and currently active attempts still require the
existing reconciliation protocol. An explicitly authorized retry may later
succeed and be applied, while the earlier unknown attempt and its fee reservation
remain unchanged. Historical unknown usage is not a claim that the new successful
response is unknown. An unfinished one-shot operator retry cannot silently
become a new round. Cancellation and lifecycle/lease invalidation preserve the
same constraints. A nonblocking per-subject process lock prevents concurrent
workers from dispatching or applying a second copy. Existing permanent budgets
and temporary recovery reservations remain authoritative.

## Pre-upgrade calls without an original-context binding

Old calls did not record this business-round context. The code cannot determine
from nearby timestamps whether a successful old response was already integrated,
was intentionally rejected, or remained unapplied. It does not guess or apply it
to today's memories. Such old prepared/executing/unknown/successful calls place
this integration lane in `memory_integration_legacy_recovery_required`. The same
holds for an old failed one-shot retry whose explicit reconciliation is still
open. Other
cognitive lanes retain their existing operation. This can include old successful
calls whose local use is already known to the operator. No old row is migrated,
rewritten or silently marked consumed.

An operator can use the bounded recovery directory to review these cases once.
This is maintenance of historical ambiguity, not approval of each future normal
integration. For a successful old response, independently review whether its
local result should be discarded. Discarding that local result never changes
the call's successful status, releases its costs, claims an integration succeeded,
resends it or resumes the runtime. When new work later becomes due, Noyra can
start a separate normal round under its existing limits.

Unknown calls cannot be discarded through this endpoint. Resolve their actual
provider/attempt ambiguity first using the established model controls. Do not
submit a guessed outcome merely to clear the directory.

## Operator API

The `/api/admin/...` and `/api/v1/admin/...` paths use the same contract. Both
endpoints require operator authentication. Cookie writes also require the normal
CSRF header. Detailed model response text, memory text, credentials and operator
explanations are excluded from the recovery directory.

`GET /api/v1/admin/memory-integration-recovery` returns up to 100 pending records
plus a truncation flag and subject-wide unresolved-work flags. Each record
contains `call_id`, original purpose/status, an `expected_fingerprint`, and
`can_discard`. Previously acknowledged records are verified before being omitted
from this pending list, so completing one page exposes the next page.

`POST /api/v1/admin/memory-integration-recovery/discard-legacy` accepts exactly:

```json
{
  "call_id": "<old successful call ID>",
  "expected_fingerprint": "<64 lowercase hex characters from the directory>",
  "evidence_sha256": "<digest of independently retained review evidence>",
  "reason": "<nonempty explanation, at most 2000 characters>"
}
```

The evidence digest attests to an operator's independent review; a digest alone
is not proof of an external fact. The operation requires owned, manually paused,
fully drained runtime, no unresolved lower-layer work and usable existing
integrity/storage boundaries. Quarantine and migration fencing prevent this
operation. Receipt and operator audit append atomically. Conflicting snapshots
are rejected. Repeating the same already accepted snapshot is idempotent.

The private receipt is bound to the unchanged logical call, remains readable
through supported encrypted event archiving, and does not contain a duplicate
model response. Raw histories, source memories and accounting remain available
for later independent review.
