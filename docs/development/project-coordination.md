# Project coordination and temporary recovery

Project execution, governance and model accounting remain separate responsibilities.
The coordinator distinguishes new execution work from a durable replay, waits for
project-related evidence, and schedules governance without treating a cached result
as another completed activity. A completed execution is accepted as phase completion
only after the existing evidence and revision checks succeed.

## Review recovery

Review waits use the existing `workflow_checkpoints` contract with owned, revision-bound
identities. Daily quota and admission waits do not advance a phase attempt or its
no-progress counter. Definite model failures use bounded, durable backoff; exhaustion
cools until the next UTC day and still respects the project's total resource envelope.
An executing or unknown provider result requires reconciliation before another request.
Changing a request generation is never a way to resend an ambiguous operation.

Successful reviews continue to use the existing project and phase revision history.
A deliberate `wait` also defers phase execution until the next consideration time.
Execution clocks start only for actual admitted review work or executable phase work;
replaying a terminal result does not create another session. Historical clocks are
preserved, and project execution time is not a provider billing ledger.

## Assistance

An open project assistance request has one stable web-origin help outbox intent.
Only its explicitly public summary is copied to that intent. Existing configured
transport delivery remains responsible for dispatch and reconciliation. Without a
transport, the request is pending route configuration and only its project waits.
Having a transport is not proof of delivery: pending, unknown and confirmed-delivered
states are reported separately. Normal public reading and sustained capability grants
are unchanged; no domain allowlist or per-message approval is introduced.

## Temporary recovery admission

A durable recovery guard is activated only by evidence of a legacy project replay
incident. It is not a normal-install model quota. All model attempts admitted while
it is active share conservative reservations in the same SQLite transaction as their
formal ledger reservation, including provider retries and routed failover.

Temporary bounds are 6 admitted attempts per rolling hour, 24 per rolling day,
400,000 input tokens and 80,000 output tokens per rolling day. Reservations are not
refunded after an admitted attempt, and do not represent actual provider billing.
Existing tighter resource, project and workflow budgets remain authoritative.

The guard automatically retires after an observed stable recovery interval: pending
results are either handled or explicitly accounted for, source checks receive service,
the observation backlog is bounded, and recent provider failures are not a storm.
Offline elapsed time alone is not evidence of stability. A retired guard is not
reactivated by the same historical incident. Unknown prices remain unknown.

## Observation and compatibility

The authenticated runtime summary separates base ledger observation from optional
coordination observation. It exposes bounded project wait and execution metadata,
source check/success/content-change timestamps, and temporary guard status and limits.
It does not expose prompts, responses, private help descriptions or credentials.
Missing optional observations are shown as unavailable rather than healthy zeros.

This change uses the current schema and its existing workflow ownership, export and
integrity contracts; it does not rewrite historical rows or add untracked state files.
Validation includes synthetic legacy state, restart recovery, atomic concurrent model
admission, rolling windows, a 72-hour virtual clock and unchanged-feed heartbeats.
Actual deployment and a sustained production observation window are separate release
acceptance steps; local tests are not evidence that a server has been upgraded.
