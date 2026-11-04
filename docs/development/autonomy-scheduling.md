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

## Runtime integration and recovery

After genesis completes, independent serial service slots cover local maintenance,
project execution and review, interaction handling, metacognitive selection, world
acquisition, world interpretation and optional embedding maintenance. Embedding refresh
is not an unconditional paid prelude to every post-genesis scheduler turn. The existing genesis state machine retains its
ordering. Metacognition still owns whether research, thought, social contact or sleep
is worthwhile; the scheduler does not replace that choice with mandatory activity.

Projects rotate independently within execution and review service. A particular
project's deferred review does not impose its deadline on every other project.
Formation still receives a review-service opportunity when no project exists.

Acquisition runs independently of a pending observation's interpretation. Interpretation
skips unresolved provider rounds using the existing business-call ambiguity fence,
without resolving, rejecting or replaying them; other runnable observations can proceed. A permanent
fetch failure advances only its own failure/check heartbeat and yields to other sources.
Successful unchanged reads update heartbeat evidence without a new model call. Ordered
poll history remains unchanged on wall-clock rollback: a source whose history is in
the future yields until UTC catches up, while other work keeps receiving service. An
in-flight rollback becomes an explicit waiting outcome instead of a repeating failure.

Agenda discovery is considered independently of project completion. Discovery uses
bounded candidate probes rather than one lookup per candidate, coalesces duplicate
source identities, and does not rewrite unchanged agenda history. A restarted process
does not write agenda revisions behind their existing timestamp high-water mark.

The authenticated runtime summary reports the clock status and each workflow's most
recent consideration, finished attempt, new progress, wait reason and next consideration.
The service bound is expressed in completed scheduler turns, not an unconditional wall
clock promise: existing network deadlines, budgets, real sleep, permission changes and
unresolved prerequisites remain authoritative. No in-flight paid call is preempted.

Wallet automation reports configuration separately from execution observation.
`execution_observation.status` is `ok` only when the pending counts, latest failure and
active execution count were read successfully in one snapshot. Otherwise the observation
is `unavailable`, its reason is a stable code, and those measures are `null`. The console
shows an unread state rather than zero pending work or an absence of failures. This is a
read-side diagnostic; it does not settle unknown payments or change automation policy.

Local acceptance covers restart/cursor persistence, malformed control state, schema 111
upgrade, exact export ownership, UTC rollback, multiple projects, a permanently failing
source, 72-hour virtual operation, and low-cost unchanged-feed polls. Real deployment,
platform interaction and sustained server observation remain separate acceptance steps.
