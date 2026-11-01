# Autonomous scheduling and acquisition boundaries

Autonomous work retains its existing capability, resource and lease checks. Scheduler
service is an opportunity to consider a workflow, not an obligation to think, publish,
contact somebody, or create a goal. Public reading stays open by default.

## Acquisition cost and evidence

Public URL validation checks credential parameter names and delimited credential words
instead of arbitrary substrings: ordinary `author` and `keyword` queries are usable,
while encoded token, API-key, password and signature parameters remain private.

The public DNS transport passes the remaining connection budget through its bounded
worker into the killable system resolver. Gate waiting does not grant a new DNS budget.
SafeWebReader uses this path by default; custom resolvers remain enclosed by the real
request and connect deadlines. Model request timeouts are a separate configuration.

Feed ingestion keeps the existing per-poll observation limit and lossless fragmentation.
Seen identities are probed by the complete indexed key in small batches, instead of
loading the entire retained cache for an unchanged feed. Cache pruning is needed only
when new identities are written. No observation, fingerprint or historical counter is
rewritten by this optimization.

Acquisition diagnostics explicitly distinguish acknowledged identities (including partial
entry identities), retained deduplication identities, observation batches, and character
budgets. These counters are not complete article counts or daily activity totals.

## Persisted serial service

Schema 112 adds one bounded scheduler-control row per subject, with a reviewed DDL
fingerprint, strict payload checks, subject ownership, retention classification and
runtime-export coverage. No historical observation or workflow evidence is migrated
into a made-up scheduler success. Existing schema 111 data upgrades additively.

A typed outcome separates actual dispatched work, useful new progress, the wait reason
and the next consideration time. The compatibility adapter treats old wait/failure/replay
strings as yielding results. Formal ledger reservations and actions independently tell
the scheduler that work occurred, even if it failed or changed no useful state.

The serial turn rotates its first workflow after work and resumes the next unserved
workflow on the next turn. Expensive work ends the turn; idle consideration is bounded
both by the fixed workflow inventory and a small between-callback time budget. A running
model call is never preempted to meet that budget. Restart keeps the cursor, and an
invalidated runtime lease cannot publish a successful service checkpoint.

In-process cooldowns use monotonic elapsed time; UTC remains the explicit recovery and
observation clock. Model-attempt admission cannot reopen a budget day older than the
subject's existing attempt history. This is not a budget reset or rewritten timestamp.
