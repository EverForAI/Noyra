# Model failures before dispatch

The model gateway distinguishes local failures before dispatch from failures
that may have happened after a provider accepted a request. An exception type
alone does not prove that a request was unsent.

## Phase and exception handling

A failed attempt-authorization transaction has not returned an owned attempt.
The gateway uses the existing guarded `fail_before_attempt` operation to close
only an unclaimed prepared call or its own settled retry boundary. If an active
attempt did commit but its return is uncertain, it stays available to normal
conservative recovery. The gateway does not guess which attempt to cancel.

Once authorization returns an attempt ID, admission observers, attempt start,
and initial authorization checks still precede provider dispatch. Ordinary
exceptions and cancellation here propagate after confirmed-unsent settlement.
The gateway does not convert a local integrity or database error into a normal
successful result or an automatic provider retry.

The final `before_send` guard stores the exception that prevented sending. The
OpenAI-compatible provider runs that guard both before entering its HTTP call
and at the request-headers trace boundary. A guard failure can therefore be
classified as unsent even if the HTTP adapter wraps it in a transport exception.
The original guard exception is propagated, with a wrapper retained as its
cause where applicable. No exception text is copied into model error codes.

Without a failed guard, provider work may have begun. In-flight cancellation,
runtime invalidation, unexpected provider exceptions, and provider-reported
unknown outcomes retain conservative accounting and fee reservations. A
provider raising an authorization-named exception after it was entered is not
itself proof of a local guard refusal. Known provider failures and normal
bounded retry behavior keep their existing contracts.

## Atomic confirmed-unsent settlement

`ModelLedger.fail_unsent_attempt` is a narrow terminal accounting transaction.
It verifies the call and attempt, checks their subject/call association, and
requires an executing call with this exact authorized/executing attempt still
latest and no other active attempt.

Cancellation of that attempt and failure of the logical call commit together.
If the call or attempt has already advanced, the operation leaves it unchanged.
If either write fails, both writes roll back. Accounting failures stay visible;
the original dispatch failure remains in the exception chain. Corrupted ledger
evidence does not authorize cancellation or release of its reservation.

Only the current confirmed-unsent attempt is excluded from budget usage.
Earlier charged attempts and unknown usage reservations remain unchanged. A
failed explicit retry of an older unknown request preserves the original
unknown attempt and one-shot reconciliation fence. A failed logical call does
not imply that its historical attempts cost nothing.

The transaction bypasses stale-lease fencing only for terminal accounting. It
never admits model work, commits cognition, grants permission, or sends data.
No schema migration or historical state rewrite is required. Existing known
lifecycle and authorization refusals use the same atomic operation for their
confirmed-unsent attempt.

## Scope and verification

Detailed audit evidence and raw test output remain outside the public source
tree. Local tests cover admission/start/authorization failures, transaction
rollback, atomic settlement failure, final HTTP guards and wrapped exceptions,
unknown fees, repeated attempts, concurrent state changes, routed local errors,
corrupted evidence, restart, and real provider-dispatch boundaries implemented
with a synthetic local HTTP transport.

This protocol does not replace unknown reconciliation or automatically settle
arbitrary old executing rows. It does not redesign mutable provider callback
sharing or introduce per-message approvals, new allowlists, or broader runtime
permissions. Already dispatched work cannot be undone by local cleanup.
