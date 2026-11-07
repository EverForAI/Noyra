# Operator sleep recovery

Use normal early wake for a healthy deep sleep. Aborting sleep is an explicit
abnormal exit for a failed or unavailable stage. It pauses the runtime and never
claims successful reflection or restores fatigue. Normal autonomous messages and
external actions do not require this maintenance control.

Both `/api/admin/...` and `/api/v1/admin/...` use this contract. An operator bearer
token or operator session is required; session writes also require the usual
CSRF header. Read-only tokens cannot access the directory or mutate a run.

`GET /api/v1/admin/sleep-recovery` returns the lifecycle and latest sleep run's
`sleep_id`, `status`, `expected_version`, timestamps and a
`reflection_committed` boolean. It omits the private reason, reflection contents,
model responses, credentials and operator explanations. The directory is
informational; the write rechecks the live state.

`POST /api/v1/admin/sleep-recovery/abort` accepts exactly:

```json
{
  "sleep_id": "<run ID from the directory>",
  "expected_version": 2,
  "reason": "<nonempty operator explanation, at most 2000 characters>"
}
```

The original version must still identify this subject's nonterminal run. The
runtime must be owned, outside integrity quarantine and migration fencing, with
no in-flight admitted operation. This includes a pending sleep callback: an
operator cannot cut across its tracked lease. A stale version or a completed run
is rejected. Existing integrity and storage readiness requirements remain.

The run failure, matching lifecycle pause, cognitive-wait cancellation, private
failure receipt and operator audit commit atomically. An audit or persistence
failure leaves the original run unchanged. The admission gate is invalidated
after the successful transaction while the control scope still serializes other
work. Repeating the accepted original snapshot is idempotent and does not pause
a later run or append another audit.

Unknown model outcomes and fee reservations remain available for the established
model reconciliation controls. Cancelling their parent wait is not a model
cancellation or fee refund. Do not invent an outcome to enable this operation.
After resolving the underlying fault, use the existing lifecycle resume control;
it retains its normal health and fencing requirements. A new sleep has a new ID
and does not replay the failed sleep's request.

Malformed fields return 400; missing runs return 404; stale or ineligible work
returns a conflict. Ownership, migration, quarantine and storage failures retain
the management transport's existing unavailable/fenced responses. A damaged
sleep history cannot be made healthy by this endpoint.
