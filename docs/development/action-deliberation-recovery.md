# Action deliberation admission and paid recovery

Action deliberation still chooses a bounded public read for an existing
autonomous goal. This change preserves the existing subject-wide mutual
exclusion rule: another `prepared`, `executing` or `unknown` action requires
reconciliation before a new deliberated action proceeds. No permission,
public-reading policy, source allowlist or per-message approval is added.

## Admission before spending

An unrelated open action yields `action_deliberation_waiting_reconciliation`
before building a new paid request. Model-call preparation rechecks this condition
inside the ledger transaction. Tool intent preparation rechecks it inside the
action writer transaction through an optional admission callback. Existing tool
callers do not enable that callback and retain their behavior.

If another action appears while the provider is already working, the completed
model response remains a paid success. The temporary conflict does not create a
proposal rejection or a new model request. Resolving the conflict allows the
same response and the same action identity to continue. A nonblocking worker lock
prevents concurrent deliberation workers from paying or acting twice.

Recovery can identify a previously prepared action belonging to its own cached
successful proposal. Other open actions still block it. Its own `executing` or
`unknown` action also waits; this is not permission to restart an interrupted
read. A completed read can still be recovered into the local deliberation record
without repeating the fetch, under the existing tool-ledger contract.

The waiting result is recorded as a nonproductive wait in metacognitive learning,
not a new model failure. Waiting does not advance a business round, update goal
progress, settle an action, or release any unknown fee reservation.

## Original evidence and successful responses

For new calls, preparation atomically appends a private, hash-verified context
receipt with the call/request identity, original context hash, allowed goal and
source fingerprints, and selected event metadata. It does not duplicate raw goal
descriptions, messages or prompt text. The request hash must match the actual
prompt, schema and generation settings before a provider request is sent.

When recovering a paid success, the integrator verifies the ledger response hash
and receipt, uses the original allowed evidence IDs, and rechecks the current
owned goal/source state, capability and action limits. Newer events pushing an
original event out of the latest-window sample cannot force a replacement paid
request. A changed goal/source revision, missing or withdrawn evidence, a forged
ID or corrupt response does not gain acceptance through recovery. Existing
successful responses are considered before new-call daily quotas and intervals.

A logical deliberation round is independent of the calendar day and route. The
existing shared model business-round fence also covers numeric
`action_deliberation:<round>` purposes. Unknown/executing calls and unfinished
one-shot retry authorizations stay fenced; cancellation does not create a new
identity or erase the original costs. A conflicting prepared request context is
quarantined instead of silently replaced.

Old successful calls have no context receipt. They retain their previous
current-evidence recovery checks, with ledger response verification; the code
does not manufacture historical fingerprints for them. Exact original-window
recovery is guaranteed only for newly bound calls. No schema migration or rewrite
of old calls, actions, observations or rejected proposals is performed.

## Limits of the guarantee

Admission checks cannot undo a provider request that was already sent before
another action appeared. That race is handled by retaining and deferring its
paid result. The action admission callback linearizes this lane's own intent;
it does not change every other subsystem into one global lock held across network
I/O. Other subsystems retain their existing admission rules.

The existing semantic-rejection and goal daily-action-limit rules remain in
place. This protocol distinguishes open-action uncertainty from such validation
failures; it does not declare every refused proposal a successful action.
