# Model invalidation before attempt admission

The model gateway separates local runtime invalidation from provider outcome
uncertainty. An invalid lease stops further work; it is not evidence that an
unsent request reached a provider, or that a possibly sent request was free.
The logical call and each provider attempt keep their separate states.

## Phase-specific outcomes

| Invalidation point | Logical call | Attempt and cost evidence |
| --- | --- | --- |
| Before preparation commits | No new call | No new attempt |
| After preparation, before first authorization | `failed` if still unclaimed | No new attempt or reservation |
| Authorization transaction rolls back | `failed` at the original safe boundary | Rolled-back attempt creates no reservation |
| Authorized or started, with dispatch confirmed unsent | Existing `failed` path | Existing `cancel_unsent_attempt` path |
| After an earlier attempt settles, before admitting a retry | `failed` at the caller's own settled boundary | Earlier fees and unknown usage reservations stay unchanged |
| Request may already have been sent | Existing conservative `unknown` path | Reservation remains held |
| Provider result is known after fencing | Existing actual success/failure accounting | Known usage is recorded; stale result remains fenced from cognition |

A `failed` logical call does not imply zero historical cost. For example, a
paid response rejected by the output schema still has a charged successful
attempt. Closing the logical retry loop does not erase that attempt.

## Atomic closure without claiming another worker's result

`ModelLedger.fail_before_attempt` runs a narrow accounting transaction so
terminal bookkeeping remains possible after lease invalidation. It admits no
new provider work and makes no changes to attempts, reservations, response
payloads, or usage evidence.

For an initial `prepared` call, there must be no active attempt. For an
`executing` call between retries, the latest attempt must be the exact
`settled_attempt_id` remembered by this gateway invocation and must already be
`succeeded` or `failed`. There must be no authorized or executing attempt.

These checks and the final update share one writer transaction. If another
worker has advanced the call, a different attempt became latest, or the call
is already terminal/unknown, the method returns false without rewriting the
shared state. The stale caller still receives `OperationInvalidated`; it must
not claim that the other worker's request failed or was cancelled.

Preparation of an explicit unknown retry does not settle its original unknown
attempt. If that retry is invalidated before authorization, the original
attempt, fees, and one-shot reconciliation fence remain unchanged. Existing
explicit reconciliation rules still determine whether another retry is safe.

## Exceptions, compatibility, and verification

With healthy accounting, the gateway re-raises the original invalidation
exception. If accounting itself fails, the accounting error remains visible
with the original invalidation in its exception context. No successful
settlement is claimed in that case.

No schema migration, historical rewrite, retry identity change, or provider
behavior change is required. Existing ledger states remain readable by the
previous runtime. A completed unsent failure survives reopening and is not
implicitly retried under its original idempotency key.

Local verification covers prepare/authorize/start/send boundaries, transaction
rollback, paid and unknown-usage retry history, competing worker state changes,
explicit unknown retries, restart, accounting failure, normal retries and
caching, and the existing final HTTP dispatch checks. Detailed test output and
audit evidence are retained outside the public repository.
